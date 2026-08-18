"""Standalone probe (Zabbix-proxy-style remote poller): Probe CRUD/delete-
guard, probe_required auth, the /api/probe/devices + /api/probe/report
endpoints, and the staleness watchdog. See models.py's Probe/ProbeAction
docstrings and poller.py's apply_probe_report/check_probe_staleness for
the design this covers.
"""
from datetime import datetime, timedelta
from unittest.mock import patch

from sitewatch.extensions import db
from sitewatch.models import Device, Circuit, CircuitStatusHistory, Probe, Setting, AuditLog
from sitewatch.poller import apply_probe_report, check_probe_staleness
from tests.factories import make_site, make_device, make_interface, make_role, make_circuit, make_probe


def _auth(key="test-probe-key"):
    return {"Authorization": f"Bearer {key}"}


# --- probe_required auth ---

def test_probe_devices_requires_bearer_token(app, client):
    resp = client.get("/api/probe/devices")
    assert resp.status_code == 401


def test_probe_devices_rejects_wrong_key(app, client):
    with app.app_context():
        make_probe(api_key="the-real-key")
        db.session.commit()
    resp = client.get("/api/probe/devices", headers=_auth("wrong-key"))
    assert resp.status_code == 401


def test_probe_devices_accepts_correct_key(app, client):
    with app.app_context():
        make_probe(api_key="the-real-key")
        db.session.commit()
    resp = client.get("/api/probe/devices", headers=_auth("the-real-key"))
    assert resp.status_code == 200


# --- /api/probe/devices ---

def test_probe_devices_returns_only_assigned_devices_with_decrypted_credentials(app, client):
    with app.app_context():
        probe = make_probe()
        site = make_site()
        owned = make_device(site, hostname="owned-dev", probe=probe)
        owned.snmp_community = "s3cr3t"
        unowned = make_device(site, hostname="unowned-dev")
        db.session.commit()
        owned_id = owned.id

    resp = client.get("/api/probe/devices", headers=_auth())
    data = resp.get_json()
    assert [d["id"] for d in data["devices"]] == [owned_id]
    assert data["devices"][0]["snmp_community"] == "s3cr3t"


def test_probe_devices_includes_only_monitored_interfaces(app, client):
    with app.app_context():
        probe = make_probe()
        site_a, site_b = make_site(name="A"), make_site(name="B")
        dev_a = make_device(site_a, probe=probe)
        dev_b = make_device(site_b)
        iface_a, iface_b = make_interface(dev_a), make_interface(dev_b)
        make_interface(dev_a)  # a second interface on dev_a, not wired into any circuit
        make_circuit(iface_a, iface_b)
        db.session.commit()
        iface_a_id = iface_a.id

    resp = client.get("/api/probe/devices", headers=_auth())
    ifaces = resp.get_json()["devices"][0]["interfaces"]
    assert len(ifaces) == 1
    assert ifaces[0]["id"] == iface_a_id


# --- /api/probe/report ---

def test_report_applies_telemetry_and_transitions_circuit_down(app, client):
    with app.app_context():
        Setting.set("down_threshold_count", "1")
        db.session.commit()
        probe = make_probe()
        site_a, site_b = make_site(name="A"), make_site(name="B")
        dev_a = make_device(site_a, probe=probe)
        dev_b = make_device(site_b)
        iface_a, iface_b = make_interface(dev_a), make_interface(dev_b)
        circuit = make_circuit(iface_a, iface_b, current_state="up")
        db.session.commit()
        dev_a_id, iface_a_id, circuit_id = dev_a.id, iface_a.id, circuit.id

    with patch("sitewatch.poller.send_down_alerts") as mock_send:
        resp = client.post("/api/probe/report", headers=_auth(), json={
            "devices": [{
                "device_id": dev_a_id, "reachable": True,
                "interfaces": {str(iface_a_id): {"data": {
                    "oper_status": "down", "admin_status": "up", "in_octets": 0, "out_octets": 0,
                }}},
            }],
        })
    assert resp.status_code == 200
    assert mock_send.call_count == 1

    with app.app_context():
        assert Device.query.get(dev_a_id).reachable is True
        assert Circuit.query.get(circuit_id).current_state == "down"
        probe = Probe.query.first()
        assert probe.last_seen_at is not None


