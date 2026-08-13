from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app, Response
from flask_login import login_required

from sitewatch.auth import admin_required
from sitewatch.extensions import db
from sitewatch.models import (
    Circuit, CircuitRole, CircuitWaypoint, Interface, Device, Site, AlertMute, Setting, CircuitStatusHistory,
)
from sitewatch.poller import poll_device_now
from sitewatch import job_log, cooldown
from sitewatch.csv_import import parse_csv, CsvImportError

circuits_bp = Blueprint("circuits", __name__, url_prefix="/circuits")

_CIRCUIT_CSV_HEADER_ALIASES = {
    "name": ["name", "circuit name"],
    "role": ["role"],
    "device_a": ["device a", "device a hostname", "hostname a"],
    "interface_a": ["interface a"],
    "device_b": ["device b", "device b hostname", "hostname b"],
    "interface_b": ["interface b"],
}
_CIRCUIT_CSV_OPTIONAL_ALIASES = {
    "parent": ["parent", "parent circuit", "bundle"],
}


def _interface_label(interface):
    """Same label the interface picker shows on circuit_form.html — the
    thing a user copies into a CSV's Interface A/B column, so matching
    against it (case-insensitively) is what makes the two consistent."""
    return interface.if_descr or f"ifIndex {interface.if_index}"


def _form_options():
    """Shared lookup data for the searchable role/bundle/waypoint pickers
    on circuit_form.html — each a flat {id, label} list the page's JS turns
    into a label->id map for its search-input + datalist combo."""
    return {
        "roles_for_form": [{"id": r.id, "label": f"{r.name} ({r.tier})"} for r in CircuitRole.query.all()],
        "bundles_for_form": [{"id": b.id, "label": b.name}
                              for b in Circuit.query.filter_by(interface_a_id=None, interface_b_id=None).all()],
        "sites_for_form": [{"id": s.id, "label": s.name + (" (passthrough)" if s.site_type == "passthrough" else "")}
                            for s in Site.query.all()],
    }


def _set_waypoints(circuit, form):
    ids = [int(x) for x in form.get("waypoint_site_ids", "").split(",") if x]
    if circuit.id is not None:
        # Explicit bulk delete first, not just reassigning circuit.waypoints
        # to a new list — the ORM's delete-orphan cascade doesn't guarantee
        # the old rows' DELETEs are flushed before the new rows' INSERTs
        # within the same flush, which trips the (circuit_id, position)
        # unique constraint when a position is reused (nearly always, since
        # positions are just 0..N-1 every time).
        CircuitWaypoint.query.filter_by(circuit_id=circuit.id).delete()
    circuit.waypoints = [CircuitWaypoint(site_id=site_id, position=i) for i, site_id in enumerate(ids)]


def _endpoint_picker_data(exclude_circuit_id=None):
    """Devices/interfaces available for the searchable pickers — leaf
    endpoints (interface_a/b) on add, and a bundle's own optional LAG
    interfaces (lag_interface_a/b) on both add and edit. An interface
    counts as available if no OTHER circuit already uses it in any of
    those four roles; exclude_circuit_id lets editing a circuit keep its
    own current picks visible instead of hiding them from their own form."""
    conditions = db.or_(
        Circuit.interface_a_id == Interface.id, Circuit.interface_b_id == Interface.id,
        Circuit.lag_interface_a_id == Interface.id, Circuit.lag_interface_b_id == Interface.id,
    )
    query = Interface.query.outerjoin(Circuit, conditions)
    if exclude_circuit_id is not None:
        query = query.filter(db.or_(Circuit.id.is_(None), Circuit.id == exclude_circuit_id))
    else:
        query = query.filter(Circuit.id.is_(None))
    available = query.all()

    # Grouped by device so the form can offer "pick a device, then pick one
    # of its interfaces" instead of one flat list of every available
    # interface on every device.
    devices_by_id = {}
    interfaces_by_device = {}
    for i in available:
        devices_by_id[i.device_id] = i.device
        label = i.if_descr or f"ifIndex {i.if_index}"
        if i.if_alias:
            label += f" — {i.if_alias}"
        interfaces_by_device.setdefault(str(i.device_id), []).append({"id": i.id, "label": label})
    for ifaces in interfaces_by_device.values():
        ifaces.sort(key=lambda x: x["label"])
    devices_for_form = sorted(
        ({"id": d.id, "label": f"{d.hostname} ({d.site.name})"} for d in devices_by_id.values()),
        key=lambda x: x["label"],
    )
    return devices_for_form, interfaces_by_device


