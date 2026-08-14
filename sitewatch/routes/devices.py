import ipaddress

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app, Response
from flask_login import login_required

from sitewatch.auth import admin_required
from sitewatch.extensions import db
from sitewatch.models import Device, Site, Circuit, CircuitStatusHistory, VENDORS, SNMP_VERSIONS
from sitewatch.discovery import perform_walk
from sitewatch.poller import poll_device_now
from sitewatch import job_log, cooldown, audit_log
from sitewatch.csv_import import parse_csv, CsvImportError

devices_bp = Blueprint("devices", __name__, url_prefix="/devices")

_DEVICE_CSV_HEADER_ALIASES = {
    "site": ["site", "site name"],
    "hostname": ["hostname", "host"],
    "mgmt_ip": ["mgmt ip", "management ip", "ip", "ip address", "mgmt_ip"],
    "vendor": ["vendor"],
}


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


def _credentials_touched(f):
    """True if the submitted form included any credential value — used to
    mark audit details with a boolean flag only, never the actual value
    (or its ciphertext, which is non-deterministic per Fernet encryption
    and would falsely show as "changed" on every save regardless)."""
    return bool(f.get("snmp_community") or f.get("snmpv3_auth_key") or f.get("snmpv3_priv_key"))


@devices_bp.route("/")
@login_required
def list_devices():
    return render_template("devices.html", devices=Device.query.all())


@devices_bp.route("/add", methods=["GET", "POST"])
@admin_required
def add_device():
    if request.method == "POST":
        f = request.form
        site = Site.query.get_or_404(int(f["site_id"]))
        if site.site_type == "passthrough":
            flash("Passthrough sites can't have devices assigned.")
            return redirect(url_for("devices.add_device"))
        device = Device(
            site_id=site.id, hostname=f["hostname"], mgmt_ip=f["mgmt_ip"],
            vendor=f["vendor"], snmp_version=f["snmp_version"], source="manual",
        )
        _apply_credentials(device, f)
        db.session.add(device)
        db.session.flush()
        details = {"site_id": device.site_id, "hostname": device.hostname, "mgmt_ip": device.mgmt_ip,
                   "vendor": device.vendor, "snmp_version": device.snmp_version}
        if _credentials_touched(f):
            details["credentials_updated"] = True
        audit_log.record("create", "Device", device.id, device.hostname, details)
        db.session.commit()
        return redirect(url_for("devices.device_detail", device_id=device.id))
    return render_template("device_form.html", device=None,
                            sites=Site.query.filter(Site.site_type.in_(["site", "minor"])).all(),
                            vendors=VENDORS, snmp_versions=SNMP_VERSIONS,
                            preselected_site_id=request.args.get("site_id", type=int))


@devices_bp.route("/import")
@admin_required
def import_devices():
    return render_template("device_import.html", vendors=VENDORS)


@devices_bp.route("/import/template")
@admin_required
def import_devices_template():
    csv_text = "Site,Hostname,Mgmt IP,Vendor\nExample HQ,core-rtr-01,10.0.0.1,ios-xe\n"
    return Response(csv_text, mimetype="text/csv",
                     headers={"Content-Disposition": "attachment; filename=sitewatch-devices-template.csv"})


