"""Background polling job. One pass = check every device's reachability,
GET each known interface's counters/status, apply the down-threshold
debounce, roll bundle/site status up, and fire alerts on new down events.

Full interface discovery (walk_interfaces) does NOT happen here — that's
triggered manually per device. This loop only polls interfaces already
known to the DB.
"""
from datetime import datetime, timedelta
import logging

from sitewatch.extensions import db, scheduler
from sitewatch.models import Device, Interface, Circuit, CircuitStatusHistory, Setting, Site
from sitewatch import telemetry
from sitewatch.snmp import SnmpError
from sitewatch.status import recompute_bundle_state, rollup_degree_status
from sitewatch.integrations.webhook_payload import send_down_alert

log = logging.getLogger(__name__)


def poll_all_devices():
    for device in Device.query.all():
        _poll_device(device)
    _recompute_all_circuit_states()
    db.session.commit()


def _poll_device(device):
    device.reachable = telemetry.check_reachable(device)
    if not device.reachable:
        return  # interfaces stay at last-known values; circuit state handled via device.reachable check below

    for iface in device.interfaces:
        try:
            data = telemetry.poll_interface_counters(device, iface)
        except SnmpError:
            continue  # transient GET failure — leave stale, next poll will retry

        now = datetime.utcnow()
        if iface.last_counter_at:
            elapsed = (now - iface.last_counter_at).total_seconds()
            if elapsed > 0 and iface.last_in_octets is not None:
                iface.last_in_bps = max(0, (data["in_octets"] - iface.last_in_octets) * 8 / elapsed)
                iface.last_out_bps = max(0, (data["out_octets"] - iface.last_out_octets) * 8 / elapsed)

        iface.last_in_octets = data["in_octets"]
        iface.last_out_octets = data["out_octets"]
        iface.last_counter_at = now
        iface.oper_status = data["oper_status"]
        iface.admin_status = data["admin_status"]
        iface.last_polled_at = now


def _leaf_target_state(circuit):
    """What state a leaf circuit's raw telemetry says right now, before
    debounce is applied."""
    ifaces = [circuit.interface_a, circuit.interface_b]
    devices = [i.device for i in ifaces]

    if any(not d.reachable for d in devices):
        return "unreachable"
    if any(i.admin_status == "down" for i in ifaces):
        return "admin_down"
    if any(i.oper_status == "down" for i in ifaces):
        return "down"
    return "up"


def _apply_debounce(circuit, target_state):
    threshold = Setting.get_int("down_threshold_count")

    # admin_down / unreachable are deterministic config/reachability facts,
    # not flappy telemetry — apply immediately, no debounce needed.
    if target_state in ("admin_down", "unreachable"):
        circuit.current_state = target_state
        circuit.consecutive_fail_count = 0
        circuit.consecutive_success_count = 0
        return

    if target_state == "down":
        circuit.consecutive_fail_count += 1
        circuit.consecutive_success_count = 0
        if circuit.current_state == "up" and circuit.consecutive_fail_count >= threshold:
            _transition(circuit, "down")
    else:  # up
        circuit.consecutive_success_count += 1
        circuit.consecutive_fail_count = 0
        if circuit.current_state == "down" and circuit.consecutive_success_count >= threshold:
            _transition(circuit, "up")
        elif circuit.current_state in ("admin_down", "unreachable"):
            circuit.current_state = "up"


def _transition(circuit, new_state):
    old_state = circuit.current_state
    circuit.current_state = new_state
    circuit.state_changed_at = datetime.utcnow()

    if new_state == "down":
        db.session.add(CircuitStatusHistory(circuit_id=circuit.id, state="down", started_at=datetime.utcnow()))
        from sitewatch.models import AlertMute
        if not AlertMute.is_muted(circuit.id):
            send_down_alert(circuit)
    elif old_state == "down" and new_state == "up":
        open_record = (CircuitStatusHistory.query
                       .filter_by(circuit_id=circuit.id, cleared_at=None)
                       .order_by(CircuitStatusHistory.started_at.desc()).first())
        if open_record:
            open_record.cleared_at = datetime.utcnow()


def _recompute_all_circuit_states():
    # Leaf circuits first (bottom-up), then bundles, since bundle state
    # depends on children already being current.
    leaves = [c for c in Circuit.query.all() if not c.is_bundle]
    for circuit in leaves:
        target = _leaf_target_state(circuit)
        _apply_debounce(circuit, target)

    bundles = [c for c in Circuit.query.all() if c.is_bundle]
    # Multiple passes handle bundles-of-bundles without needing a full topo sort.
    for _ in range(3):
        for bundle in bundles:
            recompute_bundle_state(bundle)


def poll_device_now(device):
    """Manual single-device repoll — the device detail page's "Repoll" button.
    Runs the same telemetry + debounce logic as a scheduled pass, just scoped
    to one device's reachability/interfaces instead of waiting for the next
    cycle. Circuit/bundle states are recomputed across the board afterward
    since this device's interfaces may affect circuits touching other sites."""
    _poll_device(device)
    _recompute_all_circuit_states()
    db.session.commit()


def start_poller(app):
    with app.app_context():
        interval = Setting.query.get("polling_interval_minutes")
        minutes = int(interval.value) if interval else 2

    def job():
        with app.app_context():
            try:
                poll_all_devices()
            except Exception:
                log.exception("Poll cycle failed")

    scheduler.add_job(job, "interval", minutes=minutes, id="poll_all_devices", replace_existing=True)
    scheduler.start()
