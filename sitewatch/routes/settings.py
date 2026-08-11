from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required

from sitewatch.extensions import db
from sitewatch.models import Setting, CircuitRole
from sitewatch.integrations import netbox

settings_bp = Blueprint("settings", __name__, url_prefix="/settings")


@settings_bp.route("/", methods=["GET", "POST"])
@login_required
def index():
    if request.method == "POST":
        for key in Setting.DEFAULTS:
            if key in request.form:
                Setting.set(key, request.form[key])
        db.session.commit()
        return redirect(url_for("settings.index"))
    values = {k: Setting.get(k) for k in Setting.DEFAULTS}
    return render_template("settings.html", values=values, roles=CircuitRole.query.all())


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
