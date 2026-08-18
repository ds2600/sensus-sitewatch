"""backup.py's export/import round-trip for Probe/Device.probe_id — rides
in the "devices" scope only (referenced solely by Device, unlike Layer),
and api_key_enc must never appear in an export (same credential-exclusion
policy as Device's own SNMP/SSH secrets).
"""
from sitewatch.backup import export_data, import_data
from sitewatch.extensions import db
from sitewatch.models import Device, Probe
from tests.factories import make_site, make_device, make_probe


def test_probe_round_trips_through_devices_scope(app):
    with app.app_context():
        probe = make_probe(name="RT Probe")
        site = make_site(name="RT Probe Site")
        device = make_device(site, probe=probe)
        db.session.commit()

        data = export_data(scope="devices")
        assert any(p["name"] == "RT Probe" for p in data["probes"])

        Device.query.delete()
        Probe.query.delete()
        db.session.commit()

        import_data(data, scope="devices")

        restored_probe = Probe.query.filter_by(name="RT Probe").first()
        restored_device = Device.query.filter_by(hostname=device.hostname).first()
        assert restored_probe is not None
        assert restored_device.probe_id == restored_probe.id


def test_probe_api_key_never_appears_in_export(app):
    with app.app_context():
        probe = make_probe(name="Secret Probe", api_key="super-secret-key")
        db.session.commit()

        data = export_data(scope="all")
        exported = next(p for p in data["probes"] if p["name"] == "Secret Probe")
        assert "api_key" not in exported
        assert "api_key_enc" not in exported
        assert "super-secret-key" not in str(data)


def test_restored_probe_has_no_api_key_until_regenerated(app):
    with app.app_context():
        probe = make_probe(name="RT Probe 2", api_key="original-key")
        db.session.commit()

        data = export_data(scope="devices")
        Probe.query.delete()
        db.session.commit()
        import_data(data, scope="devices")

        restored = Probe.query.filter_by(name="RT Probe 2").first()
        assert restored.api_key is None  # never carried over — must be regenerated


def test_old_backup_without_probes_still_imports(app):
    """Backward compatibility: an export from before Probe existed has no
    "probes" key at all, read via .get(..., []) — must not be an import
    error."""
    with app.app_context():
        site = make_site(name="Old Format Device Site")
        make_device(site)
        db.session.commit()
        data = export_data(scope="devices")

    del data["probes"]

    with app.app_context():
        Device.query.delete()
        db.session.commit()
        import_data(data, scope="devices")  # must not raise
        assert Device.query.count() == 1
