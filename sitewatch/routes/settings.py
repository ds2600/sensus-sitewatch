import json
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, Response, jsonify, current_app
from flask_login import current_user

from sitewatch.auth import admin_required
from sitewatch.extensions import db
from sitewatch.models import Setting, CircuitRole, Circuit, Region, Site, Device, User, USER_ROLES
from sitewatch.integrations import netbox
from sitewatch.backup import export_data, import_data, BackupImportError, SCOPES
from sitewatch.poller import (
    get_poller_status, pause_poller, resume_poller, poller_enabled_for_process, reschedule_poller,
    poll_device_now,
)
from sitewatch import job_log, cooldown

settings_bp = Blueprint("settings", __name__, url_prefix="/settings")

# Sentinel cooldown target for the repoll-all sweep itself (distinct from
# any real device id) — stops the sweep action from being re-triggered
# within 60s, on top of skipping individual devices still on their own
# cooldown inside the sweep.
_REPOLL_ALL_TARGET = "repoll_all_unreachable"


@settings_bp.route("/", methods=["GET", "POST"])
@admin_required
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
    unreachable_count = Device.query.filter_by(reachable=False).count()
    return render_template("settings.html", values=values, roles=CircuitRole.query.all(),
                            site_regions=Region.query.order_by(Region.name).all(),
                            poll_stats=poll_stats, poller_status=get_poller_status(),
                            unreachable_count=unreachable_count,
                            users=User.query.order_by(User.username).all(), user_roles=USER_ROLES)


@settings_bp.route("/activity")
@admin_required
def activity():
    return render_template("activity.html")


@settings_bp.route("/poller/start", methods=["POST"])
@admin_required
def poller_start():
    if poller_enabled_for_process():
        resume_poller()
        flash("Poller started.")
    else:
        flash("Poller not running in this process.")
    return redirect(url_for("settings.index"))


@settings_bp.route("/poller/stop", methods=["POST"])
@admin_required
def poller_stop():
    if poller_enabled_for_process():
        pause_poller()
        flash("Poller stopped.")
    else:
        flash("Poller not running in this process.")
    return redirect(url_for("settings.index"))


@settings_bp.route("/repoll-unreachable", methods=["POST"])
@admin_required
def repoll_unreachable():
    """Serial (no thread pool) sweep of every currently-unreachable device
    — deliberately gentler/simpler than the normal concurrent poll cycle
    (poller.py's poll_all_devices), since this is an operator-triggered,
    infrequent "did anything come back" check, not the routine cadence.
    Reuses poll_device_now — the same single-device, no-concurrency path
    repoll_device already uses — just looped."""
    wait = cooldown.remaining(_REPOLL_ALL_TARGET)
    if wait:
        return jsonify({"error": f"Wait {wait}s before running this again."}), 429
    devices = Device.query.filter_by(reachable=False).all()
    if not devices:
        return jsonify({"error": "No unreachable devices."}), 400
    cooldown.start(_REPOLL_ALL_TARGET)

    label = f"Repolling {len(devices)} unreachable device(s)"
    job_id = job_log.start_job(label, total=len(devices))
    device_ids = [d.id for d in devices]

    def work():
        for i, device_id in enumerate(device_ids, start=1):
            if job_log.cancel_requested(job_id):
                job_log.log_line(job_id, f"Stopped by user after {i - 1}/{len(device_ids)} device(s).")
                break
            # Skip (don't fail the whole sweep over) a device someone just
            # walked/repolled individually within its own last 60s.
            if cooldown.remaining(device_id) is None:
                cooldown.start(device_id)
                poll_device_now(Device.query.get(device_id))
            else:
                job_log.log_line(job_id, f"Skipping device {device_id} — hit within the last minute elsewhere.")
            job_log.set_progress(job_id, i)

    job_log.run_in_background(job_id, work, current_app._get_current_object())
    # This button lives on both Settings and the Circuits list — send the
    # user back to whichever one they clicked it from, not always Settings.
    next_url = request.args.get("next")
    if not next_url or not next_url.startswith("/"):
        next_url = url_for("settings.index")
    return jsonify({"job_id": job_id, "label": label, "redirect": next_url})


@settings_bp.route("/roles/add", methods=["POST"])
@admin_required
def add_role():
    db.session.add(CircuitRole(name=request.form["name"], tier=request.form["tier"]))
    db.session.commit()
    return redirect(url_for("settings.index"))


@settings_bp.route("/roles/<int:role_id>/update", methods=["POST"])
@admin_required
def update_role(role_id):
    role = CircuitRole.query.get_or_404(role_id)
    role.tier = request.form["tier"]
    db.session.commit()
    return redirect(url_for("settings.index"))


