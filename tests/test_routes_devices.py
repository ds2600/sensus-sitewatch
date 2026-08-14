from sitewatch.extensions import db
from sitewatch.models import Device, AuditLog
from sitewatch.crypto import decrypt
from tests.factories import make_site, make_device, make_interface, make_circuit


def test_add_device_with_credentials(app, admin_client):
    with app.app_context():
        site = make_site()
        db.session.commit()
        site_id = site.id

    admin_client.post("/devices/add", data={
        "site_id": str(site_id), "hostname": "new-dev", "mgmt_ip": "10.1.1.1",
        "vendor": "ios-xe", "snmp_version": "v2c", "snmp_community": "topsecret",
    }, follow_redirects=True)

    with app.app_context():
        device = Device.query.filter_by(hostname="new-dev").first()
        assert device is not None
        assert decrypt(device.snmp_community_enc) == "topsecret"

        entry = AuditLog.query.filter_by(object_type="Device", action="create").first()
        assert entry is not None
        assert entry.details is not None
        assert "topsecret" not in entry.details
        assert "credentials_updated" in entry.details


def test_edit_device_credentials_never_leak_into_audit_diff(app, admin_client):
    with app.app_context():
        site = make_site()
        device = make_device(site, hostname="edit-me")
        db.session.commit()
        device_id, site_id = device.id, site.id

    admin_client.post(f"/devices/{device_id}/edit", data={
        "site_id": str(site_id), "hostname": "edit-me-renamed", "mgmt_ip": "10.1.1.1",
        "vendor": "ios-xe", "snmp_version": "v2c", "snmp_community": "newsecret",
    }, follow_redirects=True)

    with app.app_context():
        entry = AuditLog.query.filter_by(object_type="Device", object_id=device_id, action="update").first()
        assert entry is not None
        assert "newsecret" not in entry.details
        assert "hostname" in entry.details  # the actual (non-credential) change is still captured
        assert "credentials_updated" in entry.details


def test_edit_device_without_touching_credentials_has_no_credentials_marker(app, admin_client):
    with app.app_context():
        site = make_site()
        device = make_device(site, hostname="edit-me2")
        db.session.commit()
        device_id, site_id = device.id, site.id

    admin_client.post(f"/devices/{device_id}/edit", data={
        "site_id": str(site_id), "hostname": "edit-me2-renamed", "mgmt_ip": "10.1.1.1",
        "vendor": "ios-xe", "snmp_version": "v2c",  # no snmp_community field submitted
    }, follow_redirects=True)

    with app.app_context():
        entry = AuditLog.query.filter_by(object_type="Device", object_id=device_id, action="update").first()
        assert entry is not None
        assert "credentials_updated" not in entry.details


def test_add_device_blocked_on_passthrough_site(app, admin_client):
    with app.app_context():
        site = make_site(site_type="passthrough")
        db.session.commit()
        site_id = site.id

    resp = admin_client.post("/devices/add", data={
        "site_id": str(site_id), "hostname": "nope", "mgmt_ip": "10.1.1.1",
        "vendor": "ios-xe", "snmp_version": "v2c",
    }, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert Device.query.filter_by(hostname="nope").first() is None


def test_delete_device_blocked_when_in_use_by_circuit(app, admin_client):
    with app.app_context():
        site_a, site_b = make_site(name="A"), make_site(name="B")
        dev_a = make_device(site_a)
        dev_b = make_device(site_b)
        iface_a = make_interface(dev_a)
        iface_b = make_interface(dev_b)
        make_circuit(iface_a, iface_b)
        db.session.commit()
        dev_a_id = dev_a.id

    admin_client.post(f"/devices/{dev_a_id}/delete", follow_redirects=True)

    with app.app_context():
        assert Device.query.get(dev_a_id) is not None
        assert AuditLog.query.filter_by(object_type="Device", action="delete").count() == 0


def test_delete_device_records_audit_entry(app, admin_client):
    with app.app_context():
        site = make_site()
        device = make_device(site, hostname="doomed-dev")
        db.session.commit()
        device_id = device.id

    admin_client.post(f"/devices/{device_id}/delete", follow_redirects=True)

    with app.app_context():
        assert Device.query.get(device_id) is None
        entry = AuditLog.query.filter_by(object_type="Device", object_id=device_id, action="delete").first()
        assert entry is not None
        assert entry.label == "doomed-dev"
