"""On-demand Walk/Repoll for a probe-owned device: devices.py's walk_
device/repoll_device queue a ProbeAction instead of running synchronously,
routes/probe_api.py's pending-actions/walk-result/report endpoints let the
probe fulfill it and close out the same Tail Modal job. A directly-polled
device (probe_id is None) must be completely unaffected by any of this.
"""
import time

import pytest

from sitewatch.extensions import db
from sitewatch.models import Device, Interface, ProbeAction
from sitewatch import job_log, cooldown
from tests.factories import make_site, make_device, make_probe


def _auth(key="test-probe-key"):
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def _reset_cooldown():
    """cooldown.py's rate limit is a process-global in-memory dict keyed by
    device id, not reset between tests — and every test here gets a fresh
    per-test database where device ids restart from 1, so back-to-back
    tests in this file would otherwise collide on the same cooldown
    window. Clear it before each test."""
    cooldown._last_triggered.clear()
    yield


def _wait_for_job_done(job_id, timeout=5):
    """repoll_device's non-probe path runs on a background thread
    (job_log.run_in_background) — the HTTP response comes back before that
    thread necessarily finishes, so a job-completion assertion needs a
    short poll instead of assuming it's already done."""
    deadline = time.monotonic() + timeout
    job = job_log.get_job(job_id)
    while not job["done"] and time.monotonic() < deadline:
        time.sleep(0.05)
        job = job_log.get_job(job_id)
    return job


def test_walk_on_probe_owned_device_queues_action_instead_of_running(app, admin_client):
    with app.app_context():
        probe = make_probe()
        site = make_site()
        device = make_device(site, probe=probe)
        db.session.commit()
        device_id = device.id

    resp = admin_client.post(f"/devices/{device_id}/walk")
    assert resp.status_code == 200
    job_id = resp.get_json()["job_id"]

    with app.app_context():
        action = ProbeAction.query.filter_by(device_id=device_id).first()
        assert action is not None
        assert action.action == "walk"
        assert action.job_id == job_id
        assert action.completed_at is None
        job = job_log.get_job(job_id)
        assert job["done"] is False  # stays open until the probe reports back
        assert any("Queued for Probe" in line for line in job["lines"])


def test_repoll_on_directly_polled_device_runs_synchronously_unaffected(app, admin_client):
    with app.app_context():
        site = make_site()
        device = make_device(site)  # probe_id is None
        db.session.commit()
        device_id = device.id

    resp = admin_client.post(f"/devices/{device_id}/repoll")
    job_id = resp.get_json()["job_id"]

    with app.app_context():
        assert ProbeAction.query.filter_by(device_id=device_id).count() == 0
    job = _wait_for_job_done(job_id)
    assert job["done"] is True  # finishes on its own, no probe involved


def test_pending_actions_endpoint_returns_queued_action_for_owning_probe(app, admin_client):
    with app.app_context():
        probe = make_probe()
        site = make_site()
        device = make_device(site, probe=probe)
        db.session.commit()
        device_id = device.id

    resp = admin_client.post(f"/devices/{device_id}/walk")
    assert resp.status_code == 200

    resp = admin_client.get("/api/probe/pending-actions", headers=_auth())
    actions = resp.get_json()["actions"]
    assert len(actions) == 1
    assert actions[0]["action"] == "walk"
    assert actions[0]["device"]["id"] == device_id


def test_pending_actions_scoped_to_the_authenticated_probe_only(app, admin_client):
    with app.app_context():
        probe_a = make_probe(name="probe-a", api_key="key-a")
        probe_b = make_probe(name="probe-b", api_key="key-b")
        site = make_site()
        device = make_device(site, probe=probe_a)
        db.session.commit()
        device_id = device.id

    admin_client.post(f"/devices/{device_id}/walk")

    resp = admin_client.get("/api/probe/pending-actions", headers=_auth("key-b"))
    assert resp.get_json()["actions"] == []


def test_walk_result_applies_interfaces_and_finishes_the_job(app, admin_client):
    with app.app_context():
        probe = make_probe()
        site = make_site()
        device = make_device(site, probe=probe)
        db.session.commit()
        device_id = device.id

    resp = admin_client.post(f"/devices/{device_id}/walk")
    job_id = resp.get_json()["job_id"]

    with app.app_context():
        action = ProbeAction.query.filter_by(device_id=device_id).first()
        action_id = action.id

    resp = admin_client.post("/api/probe/walk-result", headers=_auth(), json={
        "action_id": action_id, "device_id": device_id,
        "interfaces": {"1": {"if_descr": "Gi0/1", "if_alias": "", "if_speed_bps": 1000000000,
                              "oper_status": "up", "admin_status": "up"}},
    })
    assert resp.status_code == 200

    with app.app_context():
        assert Interface.query.filter_by(device_id=device_id, if_index=1).first() is not None
        assert ProbeAction.query.get(action_id).completed_at is not None
        job = job_log.get_job(job_id)
        assert job["done"] is True
        assert job["success"] is True


def test_repoll_result_via_report_endpoint_finishes_the_job(app, admin_client):
    with app.app_context():
        probe = make_probe()
        site = make_site()
        device = make_device(site, probe=probe)
        db.session.commit()
        device_id = device.id

    resp = admin_client.post(f"/devices/{device_id}/repoll")
    job_id = resp.get_json()["job_id"]

    with app.app_context():
        action = ProbeAction.query.filter_by(device_id=device_id).first()
        action_id = action.id

    resp = admin_client.post("/api/probe/report", headers=_auth(), json={
        "devices": [{"device_id": device_id, "action_id": action_id, "reachable": True, "interfaces": {}}],
    })
    assert resp.status_code == 200

    with app.app_context():
        assert ProbeAction.query.get(action_id).completed_at is not None
        job = job_log.get_job(job_id)
        assert job["done"] is True


def test_report_without_action_id_does_not_touch_any_pending_action(app, admin_client):
    """A probe's regular (non-action) telemetry report must not accidentally
    finish an unrelated pending action for the same device."""
    with app.app_context():
        probe = make_probe()
        site = make_site()
        device = make_device(site, probe=probe)
        db.session.commit()
        device_id = device.id

    admin_client.post(f"/devices/{device_id}/walk")  # queues a walk, still pending

    admin_client.post("/api/probe/report", headers=_auth(), json={
        "devices": [{"device_id": device_id, "reachable": True, "interfaces": {}}],  # no action_id
    })

    with app.app_context():
        action = ProbeAction.query.filter_by(device_id=device_id).first()
        assert action.completed_at is None
