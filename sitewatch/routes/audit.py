from datetime import datetime, timedelta

from flask import Blueprint, render_template, request

from sitewatch.auth import admin_required
from sitewatch.extensions import db
from sitewatch.models import AuditLog

audit_bp = Blueprint("audit", __name__, url_prefix="/audit")

_PER_PAGE_CHOICES = (25, 50, 100, 250)


@audit_bp.route("/")
@admin_required
def index():
    """Standalone, admin-only, server-side-paginated — deliberately not
    loaded anywhere else (no dashboard widget, nothing joined into another
    page's query) and not using the client-side List.js datatable.js
    pattern the rest of the app's lists use, since that loads every row
    into the DOM at once and this table has no bound on how large it gets.
    Every filter lives in the querystring so results are bookmarkable."""
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    if per_page not in _PER_PAGE_CHOICES:
        per_page = 50

    query = AuditLog.query.order_by(AuditLog.created_at.desc())

    username = request.args.get("username", "").strip()
    if username:
        query = query.filter(AuditLog.username == username)
    object_type = request.args.get("object_type", "").strip()
    if object_type:
        query = query.filter(AuditLog.object_type == object_type)
    action = request.args.get("action", "").strip()
    if action:
        query = query.filter(AuditLog.action == action)
    date_from = request.args.get("date_from", "").strip()
    if date_from:
        try:
            query = query.filter(AuditLog.created_at >= datetime.strptime(date_from, "%Y-%m-%d"))
        except ValueError:
            date_from = ""
    date_to = request.args.get("date_to", "").strip()
    if date_to:
        try:
            query = query.filter(AuditLog.created_at < datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1))
        except ValueError:
            date_to = ""

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    # Distinct values off AuditLog itself, not the live User/model tables —
    # a since-deleted user's or since-renamed role's past rows still need
    # to be filterable, and a join against the live table would silently
    # drop them from the dropdown.
    usernames = [r[0] for r in db.session.query(AuditLog.username).distinct().order_by(AuditLog.username)]
    object_types = [r[0] for r in db.session.query(AuditLog.object_type).distinct().order_by(AuditLog.object_type)]
    actions = [r[0] for r in db.session.query(AuditLog.action).distinct().order_by(AuditLog.action)]

    filters = {"username": username, "object_type": object_type, "action": action,
               "date_from": date_from, "date_to": date_to}
    # Non-empty filters + per_page, reused as **base_args on every
    # pagination link so paging never drops an active filter.
    base_args = {k: v for k, v in filters.items() if v}
    base_args["per_page"] = per_page

    return render_template(
        "audit_log.html", pagination=pagination, entries=pagination.items,
        usernames=usernames, object_types=object_types, actions=actions,
        filters=filters, base_args=base_args,
        per_page=per_page, per_page_choices=_PER_PAGE_CHOICES,
    )
