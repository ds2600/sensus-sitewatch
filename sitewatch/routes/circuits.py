from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required

from sitewatch.extensions import db
from sitewatch.models import Circuit, CircuitRole, CircuitWaypoint, Interface, Site, AlertMute, Setting

circuits_bp = Blueprint("circuits", __name__, url_prefix="/circuits")


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


@circuits_bp.route("/")
@login_required
def list_circuits():
    return render_template("circuits.html", circuits=Circuit.query.filter_by(parent_circuit_id=None).all())


@circuits_bp.route("/add", methods=["GET", "POST"])
@login_required
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
        if f.get("capacity_override"):
            circuit.capacity_bps_override = int(f["capacity_override"])
        _set_waypoints(circuit, f)
        db.session.add(circuit)
        db.session.commit()
        return redirect(url_for("circuits.circuit_detail", circuit_id=circuit.id))

    unmapped_interfaces = (Interface.query
                            .outerjoin(Circuit, (Circuit.interface_a_id == Interface.id) |
                                       (Circuit.interface_b_id == Interface.id))
                            .filter(Circuit.id.is_(None)).all())

    # Grouped by device so the form can offer "pick a device, then pick one
    # of its interfaces" instead of one flat list of every unmapped
    # interface on every device.
    devices_by_id = {}
    interfaces_by_device = {}
    for i in unmapped_interfaces:
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

    return render_template(
        "circuit_form.html",
        devices_for_form=devices_for_form, interfaces_by_device=interfaces_by_device,
        **_form_options(),
    )


@circuits_bp.route("/<int:circuit_id>")
@login_required
def circuit_detail(circuit_id):
    circuit = Circuit.query.get_or_404(circuit_id)
    is_muted = AlertMute.is_muted(circuit_id)
    return render_template("circuit_detail.html", circuit=circuit, is_muted=is_muted)


@circuits_bp.route("/<int:circuit_id>/edit", methods=["GET", "POST"])
@login_required
def edit_circuit(circuit_id):
    """Name, role, parent, and capacity are editable. Endpoints (which
    interfaces this circuit connects) are not — delete and recreate if
    those need to change, so history isn't attached to a circuit that's
    quietly become a different link."""
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
        _set_waypoints(circuit, f)
        db.session.commit()
        return redirect(url_for("circuits.circuit_detail", circuit_id=circuit.id))

    options = _form_options()
    options["bundles_for_form"] = [b for b in options["bundles_for_form"] if b["id"] != circuit.id]
    existing_waypoints = [{"id": w.site_id, "label": w.site.name} for w in circuit.waypoints]
    return render_template("circuit_form.html", circuit=circuit, existing_waypoints=existing_waypoints, **options)


@circuits_bp.route("/<int:circuit_id>/delete", methods=["POST"])
@login_required
def delete_circuit(circuit_id):
    circuit = Circuit.query.get_or_404(circuit_id)
    if circuit.children:
        flash(f"Has {len(circuit.children)} member circuit(s) — reassign or delete those first.")
        return redirect(url_for("circuits.circuit_detail", circuit_id=circuit_id))
    db.session.delete(circuit)
    db.session.commit()
    return redirect(url_for("circuits.list_circuits"))


@circuits_bp.route("/<int:circuit_id>/mute", methods=["POST"])
@login_required
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
@login_required
def unmute_circuit(circuit_id):
    AlertMute.query.filter_by(circuit_id=circuit_id).delete()
    db.session.commit()
    return redirect(url_for("circuits.circuit_detail", circuit_id=circuit_id))
