from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app, Response
from flask_login import login_required

from sitewatch.auth import admin_required
from sitewatch.extensions import db
from sitewatch.models import (
    Circuit, CircuitRole, CircuitWaypoint, Interface, Device, Site, Layer, AlertMute, Setting, CircuitStatusHistory,
)
from sitewatch.poller import poll_device_now
from sitewatch.integrations.webhook_payload import send_down_alerts
from sitewatch import job_log, cooldown, audit_log, custom_fields
from sitewatch.csv_import import parse_csv, CsvImportError
from sitewatch.utilization import circuit_utilization_history

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
        "sites_for_form": [{"id": s.id, "label": s.name + (" (passthrough)" if s.site_type == "passthrough"
                            else " (minor)" if s.site_type == "minor" else "")}
                            for s in Site.query.all()],
        "layers": Layer.query.order_by(Layer.name).all(),
        "custom_field_defs": custom_fields.definitions_for("circuit"),
    }


def _circuit_own_site_ids(circuit):
    """Site ids at this circuit's own two real ends — a waypoint duplicating
    one of these is meaningless (it's already an endpoint of the route, not
    a bend in the middle of it). Reads interface_a_id/interface_b_id via an
    independent Interface query rather than the circuit.interface_a/b
    relationship, since this also has to work for a circuit still being
    created (add_circuit calls this before the new Circuit is ever added to
    the session, when relationship lazy-loading isn't available yet — a
    plain query keyed on the raw FK id doesn't care either way). A bundle
    has no interfaces of its own; site_a/site_b there are borrowed from its
    first member with real endpoints, same as everywhere else on this
    model — empty/None for a brand new bundle with no members yet, which is
    exactly correct (nothing to conflict with)."""
    if circuit.is_bundle:
        a, b = circuit.site_a, circuit.site_b
        return {s.id for s in (a, b) if s}
    ids = set()
    for iface_id in (circuit.interface_a_id, circuit.interface_b_id):
        if iface_id:
            iface = Interface.query.get(iface_id)
            if iface:
                ids.add(iface.device.site_id)
    return ids


def _set_waypoints(circuit, form):
    """Returns (old_site_ids, new_site_ids), ordered — callers fold this
    into the SAME audit_log.record() call as the rest of the circuit's own
    field diff (only when they actually differ) rather than logging a
    separate entry, since every save deletes+recreates the whole waypoint
    set below regardless of whether anything actually changed."""
    ids = [int(x) for x in form.get("waypoint_site_ids", "").split(",") if x]
    own_site_ids = _circuit_own_site_ids(circuit)
    filtered_ids = [site_id for site_id in ids if site_id not in own_site_ids]
    if len(filtered_ids) != len(ids):
        flash("A circuit's own A/Z site can't also be one of its waypoints — dropped from the route.")
    old_site_ids = [w.site_id for w in circuit.waypoints] if circuit.id is not None else []
    if circuit.id is not None:
        # Explicit bulk delete first, not just reassigning circuit.waypoints
        # to a new list — the ORM's delete-orphan cascade doesn't guarantee
        # the old rows' DELETEs are flushed before the new rows' INSERTs
        # within the same flush, which trips the (circuit_id, position)
        # unique constraint when a position is reused (nearly always, since
        # positions are just 0..N-1 every time).
        CircuitWaypoint.query.filter_by(circuit_id=circuit.id).delete()
    circuit.waypoints = [CircuitWaypoint(site_id=site_id, position=i) for i, site_id in enumerate(filtered_ids)]
    return old_site_ids, filtered_ids


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
        if is_bundle and f.get("parent_circuit_id"):
            flash("A bundle can't itself belong to another bundle.")
            return redirect(url_for("circuits.add_circuit"))
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
        if f.get("utilization_threshold_pct"):
            circuit.utilization_threshold_pct = int(f["utilization_threshold_pct"])
        circuit.layer_id = f.get("layer_id", type=int) or None
        # circuit.id must still be None here (add()/flush() happen after) —
        # that's what tells _set_waypoints this is a brand-new circuit with
        # no existing CircuitWaypoint rows to bulk-delete first.
        _, new_wp = _set_waypoints(circuit, f)
        db.session.add(circuit)
        db.session.flush()  # need circuit.id for the audit row below
        details = {
            "role_id": circuit.role_id, "parent_circuit_id": circuit.parent_circuit_id,
            "interface_a_id": circuit.interface_a_id, "interface_b_id": circuit.interface_b_id,
            "lag_interface_a_id": circuit.lag_interface_a_id, "lag_interface_b_id": circuit.lag_interface_b_id,
            "capacity_bps_override": circuit.capacity_bps_override,
            "utilization_threshold_pct": circuit.utilization_threshold_pct,
            "layer_id": circuit.layer_id,
        }
        if new_wp:
            details["waypoints"] = new_wp
        details.update(custom_fields.set_values("circuit", circuit.id, f))
        audit_log.record("create", "Circuit", circuit.id, circuit.name, details)
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
        custom_field_values={},
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
    created = []
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
        created.append({"name": name, "role_id": int(role_id)})
        count += 1
    audit_log.record("import", "Circuit", None, f"CSV import: {count} circuit(s)", {
        "count": count, "created": created,
        "bundles_created": [b.name for b in created_bundles.values()],
    })
    db.session.commit()
    created_bundle_note = f" ({len(created_bundles)} new parent bundle(s) created)" if created_bundles else ""
    flash(f"Imported {count} circuit(s){created_bundle_note}.")
    return redirect(url_for("circuits.list_circuits"))


