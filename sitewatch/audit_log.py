"""Durable audit trail for human-initiated create/update/delete actions on
user-managed objects. Contrast with job_log.py: that module is in-memory,
capped at 300 entries, and wiped on restart — it's "recent background
activity" (poll cycles, walk/repoll), not an audit log (see its own
docstring). This module is DB-backed, unbounded until pruned, and about
*who changed what* — called explicitly from routes, right alongside their
own db.session.commit(), never via an automatic ORM/session hook. See
CLAUDE.md and routes/*.py for call sites.
"""
import json
import logging
from datetime import datetime, timedelta

from flask_login import current_user

from sitewatch.extensions import db
from sitewatch.models import AuditLog, Setting

log = logging.getLogger(__name__)

# Fields that must NEVER appear in details — even redacted — because they
# carry credential material or its ciphertext. Routes should never build a
# diff over these; use a boolean marker like {"credentials_updated": True}
# instead (see devices.py's add_device/edit_device).
CREDENTIAL_FIELDS = frozenset({
    "snmp_community", "snmp_community_enc",
    "snmpv3_auth_key", "snmpv3_auth_key_enc",
    "snmpv3_priv_key", "snmpv3_priv_key_enc",
    "ssh_password", "ssh_password_enc",
    "password", "password_hash",
})


def record(action, object_type, object_id, label, details=None):
    """Adds one AuditLog row to the session — does NOT call commit(). Call
    this BEFORE the route's own db.session.commit(), never after, so the
    audit row and the mutation it describes land in the exact same
    transaction: if the commit fails, neither persists, and there's never
    a ghost audit entry for a mutation that didn't actually happen.
    Captures the acting user from flask_login's current_user automatically
    — every mutating route in this app runs inside an authenticated
    request context (see auth.py's admin_required/login_required)."""
    entry = AuditLog(
        user_id=current_user.id if current_user.is_authenticated else None,
        username=current_user.username if current_user.is_authenticated else "system",
        action=action,
        object_type=object_type,
        object_id=object_id,
        label=(label or "")[:200],
        details=json.dumps(details, default=str) if details else None,
    )
    db.session.add(entry)
    return entry


def diff_fields(before, after, exclude=()):
    """before/after: plain dicts, the SAME editable-field snapshot taken
    pre- and post-mutation. Returns {"field": {"old":..., "new":...}} only
    for fields that actually changed, skipping anything in `exclude`
    (always pass CREDENTIAL_FIELDS for a model that has them). An empty
    result means nothing actually changed — callers skip calling record()
    at all in that case, so a Save that changes nothing isn't logged (this
    is an audit of mutations, not of button clicks)."""
    out = {}
    for key, new in after.items():
        old = before.get(key)
        if key in exclude or old == new:
            continue
        out[key] = {"old": old, "new": new}
    return out


def prune_old_entries():
    """Deletes AuditLog rows older than Setting "audit_log_retention_days".
    0 (or negative) disables pruning — keep every entry forever. Called by
    the daily job registered in poller.py's start_poller()."""
    days = Setting.get_int("audit_log_retention_days", 90)
    if days <= 0:
        return 0
    cutoff = datetime.utcnow() - timedelta(days=days)
    deleted = AuditLog.query.filter(AuditLog.created_at < cutoff).delete(synchronize_session=False)
    db.session.commit()
    log.info("Audit log prune: removed %d row(s) older than %d day(s).", deleted, days)
    return deleted
