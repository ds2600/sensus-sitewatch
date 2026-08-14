"""poller.py's alert-batching: every circuit that goes down within one
poll_all_devices() sweep must reach send_down_alerts() as ONE call with
the whole batch, never once per circuit. This is the regression net for
the grouped-webhook work — see webhook_payload.py's module docstring.
"""
from unittest.mock import patch

from sitewatch.extensions import db
from sitewatch.models import Circuit, Setting
from tests.factories import make_site, make_device, make_interface, make_role, make_circuit


def _simulated_down_circuit(site_a_name, site_b_name, circuit_name):
    """A leaf circuit whose interfaces are tagged [sim:down] — simulator.py
    reads this tag off if_alias and reports oper=down from then on."""
    site_a, site_b = make_site(name=site_a_name), make_site(name=site_b_name)
    dev_a, dev_b = make_device(site_a), make_device(site_b)
    iface_a = make_interface(dev_a)
    iface_b = make_interface(dev_b)
    iface_a.if_alias = "[sim:down]"
    iface_b.if_alias = "[sim:down]"
    role = make_role(tier="critical")
    return make_circuit(iface_a, iface_b, role=role, current_state="up", name=circuit_name)


def test_two_circuits_down_in_one_sweep_send_one_grouped_alert(app):
    with app.app_context():
        Setting.set("down_threshold_count", "1")  # transition on the first failed poll, no debounce wait
        db.session.commit()
        c1 = _simulated_down_circuit("Sweep Site A", "Sweep Site B", "Sweep Circuit 1")
        c2 = _simulated_down_circuit("Sweep Site C", "Sweep Site D", "Sweep Circuit 2")
        db.session.commit()
        c1_id, c2_id = c1.id, c2.id

        with patch("sitewatch.poller.send_down_alerts") as mock_send:
            from sitewatch.poller import poll_all_devices
            poll_all_devices()

        assert mock_send.call_count == 1
        batch = mock_send.call_args[0][0]
        assert {c.id for c in batch} == {c1_id, c2_id}

        assert Circuit.query.get(c1_id).current_state == "down"
        assert Circuit.query.get(c2_id).current_state == "down"


def test_idle_sweep_calls_send_down_alerts_with_empty_batch(app):
    """Called every sweep regardless (send_down_alerts no-ops internally
    on an empty list) — this just confirms nothing ever goes down."""
    with app.app_context():
        make_site(name="Quiet Site")
        db.session.commit()

        with patch("sitewatch.poller.send_down_alerts") as mock_send:
            from sitewatch.poller import poll_all_devices
            poll_all_devices()

        assert mock_send.call_count == 1
        assert mock_send.call_args[0][0] == []


def test_muted_circuit_going_down_is_excluded_from_the_batch(app):
    with app.app_context():
        Setting.set("down_threshold_count", "1")
        db.session.commit()
        c1 = _simulated_down_circuit("Muted Site A", "Muted Site B", "Muted Circuit")
        db.session.commit()
        c1_id = c1.id

        from sitewatch.models import AlertMute
        from datetime import datetime, timedelta
        db.session.add(AlertMute(circuit_id=c1_id, muted_until=datetime.utcnow() + timedelta(minutes=15)))
        db.session.commit()

        with patch("sitewatch.poller.send_down_alerts") as mock_send:
            from sitewatch.poller import poll_all_devices
            poll_all_devices()

        assert mock_send.call_args[0][0] == []
        # State still transitions to down — only the alert is suppressed.
        assert Circuit.query.get(c1_id).current_state == "down"
