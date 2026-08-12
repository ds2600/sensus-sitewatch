"""Background polling job. One pass = check every device's reachability,
GET each known interface's counters/status, apply the down-threshold
debounce, roll bundle/site status up, and fire alerts on new down events.

Full interface discovery (walk_interfaces) does NOT happen here — that's
triggered manually per device. This loop only polls interfaces already
known to the DB.
"""
from datetime import datetime, timedelta, timezone
import logging
import os
import time

from sitewatch.extensions import db, scheduler
from sitewatch.models import Device, Interface, Circuit, CircuitStatusHistory, Setting, Site
from sitewatch import telemetry
from sitewatch.snmp import SnmpError
from sitewatch.status import recompute_bundle_state, rollup_degree_status
from sitewatch.integrations.webhook_payload import send_down_alert

log = logging.getLogger(__name__)

POLLER_JOB_ID = "poll_all_devices"

# Set for the duration of an actual sweep so get_poller_status() can tell
# "waiting for next cycle" apart from "sweep in progress right now" —
# APScheduler's job/next_run_time state alone doesn't capture that.
_currently_polling = False


def poll_all_devices():
    """One full sweep: every device polled in turn, synchronously, on a
    single APScheduler job — not staggered or rotated across cycles. Timing
    is recorded (Settings: last_poll_duration_seconds/last_poll_finished_at,
    shown on the Settings page) so the configured polling_interval_minutes
    can be checked against how long a sweep actually takes."""
    global _currently_polling
    _currently_polling = True
    try:
        started = time.monotonic()
        device_count = 0
        for device in Device.query.all():
            _poll_device(device)
            # Recompute circuit/bundle state after every device, not once at
            # the end of the whole sweep — otherwise a link that goes down on
            # the very first device polled wouldn't show as down until every
            # other device in the sweep finished too, which for a real
            # multi-device deployment can be a long wait for what should be
            # near-real-time. Commit here as well, both so the fresher state
            # is actually visible to other requests right away and so SQLite
            # doesn't hold one write transaction open for the whole sweep
            # (see the "database is locked" fix — same reasoning applies).
            _recompute_all_circuit_states()
            db.session.commit()
            device_count += 1
        duration = time.monotonic() - started
        Setting.set("last_poll_duration_seconds", f"{duration:.1f}")
        Setting.set("last_poll_finished_at", datetime.utcnow().isoformat())
        db.session.commit()
        log.info("Poll cycle finished in %.1fs (%d devices)", duration, device_count)
    finally:
        _currently_polling = False


def _poll_device(device):
    device.last_polled_at = datetime.utcnow()
    device.reachable = telemetry.check_reachable(device)
    if not device.reachable:
        return  # interfaces stay at last-known values; circuit state handled via device.reachable check below

    for iface in device.interfaces:
        try:
            data = telemetry.poll_interface_counters(device, iface)
        except SnmpError as e:
            log.info("Skipping %s (ifIndex %s) on %s this cycle: %s",
                      iface.if_descr or f"ifIndex {iface.if_index}", iface.if_index, device.hostname, e)
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
    log.info("Repolling %s (%s) — %d known interface(s)...",
              device.hostname, device.mgmt_ip, len(device.interfaces))
    _poll_device(device)
    _recompute_all_circuit_states()
    db.session.commit()
    log.info("Repoll of %s complete: %s.", device.hostname, "reachable" if device.reachable else "unreachable")


def start_poller(app):
    with app.app_context():
        interval = Setting.query.get("polling_interval_minutes")
        minutes = int(interval.value) if interval else 2
        enabled = Setting.get("poller_enabled", "1") != "0"

    def job():
        with app.app_context():
            try:
                poll_all_devices()
            except Exception:
                log.exception("Poll cycle failed")

    scheduler.add_job(job, "interval", minutes=minutes, id=POLLER_JOB_ID, replace_existing=True)
    scheduler.start()
    if not enabled:
        # Restore whatever start/stop state was last set from the UI —
        # otherwise a stopped poller would silently come back on restart.
        scheduler.pause_job(POLLER_JOB_ID)


def poller_enabled_for_process():
    """Whether this process was launched with SITEWATCH_RUN_POLLER=1. The
    scheduler and its job only exist at all when this is true — a process
    started without it (e.g. `flask --app app run` for poking at routes)
    has nothing to start/stop from the UI."""
    return os.environ.get("SITEWATCH_RUN_POLLER") == "1"


def pause_poller():
    scheduler.pause_job(POLLER_JOB_ID)
    Setting.set("poller_enabled", "0")
    db.session.commit()


def resume_poller():
    scheduler.resume_job(POLLER_JOB_ID)
    Setting.set("poller_enabled", "1")
    db.session.commit()


def reschedule_poller(minutes):
    """Called from the Settings save route when polling_interval_minutes
    changes. Without this, editing the setting only ever changed what
    start_poller() would read on the *next* process restart — the live
    APScheduler job kept running on whatever interval it was created with,
    so the Settings page could say e.g. "every 2 minutes" while the actual
    job was still firing every 10. reschedule_job() takes effect immediately
    and preserves next_run_time semantics (fires next at the next multiple
    of the new interval from the job's last run, not by resetting the clock)."""
    if not poller_enabled_for_process() or not scheduler.running:
        return
    scheduler.reschedule_job(POLLER_JOB_ID, trigger="interval", minutes=minutes)


def get_poller_status():
    """Status for the header icon and the Settings page. `state` is one of
    disabled/stopped/waiting/polling; `active` is what the header icon's
    play-vs-stop color should key off. next_run_at lets the UI show exactly
    when the next cycle is due instead of a vague "waiting"."""
    if not poller_enabled_for_process():
        return {"state": "disabled", "label": "Not running in this process", "active": False,
                "next_run_at": None, "interval_minutes": None}
    if not scheduler.running:
        return {"state": "stopped", "label": "Stopped", "active": False,
                "next_run_at": None, "interval_minutes": None}
    job = scheduler.get_job(POLLER_JOB_ID)
    interval_minutes = Setting.get_int("polling_interval_minutes")
    if job is None or job.next_run_time is None:
        return {"state": "stopped", "label": "Stopped", "active": False,
                "next_run_at": None, "interval_minutes": interval_minutes}
    # APScheduler's next_run_time is tz-aware in the local system timezone;
    # normalize to UTC to match last_poll_finished_at's naive-UTC format
    # elsewhere on the Settings page.
    next_run_at = job.next_run_time.astimezone(timezone.utc).isoformat()
    if _currently_polling:
        return {"state": "polling", "label": "Polling now", "active": True,
                "next_run_at": next_run_at, "interval_minutes": interval_minutes}
    return {"state": "waiting", "label": "Waiting for next cycle", "active": True,
            "next_run_at": next_run_at, "interval_minutes": interval_minutes}
