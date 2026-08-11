from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required

from sitewatch.extensions import db
from sitewatch.models import Circuit, CircuitRole, Interface, AlertMute, Setting

circuits_bp = Blueprint("circuits", __name__, url_prefix="/circuits")


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
        "circuit_form.html", roles=CircuitRole.query.all(),
        devices_for_form=devices_for_form, interfaces_by_device=interfaces_by_device,
        bundles=Circuit.query.filter_by(interface_a_id=None, interface_b_id=None).all(),
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
        db.session.commit()
        return redirect(url_for("circuits.circuit_detail", circuit_id=circuit.id))

    bundles = [b for b in Circuit.query.filter_by(interface_a_id=None, interface_b_id=None).all()
               if b.id != circuit.id]
    return render_template("circuit_form.html", circuit=circuit, roles=CircuitRole.query.all(),
                            bundles=bundles)


@circuits_bp.route("/<int:circuit_id>/delete", methods=["POST"])
@login_required
def delete_circuit(circuit_id):
    circuit = Circuit.query.get_or_404(circuit_id)
    if circuit.children:
        flash(f"Can't delete '{circuit.name}' — it still has {len(circuit.children)} member circuit(s). Delete or reassign those first.")
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