def test_report_ownership_check_rejects_device_not_assigned_to_this_probe(app, client):
    with app.app_context():
        probe_a = make_probe(name="probe-a", api_key="key-a")
        probe_b = make_probe(name="probe-b", api_key="key-b")
        site = make_site()
        device = make_device(site, probe=probe_a)  # assigned to probe_a, not probe_b
        db.session.commit()
        device_id = device.id

    client.post("/api/probe/report", headers=_auth("key-b"), json={
        "devices": [{"device_id": device_id, "reachable": False, "interfaces": {}}],
    })

    with app.app_context():
        # probe_b's report for a device it doesn't own must be ignored —
        # device.reachable stays at its default (True), not flipped by
        # a probe with no ownership over it.
        assert Device.query.get(device_id).reachable is True


# --- staleness watchdog ---

def test_staleness_watchdog_marks_devices_unreachable_and_alerts_once(app, client):
    with app.app_context():
        probe = make_probe()
        probe.last_seen_at = datetime.utcnow() - timedelta(minutes=999)
        site = make_site()
        device = make_device(site, probe=probe, reachable=True)
        db.session.commit()
        device_id = device.id

        with patch("sitewatch.poller.send_probe_stale_alert") as mock_stale:
            check_probe_staleness()
            assert Device.query.get(device_id).reachable is False
            assert Probe.query.first().stale is True
            mock_stale.assert_called_once()

            # A second pass while still stale must not re-fire the alert.
            check_probe_staleness()
            mock_stale.assert_called_once()


def test_staleness_recovery_clears_flag_and_alerts_once(app, client):
    with app.app_context():
        probe = make_probe()
        probe.last_seen_at = datetime.utcnow() - timedelta(minutes=999)
        probe.stale = True
        site = make_site()
        device = make_device(site, probe=probe, reachable=False)
        db.session.commit()
        device_id, probe_id = device.id, probe.id

    with app.app_context(), patch("sitewatch.poller.send_probe_recovered_alert") as mock_recovered:
        apply_probe_report(Probe.query.get(probe_id), [
            {"device_id": device_id, "reachable": True, "interfaces": {}},
        ])
        mock_recovered.assert_called_once()
        assert Probe.query.get(probe_id).stale is False


def test_never_checked_in_probe_does_not_falsely_trigger_immediately(app, client):
    with app.app_context():
        Setting.set("probe_stale_after_minutes", "15")
        probe = make_probe()  # last_seen_at is None, created_at is "now"
        db.session.commit()
        probe_id = probe.id

        with patch("sitewatch.poller.send_probe_stale_alert") as mock_stale:
            check_probe_staleness()
        assert Probe.query.get(probe_id).stale is False
        mock_stale.assert_not_called()


# --- Probe CRUD / delete-guard (Settings routes) ---

def test_add_probe_generates_api_key_and_flashes_it_once(app, admin_client):
    resp = admin_client.post("/settings/probes/add", data={"name": "West Site Probe"}, follow_redirects=True)
    assert b"West Site Probe" in resp.data
    assert b"API key" in resp.data
    with app.app_context():
        probe = Probe.query.filter_by(name="West Site Probe").first()
        assert probe is not None
        assert probe.api_key  # decrypts to something non-empty
        entry = AuditLog.query.filter_by(object_type="Probe", action="create").first()
        assert entry is not None


def test_delete_probe_blocked_while_assigned_to_a_device(app, admin_client):
    with app.app_context():
        probe = make_probe(name="In Use Probe")
        site = make_site()
        make_device(site, probe=probe)
        db.session.commit()
        probe_id = probe.id

    resp = admin_client.post(f"/settings/probes/{probe_id}/delete", follow_redirects=True)
    assert b"in use" in resp.data.lower()
    with app.app_context():
        assert Probe.query.get(probe_id) is not None


def test_delete_probe_succeeds_when_unassigned(app, admin_client):
    with app.app_context():
        probe = make_probe(name="Unused Probe")
        db.session.commit()
        probe_id = probe.id

    admin_client.post(f"/settings/probes/{probe_id}/delete", follow_redirects=True)
    with app.app_context():
        assert Probe.query.get(probe_id) is None
