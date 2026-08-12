from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required

from sitewatch.extensions import db
from sitewatch.models import Device, Site, Circuit, VENDORS, SNMP_VERSIONS
from sitewatch.snmp import SnmpError
from sitewatch.discovery import perform_walk
from sitewatch.poller import poll_device_now

devices_bp = Blueprint("devices", __name__, url_prefix="/devices")


def _apply_credentials(device, f):
    """Shared by add/edit. On edit, a blank credential field means "leave
    unchanged" — otherwise saving the form without retyping a password
    would wipe it."""
    if device.snmp_version in ("v1", "v2c"):
        if f.get("snmp_community"):
            device.snmp_community = f["snmp_community"]
    else:
        device.snmpv3_username = f.get("snmpv3_username") or device.snmpv3_username
        device.snmpv3_auth_protocol = f.get("snmpv3_auth_protocol") or device.snmpv3_auth_protocol
        device.snmpv3_priv_protocol = f.get("snmpv3_priv_protocol") or device.snmpv3_priv_protocol
        if f.get("snmpv3_auth_key"):
            device.snmpv3_auth_key = f["snmpv3_auth_key"]
        if f.get("snmpv3_priv_key"):
            device.snmpv3_priv_key = f["snmpv3_priv_key"]


@devices_bp.route("/")
@login_required
def list_devices():
    return render_template("devices.html", devices=Device.query.all())


@devices_bp.route("/add", methods=["GET", "POST"])
@login_required
def add_device():
    if request.method == "POST":
        f = request.form
        site = Site.query.get_or_404(int(f["site_id"]))
        if site.site_type == "passthrough":
            flash("Can't assign a device to a passthrough site — those are map waypoints with no equipment.")
            return redirect(url_for("devices.add_device"))
        device = Device(
            site_id=site.id, hostname=f["hostname"], mgmt_ip=f["mgmt_ip"],
            vendor=f["vendor"], snmp_version=f["snmp_version"], source="manual",
        )
        _apply_credentials(device, f)
        db.session.add(device)
        db.session.commit()
        return redirect(url_for("devices.device_detail", device_id=device.id))
    return render_template("device_form.html", device=None,
                            sites=Site.query.filter_by(site_type="site").all(),
                            vendors=VENDORS, snmp_versions=SNMP_VERSIONS)


@devices_bp.route("/<int:device_id>")
@login_required
def device_detail(device_id):
    device = Device.query.get_or_404(device_id)
    return render_template("device_detail.html", device=device)


@devices_bp.route("/<int:device_id>/edit", methods=["GET", "POST"])
@login_required
def edit_device(device_id):
    device = Device.query.get_or_404(device_id)
    if request.method == "POST":
        f = request.form
        site = Site.query.get_or_404(int(f["site_id"]))
        if site.site_type == "passthrough":
            flash("Can't assign a device to a passthrough site — those are map waypoints with no equipment.")
            return redirect(url_for("devices.edit_device", device_id=device_id))
        device.site_id = site.id
        device.hostname = f["hostname"]
        device.mgmt_ip = f["mgmt_ip"]
        device.vendor = f["vendor"]
        device.snmp_version = f["snmp_version"]
        _apply_credentials(device, f)
        db.session.commit()
        return redirect(url_for("devices.device_detail", device_id=device.id))
    return render_template("device_form.html", device=device,
                            sites=Site.query.filter_by(site_type="site").all(),
                            vendors=VENDORS, snmp_versions=SNMP_VERSIONS)


@devices_bp.route("/<int:device_id>/delete", methods=["POST"])
@login_required
def delete_device(device_id):
    device = Device.query.get_or_404(device_id)
    iface_ids = [i.id for i in device.interfaces]
    in_use = Circuit.query.filter(
        db.or_(Circuit.interface_a_id.in_(iface_ids), Circuit.interface_b_id.in_(iface_ids))
    ).first() if iface_ids else None
    if in_use:
        flash(f"Can't delete this device — its interfaces are used by circuit '{in_use.name}'. Delete that circuit first.")
        return redirect(url_for("devices.device_detail", device_id=device_id))
    db.session.delete(device)  # interfaces cascade-delete automatically
    db.session.commit()
    return redirect(url_for("devices.list_devices"))


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


@devices_bp.route("/<int:device_id>/repoll", methods=["POST"])
@login_required
def repoll_device(device_id):
    device = Device.query.get_or_404(device_id)
    try:
        poll_device_now(device)
    except SnmpError as e:
        flash(f"Repoll failed: {e}")
        return redirect(url_for("devices.device_detail", device_id=device_id))

    if not device.reachable and not _has_snmp_credentials(device):
        flash(f"Repoll complete — {device.hostname} is unreachable: no SNMP credentials "
              f"configured. Set them on the Edit page (devices imported from NetBox never "
              f"carry credentials — they must be entered manually).")
    else:
        flash(f"Repoll complete — {device.hostname} is now "
              f"{'reachable' if device.reachable else 'still unreachable'}.")
    return redirect(url_for("devices.device_detail", device_id=device_id))


def _has_snmp_credentials(device):
    if device.snmp_version in ("v1", "v2c"):
        return bool(device.snmp_community)
    return bool(device.snmpv3_username)