@circuits_bp.route("/<int:circuit_id>")
@login_required
def circuit_detail(circuit_id):
    circuit = Circuit.query.get_or_404(circuit_id)
    is_muted = AlertMute.is_muted(circuit_id)
    # CircuitStatusHistory is only ever written for LEAF circuits — a
    # bundle's own current_state changes via recompute_bundle_state
    # directly, never through poller.py's _transition, so a bundle never
    # gets its own history rows. Show its members' instead.
    history_circuit_ids = [c.id for c in circuit.children] if circuit.is_bundle else [circuit.id]
    circuit_history = (
        CircuitStatusHistory.query.filter(CircuitStatusHistory.circuit_id.in_(history_circuit_ids))
        .order_by(CircuitStatusHistory.started_at.desc()).all()
        if history_circuit_ids else []
    )
    # Only unparented circuits — this is for attaching a standalone circuit
    # someone already built, not for stealing one away from another bundle
    # (Edit's own Parent bundle field already covers that, deliberately).
    attachable_circuits = (
        [{"id": c.id, "label": c.name}
         for c in Circuit.query.filter_by(parent_circuit_id=None).filter(Circuit.id != circuit.id).all()
         if not c.is_bundle]  # a bundle can't itself belong to another bundle
        if circuit.is_bundle else []
    )
    return render_template("circuit_detail.html", circuit=circuit, is_muted=is_muted,
                            circuit_history=circuit_history, attachable_circuits=attachable_circuits,
                            custom_field_defs=custom_fields.definitions_for("circuit"),
                            custom_field_values=custom_fields.values_for("circuit", circuit.id))


@circuits_bp.route("/<int:circuit_id>/utilization-history")
@login_required
def utilization_history(circuit_id):
    circuit = Circuit.query.get_or_404(circuit_id)
    if request.args.get("window") == "7d":
        points = circuit_utilization_history(circuit, hours=24 * 7, bucket="day")
    else:
        points = circuit_utilization_history(circuit, hours=24, bucket="hour")
    return jsonify({"points": points, "threshold_pct": circuit.utilization_threshold_pct})


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
    old_ticket = history.external_ticket
    history.external_ticket = request.form.get("external_ticket", "").strip() or None
    if history.external_ticket != old_ticket:
        audit_log.record("update", "Circuit", history.circuit_id, history.circuit.name,
                          {"incident_id": history.id,
                           "external_ticket": {"old": old_ticket, "new": history.external_ticket}})
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
        # Shared across every device in this loop so a bundle's two LAG
        # devices (or a leaf's two endpoints) both going down from one
        # Repoll click send a single grouped webhook, not one apiece.
        alert_batch = []
        for d in devices:
            if job_log.cancel_requested(job_id):
                job_log.log_line(job_id, "Stopped by user.")
                break
            poll_device_now(Device.query.get(d.id), alert_batch)
        send_down_alerts(alert_batch)

    job_log.run_in_background(job_id, work, current_app._get_current_object())
    return jsonify({"job_id": job_id, "label": f"Repolling {circuit.name}",
                     "redirect": url_for("circuits.circuit_detail", circuit_id=circuit_id)})


