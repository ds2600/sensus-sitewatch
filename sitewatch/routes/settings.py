import json
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, Response
from flask_login import login_required

from sitewatch.extensions import db
from sitewatch.models import Setting, CircuitRole, Circuit
from sitewatch.integrations import netbox
from sitewatch.backup import export_data, import_data, BackupImportError
from sitewatch.poller import (
    get_poller_status, pause_poller, resume_poller, poller_enabled_for_process, reschedule_poller,
)

settings_bp = Blueprint("settings", __name__, url_prefix="/settings")


@settings_bp.route("/", methods=["GET", "POST"])
@login_required
def index():
    if request.method == "POST":
        old_interval = Setting.get("polling_interval_minutes")
        for key in Setting.DEFAULTS:
            if key in request.form:
                Setting.set(key, request.form[key])
        db.session.commit()
        new_interval = request.form.get("polling_interval_minutes")
        if new_interval and new_interval != old_interval:
            reschedule_poller(int(new_interval))
            flash("Settings saved.")
        return redirect(url_for("settings.index"))
    values = {k: Setting.get(k) for k in Setting.DEFAULTS}
    poll_stats = {
        "duration": Setting.get("last_poll_duration_seconds"),
        "finished_at": Setting.get("last_poll_finished_at"),
    }
    return render_template("settings.html", values=values, roles=CircuitRole.query.all(),
                            poll_stats=poll_stats, poller_status=get_poller_status())


@settings_bp.route("/poller/start", methods=["POST"])
@login_required
def poller_start():
    if poller_enabled_for_process():
        resume_poller()
        flash("Poller started.")
    else:
        flash("Poller not running in this process.")
    return redirect(url_for("settings.index"))


@settings_bp.route("/poller/stop", methods=["POST"])
@login_required
def poller_stop():
    if poller_enabled_for_process():
        pause_poller()
        flash("Poller stopped.")
    else:
        flash("Poller not running in this process.")
    return redirect(url_for("settings.index"))


@settings_bp.route("/roles/add", methods=["POST"])
@login_required
def add_role():
    db.session.add(CircuitRole(name=request.form["name"], tier=request.form["tier"]))
    db.session.commit()
    return redirect(url_for("settings.index"))


@settings_bp.route("/roles/<int:role_id>/update", methods=["POST"])
@login_required
def update_role(role_id):
    role = CircuitRole.query.get_or_404(role_id)
    role.tier = request.form["tier"]
    db.session.commit()
    return redirect(url_for("settings.index"))


@settings_bp.route("/roles/<int:role_id>/delete", methods=["POST"])
@login_required
def delete_role(role_id):
    role = CircuitRole.query.get_or_404(role_id)
    if Circuit.query.filter_by(role_id=role.id).first():
        flash(f"Can't delete role '{role.name}' — circuits still use it. Reassign them first.")
        return redirect(url_for("settings.index"))
    db.session.delete(role)
    db.session.commit()
    return redirect(url_for("settings.index"))


@settings_bp.route("/netbox/sync", methods=["POST"])
@login_required
def netbox_sync():
    try:
        netbox.sync_sites()
        netbox.sync_devices()
        flash("NetBox sync complete.")
    except RuntimeError as e:
        flash(str(e))
    return redirect(url_for("settings.index"))


@settings_bp.route("/export")
@login_required
def export_backup():
    payload = json.dumps(export_data(), indent=2)
    filename = f"sitewatch-backup-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@settings_bp.route("/import", methods=["POST"])
@login_required
def import_backup():
    file = request.files.get("backup_file")
    if not file or file.filename == "":
        flash("Choose a backup JSON file to import.")
        return redirect(url_for("settings.index"))

    try:
        data = json.load(file.stream)
    except (json.JSONDecodeError, UnicodeDecodeError):
        flash("That file isn't valid JSON.")
        return redirect(url_for("settings.index"))

    try:
        import_data(data)
    except BackupImportError as e:
        flash(str(e))
        return redirect(url_for("settings.index"))

    flash("Import complete. Credentials aren't included in backups — re-enter "
          "SNMP/SSH credentials on each device (Devices page) before polling will work.")
    return redirect(url_for("devices.list_devices"))
