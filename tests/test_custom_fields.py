"""Custom fields: admin-defined text fields on Site/Device/Circuit
(Settings -> Custom fields). Covers the custom_fields.py helper module
directly (upsert/clear semantics, cross-object-type isolation) and the
route-level integration (form save, delete-guard behavior, audit trail,
and that deleting the parent object also clears its values despite
CustomFieldValue.object_id not being a real FK).
"""
from sitewatch.extensions import db
from sitewatch.models import CustomFieldDefinition, CustomFieldValue, Site, AuditLog
from sitewatch import custom_fields
from tests.factories import make_site, make_device, make_interface, make_circuit, make_custom_field


# --- custom_fields.py helper module, directly ---

def test_set_values_creates_and_reads_back(app):
    with app.app_context():
        field = make_custom_field("site", name="Contract #")
        site = make_site()
        db.session.commit()

        diff = custom_fields.set_values("site", site.id, {f"custom_field_{field.id}": "ABC-123"})
        db.session.commit()

        assert diff == {"custom:Contract #": {"old": None, "new": "ABC-123"}}
        assert custom_fields.values_for("site", site.id) == {field.id: "ABC-123"}


def test_set_values_blank_clears_existing_value(app):
    with app.app_context():
        field = make_custom_field("site", name="Note")
        site = make_site()
        db.session.commit()
        custom_fields.set_values("site", site.id, {f"custom_field_{field.id}": "first value"})
        db.session.commit()

        diff = custom_fields.set_values("site", site.id, {f"custom_field_{field.id}": ""})
        db.session.commit()

        assert diff == {"custom:Note": {"old": "first value", "new": None}}
        assert custom_fields.values_for("site", site.id) == {}
        assert CustomFieldValue.query.count() == 0


def test_set_values_no_change_returns_empty_diff(app):
    with app.app_context():
        field = make_custom_field("site", name="Stable")
        site = make_site()
        db.session.commit()
        custom_fields.set_values("site", site.id, {f"custom_field_{field.id}": "same"})
        db.session.commit()

        diff = custom_fields.set_values("site", site.id, {f"custom_field_{field.id}": "same"})
        assert diff == {}


def test_fields_scoped_to_object_type(app):
    """A device-type field must never apply to a site's form/values, even
    if a form happens to submit a matching custom_field_<id> key."""
    with app.app_context():
        site_field = make_custom_field("site", name="Site Only")
        device_field = make_custom_field("device", name="Device Only")
        assert custom_fields.definitions_for("site") == [site_field]
        assert custom_fields.definitions_for("device") == [device_field]


# --- route-level integration ---

def test_add_custom_field_definition_and_delete(app, admin_client):
    admin_client.post("/settings/custom-fields/add", data={"name": "Owner", "object_type": "circuit"},
                       follow_redirects=True)
    with app.app_context():
        field = CustomFieldDefinition.query.filter_by(name="Owner", object_type="circuit").first()
        assert field is not None
        field_id = field.id

    admin_client.post(f"/settings/custom-fields/{field_id}/delete", follow_redirects=True)
    with app.app_context():
        assert CustomFieldDefinition.query.get(field_id) is None


def test_deleting_field_definition_cascades_its_values(app, admin_client):
    with app.app_context():
        field = make_custom_field("site", name="To Delete")
        site = make_site()
        db.session.commit()
        custom_fields.set_values("site", site.id, {f"custom_field_{field.id}": "some value"})
        db.session.commit()
        field_id = field.id
        assert CustomFieldValue.query.count() == 1

    admin_client.post(f"/settings/custom-fields/{field_id}/delete", follow_redirects=True)
    with app.app_context():
        assert CustomFieldDefinition.query.get(field_id) is None
        assert CustomFieldValue.query.count() == 0  # cascaded, not blocked


def test_site_form_saves_custom_field_value(app, admin_client):
    with app.app_context():
        field = make_custom_field("site", name="Building")
        db.session.commit()
        field_id = field.id

    admin_client.post("/sites/add", data={
        "name": "CF Site", "lat": "1", "lon": "1", "site_type": "site",
        f"custom_field_{field_id}": "Building 7",
    }, follow_redirects=True)

    with app.app_context():
        site = Site.query.filter_by(name="CF Site").first()
        assert site is not None
        assert custom_fields.values_for("site", site.id) == {field_id: "Building 7"}
        entry = AuditLog.query.filter_by(object_type="Site", object_id=site.id, action="create").first()
        assert "custom:Building" in entry.details


def test_deleting_object_clears_its_custom_field_values(app, admin_client):
    """CustomFieldValue.object_id isn't a real FK, so nothing cascades
    automatically here — routes/sites.py's delete route must explicitly
    call custom_fields.delete_values() before deleting the site."""
    with app.app_context():
        field = make_custom_field("site", name="Cleared On Delete")
        site = make_site(name="Doomed CF Site")
        db.session.commit()
        custom_fields.set_values("site", site.id, {f"custom_field_{field.id}": "x"})
        db.session.commit()
        site_id = site.id
        assert CustomFieldValue.query.filter_by(object_id=site_id).count() == 1

    admin_client.post(f"/sites/{site_id}/delete", follow_redirects=True)

    with app.app_context():
        assert Site.query.get(site_id) is None
        assert CustomFieldValue.query.filter_by(object_id=site_id).count() == 0


def test_device_form_saves_custom_field_value(app, admin_client):
    with app.app_context():
        device_field = make_custom_field("device", name="Asset Tag")
        site_a = make_site(name="A")
        db.session.commit()
        site_a_id, device_field_id = site_a.id, device_field.id

    admin_client.post("/devices/add", data={
        "site_id": str(site_a_id), "hostname": "cf-device", "mgmt_ip": "10.9.9.9",
        "vendor": "ios-xe", "snmp_version": "v2c",
        f"custom_field_{device_field_id}": "AT-42",
    }, follow_redirects=True)
    with app.app_context():
        from sitewatch.models import Device
        device = Device.query.filter_by(hostname="cf-device").first()
        assert device is not None
        assert custom_fields.values_for("device", device.id) == {device_field_id: "AT-42"}


def test_circuit_form_saves_custom_field_value(app, admin_client):
    from tests.factories import make_role
    with app.app_context():
        circuit_field = make_custom_field("circuit", name="Client")
        site_a, site_b = make_site(name="A"), make_site(name="B")
        dev_a, dev_b = make_device(site_a), make_device(site_b)
        iface_a, iface_b = make_interface(dev_a), make_interface(dev_b)
        role = make_role()
        db.session.commit()
        circuit_field_id, role_id = circuit_field.id, role.id
        iface_a_id, iface_b_id = iface_a.id, iface_b.id

    admin_client.post("/circuits/add", data={
        "name": "CF Circuit", "role_id": str(role_id),
        "interface_a_id": str(iface_a_id), "interface_b_id": str(iface_b_id),
        f"custom_field_{circuit_field_id}": "Acme Corp",
    }, follow_redirects=True)

    with app.app_context():
        from sitewatch.models import Circuit
        circuit = Circuit.query.filter_by(name="CF Circuit").first()
        assert circuit is not None
        assert custom_fields.values_for("circuit", circuit.id) == {circuit_field_id: "Acme Corp"}