@settings_bp.route("/roles/<int:role_id>/delete", methods=["POST"])
@admin_required
def delete_role(role_id):
    role = CircuitRole.query.get_or_404(role_id)
    if Circuit.query.filter_by(role_id=role.id).first():
        flash(f"'{role.name}' is in use by circuits — reassign them first.")
        return redirect(url_for("settings.index"))
    db.session.delete(role)
    db.session.commit()
    return redirect(url_for("settings.index"))


@settings_bp.route("/site-regions/add", methods=["POST"])
@admin_required
def add_site_region():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Name a region before adding it.")
        return redirect(url_for("settings.index"))
    if Region.query.filter_by(name=name).first():
        flash(f"A region named '{name}' already exists.")
        return redirect(url_for("settings.index"))
    db.session.add(Region(name=name))
    db.session.commit()
    return redirect(url_for("settings.index"))


@settings_bp.route("/site-regions/<int:region_id>/delete", methods=["POST"])
@admin_required
def delete_site_region(region_id):
    region = Region.query.get_or_404(region_id)
    if Site.query.filter_by(region_id=region.id).first():
        flash(f"'{region.name}' is in use by sites — reassign them first.")
        return redirect(url_for("settings.index"))
    db.session.delete(region)
    db.session.commit()
    return redirect(url_for("settings.index"))


@settings_bp.route("/netbox/sync", methods=["POST"])
@admin_required
def netbox_sync():
    try:
        netbox.sync_sites()
        netbox.sync_devices()
        flash("NetBox sync complete.")
    except RuntimeError as e:
        flash(str(e))
    return redirect(url_for("settings.index"))


@settings_bp.route("/export")
@admin_required
def export_backup():
    scope = request.args.get("scope", "all")
    if scope not in SCOPES:
        flash(f"Unknown export scope: {scope!r}.")
        return redirect(url_for("settings.index"))
    payload = json.dumps(export_data(scope=scope), indent=2)
    suffix = "backup" if scope == "all" else scope
    filename = f"sitewatch-{suffix}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@settings_bp.route("/import", methods=["POST"])
@admin_required
def import_backup():
    scope = request.form.get("scope", "all")
    if scope not in SCOPES:
        flash(f"Unknown import scope: {scope!r}.")
        return redirect(url_for("settings.index"))

    file = request.files.get("backup_file")
    if not file or file.filename == "":
        flash("Choose a JSON file to import.")
        return redirect(url_for("settings.index"))

    try:
        data = json.load(file.stream)
    except (json.JSONDecodeError, UnicodeDecodeError):
        flash("That file isn't valid JSON.")
        return redirect(url_for("settings.index"))

    try:
        import_data(data, scope=scope)
    except BackupImportError as e:
        flash(str(e))
        return redirect(url_for("settings.index"))

    if scope in ("all", "devices"):
        flash("Import complete. Re-enter device credentials — not included in backups.")
        return redirect(url_for("devices.list_devices"))
    flash("Import complete.")
    return redirect(url_for("settings.index"))


@settings_bp.route("/users/add", methods=["POST"])
@admin_required
def add_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "read_only")
    if role not in USER_ROLES:
        role = "read_only"
    if not username or not password:
        flash("Username and password are required.")
        return redirect(url_for("settings.index"))
    if User.query.filter_by(username=username).first():
        flash(f"A user named '{username}' already exists.")
        return redirect(url_for("settings.index"))
    user = User(username=username, role=role)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    flash(f"User '{username}' added.")
    return redirect(url_for("settings.index"))


@settings_bp.route("/users/<int:user_id>/role", methods=["POST"])
@admin_required
def update_user_role(user_id):
    user = User.query.get_or_404(user_id)
    role = request.form.get("role")
    if role not in USER_ROLES:
        flash("Invalid role.")
        return redirect(url_for("settings.index"))
    if user.id == current_user.id and role != "admin":
        flash("You can't demote your own account.")
        return redirect(url_for("settings.index"))
    user.role = role
    db.session.commit()
    return redirect(url_for("settings.index"))


@settings_bp.route("/users/<int:user_id>/password", methods=["POST"])
@admin_required
def reset_user_password(user_id):
    user = User.query.get_or_404(user_id)
    password = request.form.get("password", "")
    if not password:
        flash("Enter a new password.")
        return redirect(url_for("settings.index"))
    user.set_password(password)
    db.session.commit()
    flash(f"Password updated for '{user.username}'.")
    return redirect(url_for("settings.index"))


@settings_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id):
    # No separate "last admin" guard needed: only an admin can reach this
    # route at all, and the self-delete check below means deleting anyone
    # ELSE always leaves at least the current admin standing — the only
    # way to strand the app with zero admins would be deleting yourself
    # as the last one, which this already blocks.
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("You can't delete your own account.")
        return redirect(url_for("settings.index"))
    db.session.delete(user)
    db.session.commit()
    return redirect(url_for("settings.index"))