@circuits_bp.route("/<int:circuit_id>/attach-member", methods=["POST"])
@admin_required
def attach_member(circuit_id):
    bundle = Circuit.query.get_or_404(circuit_id)
    member_id = request.form.get("member_circuit_id", type=int)
    member = Circuit.query.get(member_id) if member_id else None
    if not member or member.id == bundle.id or member.parent_circuit_id is not None or member.is_bundle:
        flash("Pick an existing, unattached, non-bundle circuit.")
        return redirect(url_for("circuits.circuit_detail", circuit_id=circuit_id))
    old_parent = member.parent_circuit_id
    member.parent_circuit_id = bundle.id
    audit_log.record("update", "Circuit", member.id, member.name,
                      {"parent_circuit_id": {"old": old_parent, "new": bundle.id}, "attached_to": bundle.name})
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
        before = {
            "name": circuit.name, "role_id": circuit.role_id, "parent_circuit_id": circuit.parent_circuit_id,
            "capacity_bps_override": circuit.capacity_bps_override,
            "utilization_threshold_pct": circuit.utilization_threshold_pct,
            "lag_interface_a_id": circuit.lag_interface_a_id, "lag_interface_b_id": circuit.lag_interface_b_id,
            "layer_id": circuit.layer_id,
        }
        circuit.name = f["name"]
        circuit.role_id = int(f["role_id"])
        new_parent = int(f["parent_circuit_id"]) if f.get("parent_circuit_id") else None
        if new_parent == circuit.id:
            flash("A circuit can't be its own parent.")
            return redirect(url_for("circuits.edit_circuit", circuit_id=circuit_id))
        if new_parent is not None and circuit.is_bundle:
            flash("A bundle can't itself belong to another bundle.")
            return redirect(url_for("circuits.edit_circuit", circuit_id=circuit_id))
        circuit.parent_circuit_id = new_parent
        circuit.capacity_bps_override = int(f["capacity_override"]) if f.get("capacity_override") else None
        circuit.utilization_threshold_pct = (
            int(f["utilization_threshold_pct"]) if f.get("utilization_threshold_pct") else None
        )
        circuit.layer_id = f.get("layer_id", type=int) or None
        if circuit.is_bundle:
            _set_lag_interfaces(circuit, f)
        old_wp, new_wp = _set_waypoints(circuit, f)
        after = {
            "name": circuit.name, "role_id": circuit.role_id, "parent_circuit_id": circuit.parent_circuit_id,
            "capacity_bps_override": circuit.capacity_bps_override,
            "utilization_threshold_pct": circuit.utilization_threshold_pct,
            "lag_interface_a_id": circuit.lag_interface_a_id, "lag_interface_b_id": circuit.lag_interface_b_id,
            "layer_id": circuit.layer_id,
        }
        diff = audit_log.diff_fields(before, after)
        if old_wp != new_wp:
            diff["waypoints"] = {"old": old_wp, "new": new_wp}
        diff.update(custom_fields.set_values("circuit", circuit.id, f))
        if diff:
            audit_log.record("update", "Circuit", circuit.id, circuit.name, diff)
        db.session.commit()
        return redirect(url_for("circuits.circuit_detail", circuit_id=circuit.id))

    options = _form_options()
    options["bundles_for_form"] = [b for b in options["bundles_for_form"] if b["id"] != circuit.id]
    existing_waypoints = [{"id": w.site_id, "label": w.site.name} for w in circuit.waypoints]
    devices_for_form, interfaces_by_device = _endpoint_picker_data(exclude_circuit_id=circuit.id)
    return render_template(
        "circuit_form.html", circuit=circuit, existing_waypoints=existing_waypoints,
        devices_for_form=devices_for_form, interfaces_by_device=interfaces_by_device,
        custom_field_values=custom_fields.values_for("circuit", circuit.id),
        **options,
    )


@circuits_bp.route("/<int:circuit_id>/delete", methods=["POST"])
@admin_required
def delete_circuit(circuit_id):
    circuit = Circuit.query.get_or_404(circuit_id)
    if circuit.children:
        flash(f"Has {len(circuit.children)} member circuit(s) — reassign or delete those first.")
        return redirect(url_for("circuits.circuit_detail", circuit_id=circuit_id))
    name = circuit.name
    custom_fields.delete_values("circuit", circuit_id)
    db.session.delete(circuit)
    audit_log.record("delete", "Circuit", circuit_id, name)
    db.session.commit()
    return redirect(url_for("circuits.list_circuits"))


@circuits_bp.route("/<int:circuit_id>/mute", methods=["POST"])
@admin_required
def mute_circuit(circuit_id):
    circuit = Circuit.query.get_or_404(circuit_id)
    minutes = min(int(request.form["minutes"]), Setting.get_int("mute_max_minutes"))
    existing = AlertMute.query.filter_by(circuit_id=circuit_id).first()
    until = datetime.utcnow() + timedelta(minutes=minutes)
    if existing:
        existing.muted_until = until
    else:
        db.session.add(AlertMute(circuit_id=circuit_id, muted_until=until))
    audit_log.record("mute", "Circuit", circuit_id, circuit.name,
                      {"muted_until": until.isoformat(), "minutes": minutes})
    db.session.commit()
    return redirect(url_for("circuits.circuit_detail", circuit_id=circuit_id))


@circuits_bp.route("/<int:circuit_id>/unmute", methods=["POST"])
@admin_required
def unmute_circuit(circuit_id):
    circuit = Circuit.query.get_or_404(circuit_id)
    AlertMute.query.filter_by(circuit_id=circuit_id).delete()
    audit_log.record("unmute", "Circuit", circuit_id, circuit.name)
    db.session.commit()
    return redirect(url_for("circuits.circuit_detail", circuit_id=circuit_id))
