"""audit_log.py itself — diffing, credential exclusion, and retention
pruning, independent of any one route's use of it. record()'s
current_user capture is covered by tests/test_routes_audit.py instead of
here, since it only resolves inside a real logged-in request.
"""
from datetime import datetime, timedelta

from sitewatch import audit_log
from sitewatch.extensions import db
from sitewatch.models import AuditLog, Setting


def test_diff_fields_only_reports_changed_keys():
    before = {"a": 1, "b": 2, "c": 3}
    after = {"a": 1, "b": 20, "c": 3}
    assert audit_log.diff_fields(before, after) == {"b": {"old": 2, "new": 20}}


def test_diff_fields_empty_when_nothing_changed():
    same = {"a": 1, "b": 2}
    assert audit_log.diff_fields(same, dict(same)) == {}


def test_diff_fields_respects_exclude():
    before = {"hostname": "old", "snmp_community": "secret1"}
    after = {"hostname": "new", "snmp_community": "secret2"}
    diff = audit_log.diff_fields(before, after, exclude=audit_log.CREDENTIAL_FIELDS)
    assert diff == {"hostname": {"old": "old", "new": "new"}}
    assert "snmp_community" not in diff


def test_prune_old_entries_removes_only_rows_past_retention(app):
    with app.app_context():
        Setting.set("audit_log_retention_days", "30")
        db.session.commit()

        old = AuditLog(username="admin", action="create", object_type="Site", object_id=1, label="Old",
                        created_at=datetime.utcnow() - timedelta(days=40))
        recent = AuditLog(username="admin", action="create", object_type="Site", object_id=2, label="Recent",
                           created_at=datetime.utcnow() - timedelta(days=5))
        db.session.add_all([old, recent])
        db.session.commit()

        deleted = audit_log.prune_old_entries()

        assert deleted == 1
        remaining = AuditLog.query.all()
        assert len(remaining) == 1
        assert remaining[0].label == "Recent"


def test_prune_disabled_when_retention_is_zero(app):
    with app.app_context():
        Setting.set("audit_log_retention_days", "0")
        db.session.commit()

        old = AuditLog(username="admin", action="create", object_type="Site", object_id=1, label="Ancient",
                        created_at=datetime.utcnow() - timedelta(days=10000))
        db.session.add(old)
        db.session.commit()

        deleted = audit_log.prune_old_entries()

        assert deleted == 0
        assert AuditLog.query.count() == 1