@devices_bp.route("/import/preview", methods=["POST"])
@admin_required
def import_devices_preview():
    file = request.files.get("csv_file")
    if not file or file.filename == "":
        flash("Choose a CSV file.")
        return redirect(url_for("devices.import_devices"))
    try:
        rows = parse_csv(file, _DEVICE_CSV_HEADER_ALIASES)
    except CsvImportError as e:
        flash(str(e))
        return redirect(url_for("devices.import_devices"))

    sites_by_name = {s.name.strip().lower(): s for s in Site.query.all()}
    existing_hostnames = {d.hostname.strip().lower() for d in Device.query.all()}
    existing_ips = {d.mgmt_ip.strip() for d in Device.query.all()}
    seen_hostnames = set()
    seen_ips = set()

    preview_rows = []
    for i, row in enumerate(rows, start=1):
        errors = []

        site_name = row["site"]
        site = sites_by_name.get(site_name.strip().lower()) if site_name else None
        if not site_name:
            errors.append("Site is required.")
        elif site is None:
            errors.append(f"Site '{site_name}' not found.")
        elif site.site_type == "passthrough":
            errors.append(f"'{site_name}' is a passthrough site — can't have devices.")

        hostname = row["hostname"]
        hostname_key = hostname.strip().lower()
        if not hostname:
            errors.append("Hostname is required.")
        elif hostname_key in existing_hostnames:
            errors.append(f"Hostname '{hostname}' already exists.")
        elif hostname_key in seen_hostnames:
            errors.append(f"Hostname '{hostname}' is duplicated earlier in this file.")

        mgmt_ip = row["mgmt_ip"]
        if not mgmt_ip:
            errors.append("Mgmt IP is required.")
        else:
            try:
                ipaddress.ip_address(mgmt_ip)
                if mgmt_ip in existing_ips:
                    errors.append(f"Mgmt IP '{mgmt_ip}' already exists.")
                elif mgmt_ip in seen_ips:
                    errors.append(f"Mgmt IP '{mgmt_ip}' is duplicated earlier in this file.")
            except ValueError:
                errors.append(f"'{mgmt_ip}' isn't a valid IP address.")

        vendor = row["vendor"].strip().lower()
        if vendor not in VENDORS:
            errors.append(f"Vendor must be one of: {', '.join(VENDORS)}.")

        if hostname_key:
            seen_hostnames.add(hostname_key)
        if mgmt_ip:
            seen_ips.add(mgmt_ip)

        preview_rows.append({
            "row_num": i, "site": site_name, "site_id": site.id if site else None,
            "hostname": hostname, "mgmt_ip": mgmt_ip, "vendor": vendor, "errors": errors,
        })

    valid_count = sum(1 for r in preview_rows if not r["errors"])
    return render_template("device_import_preview.html", rows=preview_rows,
                            valid_count=valid_count, total_count=len(preview_rows))


@devices_bp.route("/import/confirm", methods=["POST"])
@admin_required
def import_devices_confirm():
    site_ids = request.form.getlist("row_site_id")
    hostnames = request.form.getlist("row_hostname")
    mgmt_ips = request.form.getlist("row_mgmt_ip")
    vendors = request.form.getlist("row_vendor")
    if not hostnames:
        flash("Nothing to import.")
        return redirect(url_for("devices.import_devices"))
    created = []
    for site_id, hostname, mgmt_ip, vendor in zip(site_ids, hostnames, mgmt_ips, vendors):
        db.session.add(Device(
            site_id=int(site_id), hostname=hostname, mgmt_ip=mgmt_ip,
            vendor=vendor, snmp_version="v2c", source="manual",
        ))
        created.append({"hostname": hostname, "site_id": int(site_id)})
    audit_log.record("import", "Device", None, f"CSV import: {len(hostnames)} device(s)",
                      {"count": len(hostnames), "created": created})
    db.session.commit()
    flash(f"Imported {len(hostnames)} device(s). Add SNMP credentials on each device's Edit page before polling.")
    return redirect(url_for("devices.list_devices"))


@devices_bp.route("/<int:device_id>")
@login_required
def device_detail(device_id):
    device = Device.query.get_or_404(device_id)
    iface_ids = [i.id for i in device.interfaces]
    circuits_by_interface_id = {}
    if iface_ids:
        for c in Circuit.query.filter(db.or_(Circuit.interface_a_id.in_(iface_ids),
                                               Circuit.interface_b_id.in_(iface_ids))).all():
            if c.interface_a_id in iface_ids:
                circuits_by_interface_id[c.interface_a_id] = c
            if c.interface_b_id in iface_ids:
                circuits_by_interface_id[c.interface_b_id] = c

    # circuits_by_interface_id's values are always leaf circuits (a bundle
    # never has interface_a/b set), so no bundle-member expansion needed
    # here the way sites.py/circuits.py's history queries need — every id
    # here already has its own CircuitStatusHistory rows directly.
    history_circuit_ids = {c.id for c in circuits_by_interface_id.values()}
    circuit_history = (
        CircuitStatusHistory.query.filter(CircuitStatusHistory.circuit_id.in_(history_circuit_ids))
        .order_by(CircuitStatusHistory.started_at.desc()).all()
        if history_circuit_ids else []
    )

    return render_template("device_detail.html", device=device,
                            circuits_by_interface_id=circuits_by_interface_id,
                            circuit_history=circuit_history)


