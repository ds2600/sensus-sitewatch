"""backup.py's export/import round-trip for Layer and CustomFieldDefinition/
Value — both new, both worth their own regression coverage since Layer's
cross-scope reference (Site/Device/Circuit all point at it, but it only
rides in the "sites" scope) is exactly the kind of thing that's easy to
silently break in a future edit.
"""
from sitewatch.backup import export_data, import_data
from sitewatch.extensions import db
from sitewatch.models import Site, Device, Circuit, Layer, CustomFieldDefinition, CustomFieldValue
from sitewatch import custom_fields
from tests.factories import make_site, make_device, make_interface, make_circuit, make_layer, make_custom_field


def test_layer_round_trips_through_all_scope(app):
    with app.app_context():
        layer = make_layer(name="RT Layer")
        site = make_site(name="RT Layer Site", layer=layer)
        db.session.commit()

        data = export_data(scope="all")
        assert any(l["name"] == "RT Layer" for l in data["layers"])

        Circuit.query.delete()
        Device.query.delete()
        Site.query.delete()
        Layer.query.delete()
        db.session.commit()

        import_data(data, scope="all")

        restored_layer = Layer.query.filter_by(name="RT Layer").first()
        restored_site = Site.query.filter_by(name="RT Layer Site").first()
        assert restored_layer is not None
        assert restored_site.layer_id == restored_layer.id


def test_device_and_circuit_layer_id_round_trip(app):
    with app.app_context():
        layer = make_layer(name="RT Layer 2")
        site_a, site_b = make_site(name="A"), make_site(name="B")
        dev_a = make_device(site_a, layer=layer)
        dev_b = make_device(site_b)
        circuit = make_circuit(make_interface(dev_a), make_interface(dev_b), name="RT Circuit", layer=layer)
        db.session.commit()

        data = export_data(scope="all")

        Circuit.query.delete()
        Device.query.delete()
        Site.query.delete()
        Layer.query.delete()
        db.session.commit()

        import_data(data, scope="all")

        restored_layer = Layer.query.filter_by(name="RT Layer 2").first()
        restored_device = Device.query.filter_by(hostname=dev_a.hostname).first()
        restored_circuit = Circuit.query.filter_by(name="RT Circuit").first()
        assert restored_device.layer_id == restored_layer.id
        assert restored_circuit.layer_id == restored_layer.id


def test_custom_fields_round_trip_per_scope(app):
    with app.app_context():
        field = make_custom_field("site", name="RT Field")
        site = make_site(name="RT CF Site")
        db.session.commit()
        custom_fields.set_values("site", site.id, {f"custom_field_{field.id}": "RT Value"})
        db.session.commit()

        data = export_data(scope="sites")
        assert any(d["name"] == "RT Field" for d in data["site_custom_field_definitions"])
        assert any(v["value"] == "RT Value" for v in data["site_custom_field_values"])

        Site.query.delete()
        CustomFieldValue.query.delete()
        CustomFieldDefinition.query.delete()
        db.session.commit()

        import_data(data, scope="sites")

        restored_field = CustomFieldDefinition.query.filter_by(name="RT Field").first()
        restored_site = Site.query.filter_by(name="RT CF Site").first()
        assert restored_field is not None
        assert custom_fields.values_for("site", restored_site.id) == {restored_field.id: "RT Value"}


def test_old_backup_without_layers_or_custom_fields_still_imports(app):
    """The backward-compatibility promise: an export from before these
    features existed has neither key at all, and that must not be an
    import error (both are read via .get(..., []))."""
    with app.app_context():
        site = make_site(name="Old Format Site")
        db.session.commit()
        data = export_data(scope="all")

    del data["layers"]
    del data["site_custom_field_definitions"]
    del data["site_custom_field_values"]

    with app.app_context():
        Site.query.delete()
        db.session.commit()
        import_data(data, scope="all")  # must not raise
        assert Site.query.filter_by(name="Old Format Site").count() == 1
