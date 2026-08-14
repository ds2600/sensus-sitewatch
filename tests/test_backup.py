"""backup.py — export/import round-tripping and the backward-compatibility
guarantee CLAUDE.md calls out explicitly: a backup exported before a
change must still import cleanly after it. These tests are the regression
net for that promise, not just "does export/import work."
"""
from sitewatch.backup import export_data, import_data
from sitewatch.extensions import db
from sitewatch.models import Site, Device, Circuit, User
from tests.factories import make_site, make_device, make_interface, make_circuit, make_role


def test_export_all_never_includes_users_or_credentials(app):
    with app.app_context():
        site = make_site()
        device = make_device(site)
        device.snmp_community_enc = "should-never-appear"
        db.session.commit()

        data = export_data(scope="all")
        assert "users" not in data
        text = str(data)
        assert "should-never-appear" not in text
        assert "snmp_community" not in text


def test_export_import_round_trip_preserves_data(app):
    with app.app_context():
        site_a, site_b = make_site(name="RT Site A"), make_site(name="RT Site B")
        dev_a, dev_b = make_device(site_a), make_device(site_b)
        iface_a, iface_b = make_interface(dev_a), make_interface(dev_b)
        role = make_role(name="rt-role")
        make_circuit(iface_a, iface_b, role=role, name="RT Circuit")
        db.session.commit()

        exported = export_data(scope="all")

        # Wipe everything the "all" scope owns, then restore from the export.
        Circuit.query.delete()
        Device.query.delete()
        Site.query.delete()
        db.session.commit()
        assert Site.query.count() == 0

        import_data(exported, scope="all")

        assert Site.query.filter_by(name="RT Site A").count() == 1
        assert Device.query.count() == 2
        restored_circuit = Circuit.query.filter_by(name="RT Circuit").first()
        assert restored_circuit is not None
        assert restored_circuit.role.name == "rt-role"


def test_import_all_tolerates_missing_optional_regions_key(app):
    """An export from before MapRegion existed has no "regions" key at
    all — CLAUDE.md is explicit that this must not be an import error."""
    with app.app_context():
        make_site(name="Old Format Site")
        db.session.commit()
        data = export_data(scope="all")

    del data["regions"]  # simulate a pre-MapRegion export

    with app.app_context():
        Site.query.delete()
        db.session.commit()
        import_data(data, scope="all")  # must not raise
        assert Site.query.filter_by(name="Old Format Site").count() == 1


def test_scoped_sites_import_does_not_touch_devices(app):
    with app.app_context():
        site = make_site(name="Scoped Site")
        make_device(site, hostname="untouched-device")
        db.session.commit()
        sites_export = export_data(scope="sites")

        Site.query.delete()
        db.session.commit()
        import_data(sites_export, scope="sites")

        # The site is back, but the device that referenced its old site_id
        # was never touched by this scope at all.
        assert Site.query.filter_by(name="Scoped Site").count() == 1
        assert Device.query.filter_by(hostname="untouched-device").count() == 1


def test_import_preserves_original_ids(app):
    with app.app_context():
        site = make_site(name="Id Preserving Site")
        db.session.commit()
        original_id = site.id
        data = export_data(scope="sites")

        Site.query.delete()
        db.session.commit()
        import_data(data, scope="sites")

        restored = Site.query.filter_by(name="Id Preserving Site").first()
        assert restored.id == original_id