def _set_lag_interfaces(circuit, form):
    circuit.lag_interface_a_id = int(form["lag_interface_a_id"]) if form.get("lag_interface_a_id") else None
    circuit.lag_interface_b_id = int(form["lag_interface_b_id"]) if form.get("lag_interface_b_id") else None


@circuits_bp.route("/")
@login_required
def list_circuits():
    circuits = Circuit.query.filter_by(parent_circuit_id=None).all()
    muted_ids = {c.id for c in circuits if AlertMute.is_muted(c.id)}
    unreachable_count = Device.query.filter_by(reachable=False).count()
    return render_template("circuits.html", circuits=circuits, muted_ids=muted_ids,
                            unreachable_count=unreachable_count)


@circuits_bp.route("/add", methods=["GET", "POST"])
@admin_required
def add_circuit():
    if request.method == "POST":
        f = request.form
        is_bundle = f.get("is_bundle") == "on"
        circuit = Circuit(
            name=f["name"],
            role_id=int(f["role_id"]),
            parent_circuit_id=int(f["parent_circuit_id"]) if f.get("parent_circuit_id") else None,
        )
        if not is_bundle:
            circuit.interface_a_id = int(f["interface_a_id"])
            circuit.interface_b_id = int(f["interface_b_id"])
        else:
            _set_lag_interfaces(circuit, f)
        if f.get("capacity_override"):
            circuit.capacity_bps_override = int(f["capacity_override"])
        _set_waypoints(circuit, f)
        db.session.add(circuit)
        db.session.commit()
        return redirect(url_for("circuits.circuit_detail", circuit_id=circuit.id))

    devices_for_form, interfaces_by_device = _endpoint_picker_data()
    interface_a_id = request.args.get("interface_a_id", type=int)
    preselected_interface_a = Interface.query.get(interface_a_id) if interface_a_id else None

    # "Duplicate" (circuit_detail.html): prefills name/role/parent/capacity/
    # waypoints from an existing circuit as a starting point — deliberately
    # NOT its interfaces, since two circuits can't actually share an
    # endpoint. The DEVICES do mirror over though (interface pickers land
    # already scoped to the source's devices, interface itself left for a
    # fresh pick) — saves re-searching for the same device on both ends
    # when the new circuit is, as usual, another link off the same gear.
    duplicate_id = request.args.get("duplicate_id", type=int)
    duplicate_source = Circuit.query.get(duplicate_id) if duplicate_id else None
    existing_waypoints = ([{"id": w.site_id, "label": w.site.name} for w in duplicate_source.waypoints]
                           if duplicate_source else [])

    prefill_device_a = preselected_interface_a.device if preselected_interface_a else None
    prefill_device_b = None
    if duplicate_source and not duplicate_source.is_bundle:
        if not prefill_device_a and duplicate_source.interface_a:
            prefill_device_a = duplicate_source.interface_a.device
        if duplicate_source.interface_b:
            prefill_device_b = duplicate_source.interface_b.device
    prefill_lag_device_a = prefill_lag_device_b = None
    if duplicate_source and duplicate_source.is_bundle:
        if duplicate_source.lag_interface_a:
            prefill_lag_device_a = duplicate_source.lag_interface_a.device
        if duplicate_source.lag_interface_b:
            prefill_lag_device_b = duplicate_source.lag_interface_b.device

    return render_template(
        "circuit_form.html",
        devices_for_form=devices_for_form, interfaces_by_device=interfaces_by_device,
        preselected_parent_id=(request.args.get("parent_id", type=int)
                                or (duplicate_source.parent_circuit_id if duplicate_source else None)),
        preselected_interface_a=preselected_interface_a,
        duplicate_source=duplicate_source, existing_waypoints=existing_waypoints,
        prefill_device_a=prefill_device_a, prefill_device_b=prefill_device_b,
        prefill_lag_device_a=prefill_lag_device_a, prefill_lag_device_b=prefill_lag_device_b,
        **_form_options(),
    )


@circuits_bp.route("/import")
@admin_required
def import_circuits():
    return render_template("circuit_import.html")


@circuits_bp.route("/import/template")
@admin_required
def import_circuits_template():
    csv_text = ("Name,Role,Device A,Interface A,Device B,Interface B,Parent\n"
                "Site A - Site B Link,core,core-rtr-01,GigabitEthernet0/0/1,core-rtr-02,GigabitEthernet0/0/1,\n")
    return Response(csv_text, mimetype="text/csv",
                     headers={"Content-Disposition": "attachment; filename=sitewatch-circuits-template.csv"})


