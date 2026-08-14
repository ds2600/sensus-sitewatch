from sitewatch.extensions import db
from sitewatch.models import Circuit, CircuitWaypoint, AlertMute, AuditLog
from tests.factories import make_site, make_device, make_interface, make_role, make_circuit, make_bundle


def test_add_leaf_circuit(app, admin_client):
    with app.app_context():
        site_a, site_b = make_site(name="A"), make_site(name="B")
        dev_a, dev_b = make_device(site_a), make_device(site_b)
        iface_a, iface_b = make_interface(dev_a), make_interface(dev_b)
        role = make_role(name="core-role")
        db.session.commit()
        iface_a_id, iface_b_id, role_id = iface_a.id, iface_b.id, role.id

    admin_client.post("/circuits/add", data={
        "name": "New Circuit", "role_id": str(role_id),
        "interface_a_id": str(iface_a_id), "interface_b_id": str(iface_b_id),
    }, follow_redirects=True)

    with app.app_context():
        circuit = Circuit.query.filter_by(name="New Circuit").first()
        assert circuit is not None
        entry = AuditLog.query.filter_by(object_type="Circuit", action="create").first()
        assert entry is not None
        assert entry.object_id == circuit.id


# --- 0.8.4 regression: a circuit's own A/Z site can't also be its waypoint ---

def test_waypoint_matching_own_endpoint_is_dropped(app, admin_client):
    with app.app_context():
        site_a, site_b = make_site(name="A"), make_site(name="B")
        dev_a, dev_b = make_device(site_a), make_device(site_b)
        iface_a, iface_b = make_interface(dev_a), make_interface(dev_b)
        role = make_role()
        db.session.commit()
        iface_a_id, iface_b_id, role_id, site_a_id = iface_a.id, iface_b.id, role.id, site_a.id

    admin_client.post("/circuits/add", data={
        "name": "WP Circuit", "role_id": str(role_id),
        "interface_a_id": str(iface_a_id), "interface_b_id": str(iface_b_id),
        "waypoint_site_ids": str(site_a_id),  # site A is this circuit's own A end
    }, follow_redirects=True)

    with app.app_context():
        circuit = Circuit.query.filter_by(name="WP Circuit").first()
        assert circuit is not None
        assert circuit.waypoints == []  # dropped, not saved


def test_waypoint_edit_diff_recorded_in_same_audit_row_as_other_field_changes(app, admin_client):
    with app.app_context():
        site_a, site_b, waypoint_site = make_site(name="A"), make_site(name="B"), make_site(name="WP")
        dev_a, dev_b = make_device(site_a), make_device(site_b)
        iface_a, iface_b = make_interface(dev_a), make_interface(dev_b)
        circuit = make_circuit(iface_a, iface_b, name="Edit Me")
        db.session.commit()
        circuit_id, role_id, waypoint_site_id = circuit.id, circuit.role_id, waypoint_site.id

    admin_client.post(f"/circuits/{circuit_id}/edit", data={
        "name": "Edit Me Renamed", "role_id": str(role_id),
        "waypoint_site_ids": str(waypoint_site_id),
    }, follow_redirects=True)

    with app.app_context():
        entries = AuditLog.query.filter_by(object_type="Circuit", object_id=circuit_id, action="update").all()
        assert len(entries) == 1  # ONE row, not a separate one for the waypoint change
        assert "waypoints" in entries[0].details
        assert "name" in entries[0].details


# --- 0.8.4 regression: a bundle can't itself belong to another bundle ---

def test_bundle_cannot_have_a_parent_via_add(app, admin_client):
    with app.app_context():
        role = make_role()
        parent = make_bundle(role=role, name="Parent Bundle")
        db.session.commit()
        role_id, parent_id = role.id, parent.id

    admin_client.post("/circuits/add", data={
        "name": "Nested Bundle", "role_id": str(role_id),
        "is_bundle": "on", "parent_circuit_id": str(parent_id),
    }, follow_redirects=True)

    with app.app_context():
        assert Circuit.query.filter_by(name="Nested Bundle").first() is None


