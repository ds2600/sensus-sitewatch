from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required

from sitewatch.extensions import db
from sitewatch.models import Device, Site, VENDORS, SNMP_VERSIONS
from sitewatch.snmp import SnmpError
from sitewatch.discovery import perform_walk

devices_bp = Blueprint("devices", __name__, url_prefix="/devices")


@devices_bp.route("/")
@login_required
def list_devices():
    return render_template("devices.html", devices=Device.query.all())


@devices_bp.route("/add", methods=["GET", "POST"])
@login_required
def add_device():
    if request.method == "POST":
        f = request.form
        device = Device(
            site_id=int(f["site_id"]), hostname=f["hostname"], mgmt_ip=f["mgmt_ip"],
            vendor=f["vendor"], snmp_version=f["snmp_version"], source="manual",
        )
        if device.snmp_version in ("v1", "v2c"):
            device.snmp_community = f.get("snmp_community", "")
        else:
            device.snmpv3_username = f.get("snmpv3_username")
            device.snmpv3_auth_protocol = f.get("snmpv3_auth_protocol")
            device.snmpv3_auth_key = f.get("snmpv3_auth_key", "")
            device.snmpv3_priv_protocol = f.get("snmpv3_priv_protocol")
            device.snmpv3_priv_key = f.get("snmpv3_priv_key", "")
        db.session.add(device)
        db.session.commit()
        return redirect(url_for("devices.device_detail", device_id=device.id))
    return render_template("device_form.html", sites=Site.query.all(),
                            vendors=VENDORS, snmp_versions=SNMP_VERSIONS)


@devices_bp.route("/<int:device_id>")
@login_required
def device_detail(device_id):
    device = Device.query.get_or_404(device_id)
    return render_template("device_detail.html", device=device)


@devices_bp.route("/<int:device_id>/walk", methods=["POST"])
@login_required
def walk_device(device_id):
    device = Device.query.get_or_404(device_id)
    try:
        count = perform_walk(device)
    except SnmpError as e:
        flash(f"Walk failed: {e}")
        return redirect(url_for("devices.device_detail", device_id=device_id))

    db.session.commit()
    flash(f"Walk complete: {count} interfaces found.")
    return redirect(url_for("devices.device_detail", device_id=device_id))