@circuits_bp.route("/import/preview", methods=["POST"])
@admin_required
def import_circuits_preview():
    file = request.files.get("csv_file")
    if not file or file.filename == "":
        flash("Choose a CSV file.")
        return redirect(url_for("circuits.import_circuits"))
    try:
        rows = parse_csv(file, _CIRCUIT_CSV_HEADER_ALIASES, _CIRCUIT_CSV_OPTIONAL_ALIASES)
    except CsvImportError as e:
        flash(str(e))
        return redirect(url_for("circuits.import_circuits"))

    roles_by_name = {r.name.strip().lower(): r for r in CircuitRole.query.all()}
    devices_by_hostname = {d.hostname.strip().lower(): d for d in Device.query.all()}
    bundles_by_name = {c.name.strip().lower(): c
                        for c in Circuit.query.filter_by(interface_a_id=None, interface_b_id=None).all()}
    used_interface_ids = set()
    for c in Circuit.query.all():
        for fid in (c.interface_a_id, c.interface_b_id, c.lag_interface_a_id, c.lag_interface_b_id):
            if fid:
                used_interface_ids.add(fid)
    seen_interface_ids = set()

    preview_rows = []
    for i, row in enumerate(rows, start=1):
        errors = []
        notes = []

        name = row["name"]
        if not name:
            errors.append("Name is required.")

        role = roles_by_name.get(row["role"].strip().lower()) if row["role"] else None
        if not row["role"]:
            errors.append("Role is required.")
        elif role is None:
            errors.append(f"Role '{row['role']}' not found.")

        def _resolve_endpoint(device_key, interface_key, label):
            device_name = row[device_key]
            interface_name = row[interface_key]
            if not device_name:
                errors.append(f"Device {label} is required.")
                return None, None
            device = devices_by_hostname.get(device_name.strip().lower())
            if device is None:
                errors.append(f"Device '{device_name}' not found.")
                return device_name, None
            if not interface_name:
                errors.append(f"Interface {label} is required.")
                return device_name, None
            interfaces_by_label = {_interface_label(iface).strip().lower(): iface for iface in device.interfaces}
            interface = interfaces_by_label.get(interface_name.strip().lower())
            if interface is None:
                errors.append(f"Interface '{interface_name}' not found on device '{device_name}'.")
                return device_name, None
            if interface.id in used_interface_ids:
                errors.append(f"Interface '{interface_name}' on '{device_name}' is already used by another circuit.")
            elif interface.id in seen_interface_ids:
                errors.append(f"Interface '{interface_name}' on '{device_name}' is duplicated earlier in this file.")
            else:
                seen_interface_ids.add(interface.id)
            return device_name, interface

        device_a_name, interface_a = _resolve_endpoint("device_a", "interface_a", "A")
        device_b_name, interface_b = _resolve_endpoint("device_b", "interface_b", "B")

        parent_name = row["parent"]
        parent_id = None
        parent_new_name = ""
        if parent_name:
            bundle = bundles_by_name.get(parent_name.strip().lower())
            if bundle is not None:
                parent_id = bundle.id
            else:
                parent_new_name = parent_name
                notes.append(f"Parent '{parent_name}' not found — will be created.")

        preview_rows.append({
            "row_num": i, "name": name, "role": row["role"], "role_id": role.id if role else None,
            "device_a": device_a_name, "interface_a": row["interface_a"],
            "interface_a_id": interface_a.id if interface_a else None,
            "device_b": device_b_name, "interface_b": row["interface_b"],
            "interface_b_id": interface_b.id if interface_b else None,
            "parent": parent_name, "parent_id": parent_id, "parent_new_name": parent_new_name,
            "errors": errors, "notes": notes,
        })

    valid_count = sum(1 for r in preview_rows if not r["errors"])
    return render_template("circuit_import_preview.html", rows=preview_rows,
                            valid_count=valid_count, total_count=len(preview_rows))