def test_bundle_cannot_be_reparented_via_edit(app, admin_client):
    with app.app_context():
        role = make_role()
        parent = make_bundle(role=role, name="Parent Bundle 2")
        child_bundle = make_bundle(role=role, name="Child Bundle")
        db.session.commit()
        role_id, parent_id, child_id = role.id, parent.id, child_bundle.id

    admin_client.post(f"/circuits/{child_id}/edit", data={
        "name": "Child Bundle", "role_id": str(role_id), "parent_circuit_id": str(parent_id),
    }, follow_redirects=True)

    with app.app_context():
        child = Circuit.query.get(child_id)
        assert child.parent_circuit_id is None


def test_attach_member_rejects_a_bundle(app, admin_client):
    with app.app_context():
        role = make_role()
        bundle = make_bundle(role=role, name="Target Bundle")
        other_bundle = make_bundle(role=role, name="Other Bundle")
        db.session.commit()
        bundle_id, other_bundle_id = bundle.id, other_bundle.id

    admin_client.post(f"/circuits/{bundle_id}/attach-member", data={
        "member_circuit_id": str(other_bundle_id),
    }, follow_redirects=True)

    with app.app_context():
        other = Circuit.query.get(other_bundle_id)
        assert other.parent_circuit_id is None


def test_attach_member_accepts_unattached_leaf_and_is_audited(app, admin_client):
    with app.app_context():
        site_a, site_b = make_site(name="A"), make_site(name="B")
        dev_a, dev_b = make_device(site_a), make_device(site_b)
        iface_a, iface_b = make_interface(dev_a), make_interface(dev_b)
        role = make_role()
        bundle = make_bundle(role=role, name="Target Bundle 2")
        leaf = make_circuit(iface_a, iface_b, role=role, name="Standalone Leaf")
        db.session.commit()
        bundle_id, leaf_id = bundle.id, leaf.id

    admin_client.post(f"/circuits/{bundle_id}/attach-member", data={
        "member_circuit_id": str(leaf_id),
    }, follow_redirects=True)

    with app.app_context():
        leaf = Circuit.query.get(leaf_id)
        assert leaf.parent_circuit_id == bundle_id
        entry = AuditLog.query.filter_by(object_type="Circuit", object_id=leaf_id, action="update").first()
        assert entry is not None


# --- delete guard: a bundle with members can't be deleted ---

def test_delete_bundle_blocked_when_it_has_members(app, admin_client):
    with app.app_context():
        site_a, site_b = make_site(name="A"), make_site(name="B")
        dev_a, dev_b = make_device(site_a), make_device(site_b)
        iface_a, iface_b = make_interface(dev_a), make_interface(dev_b)
        role = make_role()
        bundle = make_bundle(role=role, name="Occupied Bundle")
        make_circuit(iface_a, iface_b, role=role, parent=bundle)
        db.session.commit()
        bundle_id = bundle.id

    admin_client.post(f"/circuits/{bundle_id}/delete", follow_redirects=True)

    with app.app_context():
        assert Circuit.query.get(bundle_id) is not None
        assert AuditLog.query.filter_by(object_type="Circuit", action="delete").count() == 0


# --- mute / unmute ---

def test_mute_and_unmute_circuit(app, admin_client):
    with app.app_context():
        site_a, site_b = make_site(name="A"), make_site(name="B")
        dev_a, dev_b = make_device(site_a), make_device(site_b)
        iface_a, iface_b = make_interface(dev_a), make_interface(dev_b)
        circuit = make_circuit(iface_a, iface_b, name="Muteable")
        db.session.commit()
        circuit_id = circuit.id

    admin_client.post(f"/circuits/{circuit_id}/mute", data={"minutes": "15"}, follow_redirects=True)
    with app.app_context():
        assert AlertMute.is_muted(circuit_id) is True
        entry = AuditLog.query.filter_by(object_type="Circuit", object_id=circuit_id, action="mute").first()
        assert entry is not None
        assert entry.label == "Muteable"

    admin_client.post(f"/circuits/{circuit_id}/unmute", follow_redirects=True)
    with app.app_context():
        assert AlertMute.is_muted(circuit_id) is False
        entry = AuditLog.query.filter_by(object_type="Circuit", object_id=circuit_id, action="unmute").first()
        assert entry is not None