@devices_bp.route("/<int:device_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_device(device_id):
    device = Device.query.get_or_404(device_id)
    if request.method == "POST":
        f = request.form
        site = Site.query.get_or_404(int(f["site_id"]))
        if site.site_type == "passthrough":
            flash("Passthrough sites can't have devices assigned.")
            return redirect(url_for("devices.edit_device", device_id=device_id))
        before = {"site_id": device.site_id, "hostname": device.hostname, "mgmt_ip": device.mgmt_ip,
                  "vendor": device.vendor, "snmp_version": device.snmp_version}
        device.site_id = site.id
        device.hostname = f["hostname"]
        device.mgmt_ip = f["mgmt_ip"]
        device.vendor = f["vendor"]
        device.snmp_version = f["snmp_version"]
        _apply_credentials(device, f)
        after = {"site_id": device.site_id, "hostname": device.hostname, "mgmt_ip": device.mgmt_ip,
                 "vendor": device.vendor, "snmp_version": device.snmp_version}
        diff = audit_log.diff_fields(before, after)
        if _credentials_touched(f):
            diff["credentials_updated"] = True
        if diff:
            audit_log.record("update", "Device", device.id, device.hostname, diff)
        db.session.commit()
        return redirect(url_for("devices.device_detail", device_id=device.id))
    return render_template("device_form.html", device=device,
                            sites=Site.query.filter(Site.site_type.in_(["site", "minor"])).all(),
                            vendors=VENDORS, snmp_versions=SNMP_VERSIONS)


@devices_bp.route("/<int:device_id>/delete", methods=["POST"])
@admin_required
def delete_device(device_id):
    device = Device.query.get_or_404(device_id)
    iface_ids = [i.id for i in device.interfaces]
    in_use = Circuit.query.filter(
        db.or_(Circuit.interface_a_id.in_(iface_ids), Circuit.interface_b_id.in_(iface_ids))
    ).first() if iface_ids else None
    if in_use:
        flash(f"In use by circuit '{in_use.name}' — delete that first.")
        return redirect(url_for("devices.device_detail", device_id=device_id))
    hostname = device.hostname
    db.session.delete(device)  # interfaces cascade-delete automatically
    audit_log.record("delete", "Device", device_id, hostname)
    db.session.commit()
    return redirect(url_for("devices.list_devices"))


@devices_bp.route("/<int:device_id>/walk", methods=["POST"])
@admin_required
def walk_device(device_id):
    device = Device.query.get_or_404(device_id)
    hostname = device.hostname
    wait = cooldown.check_and_start(device_id)
    if wait:
        return jsonify({"error": f"Wait {wait}s before hitting {hostname} again."}), 429
    job_id = job_log.start_job(f"Walking {hostname}")

    def work():
        d = Device.query.get_or_404(device_id)
        perform_walk(d)
        db.session.commit()

    job_log.run_in_background(job_id, work, current_app._get_current_object())
    return jsonify({"job_id": job_id, "label": f"Walking {hostname}",
                     "redirect": url_for("devices.device_detail", device_id=device_id)})


@devices_bp.route("/<int:device_id>/repoll", methods=["POST"])
@admin_required
def repoll_device(device_id):
    device = Device.query.get_or_404(device_id)
    hostname = device.hostname
    wait = cooldown.check_and_start(device_id)
    if wait:
        return jsonify({"error": f"Wait {wait}s before hitting {hostname} again."}), 429
    job_id = job_log.start_job(f"Repolling {hostname}")

    def work():
        d = Device.query.get_or_404(device_id)
        poll_device_now(d)
        if not d.reachable and not _has_snmp_credentials(d):
            job_log.log_line(job_id, "NOTE: no SNMP credentials set — add them on the Edit page.")

    job_log.run_in_background(job_id, work, current_app._get_current_object())
    return jsonify({"job_id": job_id, "label": f"Repolling {hostname}",
                     "redirect": url_for("devices.device_detail", device_id=device_id)})


def _has_snmp_credentials(device):
    if device.snmp_version in ("v1", "v2c"):
        return bool(device.snmp_community)
    return bool(device.snmpv3_username)