@circuits_bp.route("/import/confirm", methods=["POST"])
@admin_required
def import_circuits_confirm():
    names = request.form.getlist("row_name")
    role_ids = request.form.getlist("row_role_id")
    interface_a_ids = request.form.getlist("row_interface_a_id")
    interface_b_ids = request.form.getlist("row_interface_b_id")
    parent_ids = request.form.getlist("row_parent_id")
    parent_new_names = request.form.getlist("row_parent_new_name")
    if not names:
        flash("Nothing to import.")
        return redirect(url_for("circuits.import_circuits"))

    created_bundles = {}  # lowercased name -> Circuit, so rows sharing a new parent name reuse the same bundle
    count = 0
    for name, role_id, ia, ib, pid, pnew in zip(
        names, role_ids, interface_a_ids, interface_b_ids, parent_ids, parent_new_names
    ):
        parent_circuit_id = int(pid) if pid else None
        if pnew:
            key = pnew.strip().lower()
            bundle = created_bundles.get(key)
            if bundle is None:
                bundle = Circuit(name=pnew, role_id=CircuitRole.default_role().id)
                db.session.add(bundle)
                db.session.flush()
                created_bundles[key] = bundle
            parent_circuit_id = bundle.id
        db.session.add(Circuit(
            name=name, role_id=int(role_id),
            interface_a_id=int(ia), interface_b_id=int(ib),
            parent_circuit_id=parent_circuit_id,
        ))
        count += 1
    db.session.commit()
    created_bundle_note = f" ({len(created_bundles)} new parent bundle(s) created)" if created_bundles else ""
    flash(f"Imported {count} circuit(s){created_bundle_note}.")
    return redirect(url_for("circuits.list_circuits"))


@circuits_bp.route("/<int:circuit_id>")
@login_required
def circuit_detail(circuit_id):
    circuit = Circuit.query.get_or_404(circuit_id)
    is_muted = AlertMute.is_muted(circuit_id)
    open_incident = (CircuitStatusHistory.query
                      .filter_by(circuit_id=circuit_id, cleared_at=None)
                      .order_by(CircuitStatusHistory.started_at.desc()).first())
    # Only unparented circuits — this is for attaching a standalone circuit
    # someone already built, not for stealing one away from another bundle
    # (Edit's own Parent bundle field already covers that, deliberately).
    attachable_circuits = (
        [{"id": c.id, "label": c.name}
         for c in Circuit.query.filter_by(parent_circuit_id=None).filter(Circuit.id != circuit.id).all()]
        if circuit.is_bundle else []
    )
    return render_template("circuit_detail.html", circuit=circuit, is_muted=is_muted,
                            open_incident=open_incident, attachable_circuits=attachable_circuits)


@circuits_bp.route("/incidents")
@login_required
def list_incidents():
    """All down-incidents, open and closed — the dashboard's own lists only
    ever show what's currently down plus the last 20 cleared, so this is
    the uncapped history view (and the target of "Past incidents" links)."""
    incidents = (CircuitStatusHistory.query.join(Circuit)
                 .order_by(CircuitStatusHistory.started_at.desc()).all())
    return render_template("circuit_incidents.html", incidents=incidents)


@circuits_bp.route("/incidents/<int:history_id>/ticket", methods=["POST"])
@login_required
def set_incident_ticket(history_id):
    history = CircuitStatusHistory.query.get_or_404(history_id)
    history.external_ticket = request.form.get("external_ticket", "").strip() or None
    db.session.commit()
    next_url = request.form.get("next")
    if not next_url or not next_url.startswith("/"):
        next_url = url_for("circuits.circuit_detail", circuit_id=history.circuit_id)
    return redirect(next_url)


@circuits_bp.route("/<int:circuit_id>/repoll", methods=["POST"])
@admin_required
def repoll_circuit(circuit_id):
    """The circuit page's Repoll button — only shown there while the
    circuit is unreachable (see circuit_detail.html). Repolls whichever
    real device(s) back it: a leaf's interface_a/b devices, or a bundle's
    own LAG interface devices — a bundle only ever shows "unreachable"
    via a hard LAG failure (see poller.py's recompute_bundle_state), never
    from pure member rollup, so the LAG devices are the complete story."""
    circuit = Circuit.query.get_or_404(circuit_id)
    ifaces = ([circuit.lag_interface_a, circuit.lag_interface_b] if circuit.is_bundle
              else [circuit.interface_a, circuit.interface_b])
    devices = list({i.device_id: i.device for i in ifaces if i}.values())
    if not devices:
        return jsonify({"error": "No devices to repoll."}), 400
    # Peek every device before committing to any — starting device A's
    # cooldown only to then reject the whole request over device B would
    # falsely cost device A a wasted 60s wait for a repoll that never ran.
    for d in devices:
        wait = cooldown.remaining(d.id)
        if wait:
            return jsonify({"error": f"Wait {wait}s before hitting {d.hostname} again."}), 429
    for d in devices:
        cooldown.start(d.id)

    job_id = job_log.start_job(f"Repolling {circuit.name}")

    def work():
        for d in devices:
            poll_device_now(Device.query.get(d.id))

    job_log.run_in_background(job_id, work, current_app._get_current_object())
    return jsonify({"job_id": job_id, "label": f"Repolling {circuit.name}",
                     "redirect": url_for("circuits.circuit_detail", circuit_id=circuit_id)})


