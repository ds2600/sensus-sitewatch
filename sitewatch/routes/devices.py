from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required

from sitewatch.extensions import db
from sitewatch.models import Device, Site, Interface, VENDORS, SNMP_VERSIONS
from sitewatch.snmp import walk_interfaces, SnmpError

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
        discovered = walk_interfaces(device)
    except SnmpError as e:
        flash(f"Walk failed: {e}")
        return redirect(url_for("devices.device_detail", device_id=device_id))

    existing = {i.if_index: i for i in device.interfaces}
    for idx, data in discovered.items():
        if idx in existing:
            iface = existing[idx]
            iface.if_descr = data["if_descr"]
            iface.if_alias = data["if_alias"]
            iface.if_speed_bps = data["if_speed_bps"]
        else:
            db.session.add(Interface(device_id=device.id, if_index=idx, **data))

    device.last_walked_at = datetime.utcnow()
    db.session.commit()
    flash(f"Walk complete: {len(discovered)} interfaces found.")
    return redirect(url_for("devices.device_detail", device_id=device_id))