@circuits_bp.route("/<int:circuit_id>/attach-member", methods=["POST"])
@admin_required
def attach_member(circuit_id):
    bundle = Circuit.query.get_or_404(circuit_id)
    member_id = request.form.get("member_circuit_id", type=int)
    member = Circuit.query.get(member_id) if member_id else None
    if not member or member.id == bundle.id or member.parent_circuit_id is not None:
        flash("Pick an existing, unattached circuit.")
        return redirect(url_for("circuits.circuit_detail", circuit_id=circuit_id))
    member.parent_circuit_id = bundle.id
    db.session.commit()
    return redirect(url_for("circuits.circuit_detail", circuit_id=circuit_id))


@circuits_bp.route("/<int:circuit_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_circuit(circuit_id):
    """Name, role, parent, and capacity are editable. Leaf endpoints
    (interface_a/b) are not — delete and recreate if those need to change,
    so history isn't attached to a circuit that's quietly become a
    different link. A bundle's own optional LAG interfaces are editable
    here, unlike leaf endpoints — they're monitoring enrichment, not the
    circuit's identity."""
    circuit = Circuit.query.get_or_404(circuit_id)
    if request.method == "POST":
        f = request.form
        circuit.name = f["name"]
        circuit.role_id = int(f["role_id"])
        new_parent = int(f["parent_circuit_id"]) if f.get("parent_circuit_id") else None
        if new_parent == circuit.id:
            flash("A circuit can't be its own parent.")
            return redirect(url_for("circuits.edit_circuit", circuit_id=circuit_id))
        circuit.parent_circuit_id = new_parent
        circuit.capacity_bps_override = int(f["capacity_override"]) if f.get("capacity_override") else None
        if circuit.is_bundle:
            _set_lag_interfaces(circuit, f)
        _set_waypoints(circuit, f)
        db.session.commit()
        return redirect(url_for("circuits.circuit_detail", circuit_id=circuit.id))

    options = _form_options()
    options["bundles_for_form"] = [b for b in options["bundles_for_form"] if b["id"] != circuit.id]
    existing_waypoints = [{"id": w.site_id, "label": w.site.name} for w in circuit.waypoints]
    devices_for_form, interfaces_by_device = _endpoint_picker_data(exclude_circuit_id=circuit.id)
    return render_template(
        "circuit_form.html", circuit=circuit, existing_waypoints=existing_waypoints,
        devices_for_form=devices_for_form, interfaces_by_device=interfaces_by_device,
        **options,
    )


@circuits_bp.route("/<int:circuit_id>/delete", methods=["POST"])
@admin_required
def delete_circuit(circuit_id):
    circuit = Circuit.query.get_or_404(circuit_id)
    if circuit.children:
        flash(f"Has {len(circuit.children)} member circuit(s) — reassign or delete those first.")
        return redirect(url_for("circuits.circuit_detail", circuit_id=circuit_id))
    db.session.delete(circuit)
    db.session.commit()
    return redirect(url_for("circuits.list_circuits"))


@circuits_bp.route("/<int:circuit_id>/mute", methods=["POST"])
@admin_required
def mute_circuit(circuit_id):
    minutes = min(int(request.form["minutes"]), Setting.get_int("mute_max_minutes"))
    existing = AlertMute.query.filter_by(circuit_id=circuit_id).first()
    until = datetime.utcnow() + timedelta(minutes=minutes)
    if existing:
        existing.muted_until = until
    else:
        db.session.add(AlertMute(circuit_id=circuit_id, muted_until=until))
    db.session.commit()
    return redirect(url_for("circuits.circuit_detail", circuit_id=circuit_id))


@circuits_bp.route("/<int:circuit_id>/unmute", methods=["POST"])
@admin_required
def unmute_circuit(circuit_id):
    AlertMute.query.filter_by(circuit_id=circuit_id).delete()
    db.session.commit()
    return redirect(url_for("circuits.circuit_detail", circuit_id=circuit_id))
