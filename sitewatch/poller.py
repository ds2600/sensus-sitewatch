"""Background polling job. One pass = check every device's reachability,
GET each circuit-linked interface's counters/status, apply the
down-threshold debounce, roll bundle/site status up, and fire alerts on new
down events.

Full interface discovery (walk_interfaces) does NOT happen here — that's
triggered manually per device, and discovers every interface on the box.
This loop deliberately polls a much smaller set: only interfaces actually
wired into a circuit (see _monitored_interface_ids). A device can easily
have hundreds of interfaces from a walk but only a handful ever become
circuits — polling all of them every cycle was most of what made poll
cycles slow for no operational benefit, since sitewatch has no use for
telemetry on an interface no circuit references.

Devices are polled concurrently (Settings: poller_max_workers, default 8) —
each device is an independent SNMP target, so device-level concurrency is
the safe axis: no single device gets hit harder than a serial poll would,
only the wall-clock time for the whole sweep drops. The concurrency is
strictly I/O: worker threads (_fetch_device_telemetry) only ever touch a
plain-data _DeviceSnapshot, never the ORM/db.session — all writes happen
back on the poll loop's own thread (_apply_fetch_result), so there's no
need for per-thread sessions or app contexts, and no risk of concurrent
SQLite writers.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
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
from sitewatch import job_log

log = logging.getLogger(__name__)

POLLER_JOB_ID = "poll_all_devices"

# Set for the duration of an actual sweep so get_poller_status() can tell
# "waiting for next cycle" apart from "sweep in progress right now" —
# APScheduler's job/next_run_time state alone doesn't capture that.
_currently_polling = False


def poll_all_devices():
    """One full sweep: every device's SNMP fetch runs concurrently (up to
    poller_max_workers at once — device-level parallelism, since each
    device is an independent target, see module docstring), but the
    resulting ORM writes/debounce/commit happen back on this thread, one
    device at a time as its fetch completes. Timing is recorded (Settings:
    last_poll_duration_seconds/last_poll_finished_at, shown on the Settings
    page) so the configured polling_interval_minutes can be checked against
    how long a sweep actually takes."""
    global _currently_polling
    _currently_polling = True
    try:
        started = time.monotonic()
        device_count = 0
        monitored_ids = _monitored_interface_ids()
        devices_by_id = {d.id: d for d in Device.query.all()}
        snapshots = {device_id: _snapshot_device(device, monitored_ids)
                     for device_id, device in devices_by_id.items()}

        max_workers = max(1, Setting.get_int("poller_max_workers"))
        job_id = job_log.current_job_id()  # propagated into worker threads below — see job_log.run_in_job
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(job_log.run_in_job, job_id, _fetch_device_telemetry, snapshot): device_id
                for device_id, snapshot in snapshots.items()
            }
            # as_completed, not submission order — a device finishes as soon as
            # its own fetch does, not gated on slower devices ahead of it.
            for future in as_completed(futures):
                if job_log.cancel_requested(job_id):
                    # Only affects futures the pool hasn't started on yet (at
                    # most max_workers are ever running at once) — those still
                    # run to completion/timeout, there's no safe way to abort a
                    # blocking SNMP call already in flight. But nothing beyond
                    # what's already running starts, which is the actual fix
                    # for "sweep grinding through every device while I'm on the
                    # wrong network": wait out what's in flight, then stop dead
                    # instead of working through the rest of the device list.
                    for f in futures:
                        f.cancel()
                    log.warning("Poll cycle stopped by user after %d device(s).", device_count)
                    break
                device = devices_by_id[futures[future]]
                _apply_fetch_result(device, future.result())
                # Recompute circuit/bundle state after every device, not once at
                # the end of the whole sweep — otherwise a link that goes down on
                # the very first device to finish wouldn't show as down until
                # every other device in the sweep finished too, which for a real
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
        log.info("Poll cycle finished in %.1fs (%d devices, up to %d concurrent)",
                  duration, device_count, max_workers)
    finally:
        _currently_polling = False


def _monitored_interface_ids():
    """Every interface id referenced by some circuit's endpoints — the only
    ones the regular poll loop touches. Computed once per sweep (or once
    per manual repoll) rather than per-device since it's the same set
    throughout a given poll pass. Includes bundles' optional LAG interfaces
    (lag_interface_a/b) alongside regular leaf endpoints — same 4-GET poll,
    no special-casing needed in _poll_device."""
    ids = set()
    columns = (Circuit.interface_a_id, Circuit.interface_b_id,
               Circuit.lag_interface_a_id, Circuit.lag_interface_b_id)
    for row in db.session.query(*columns).all():
        for iface_id in row:
            if iface_id is not None:
                ids.add(iface_id)
    return ids


@dataclass
class _IfaceSnapshot:
    """Plain-data mirror of the Interface columns telemetry.py/snmp.py
    actually read. Worker threads get one of these instead of the real ORM
    object — duck-types the same attributes, so telemetry.py needs no
    changes, but carries no session/engine reference a background thread
    could touch unsafely."""
    id: int
    if_index: int
    if_descr: str
    if_alias: str
    if_speed_bps: int
    last_counter_at: object
    last_in_octets: object
    last_out_octets: object


@dataclass
class _DeviceSnapshot:
    """Plain-data mirror of the Device columns telemetry.py/snmp.py read —
    credentials already decrypted (decrypt() is pure computation, safe off
    the main thread, but the Device ORM object and its session are not)."""
    id: int
    hostname: str
    mgmt_ip: str
    snmp_version: str
    snmp_community: str
    snmpv3_username: str
    snmpv3_auth_protocol: str
    snmpv3_auth_key: str
    snmpv3_priv_protocol: str
    snmpv3_priv_key: str
    interfaces: list


def _snapshot_device(device, monitored_ids):
    """Builds the plain-data snapshot passed to a worker thread. Must run on
    the main thread (touches the ORM/session-backed device.interfaces and
    the credential-decrypting properties)."""
    ifaces = [
        _IfaceSnapshot(
            id=i.id, if_index=i.if_index, if_descr=i.if_descr, if_alias=i.if_alias,
            if_speed_bps=i.if_speed_bps, last_counter_at=i.last_counter_at,
            last_in_octets=i.last_in_octets, last_out_octets=i.last_out_octets,
        )
        for i in device.interfaces if i.id in monitored_ids
    ]
    return _DeviceSnapshot(
        id=device.id, hostname=device.hostname, mgmt_ip=device.mgmt_ip,
        snmp_version=device.snmp_version, snmp_community=device.snmp_community,
        snmpv3_username=device.snmpv3_username, snmpv3_auth_protocol=device.snmpv3_auth_protocol,
        snmpv3_auth_key=device.snmpv3_auth_key, snmpv3_priv_protocol=device.snmpv3_priv_protocol,
        snmpv3_priv_key=device.snmpv3_priv_key, interfaces=ifaces,
    )


def _fetch_device_telemetry(snapshot):
    """Network I/O only — reachability + one GET per monitored interface.
    Safe to run in a worker thread: touches nothing but the snapshot and
    telemetry.py/snmp.py's own locals, no ORM/db.session access at all.
    Returns plain data; _apply_fetch_result does the actual ORM writes back
    on the main thread."""
    reachable = telemetry.check_reachable(snapshot)
    result = {"reachable": reachable, "interfaces": {}}
    if not reachable:
        return result  # interfaces stay at last-known values, same as before
    for iface in snapshot.interfaces:
        try:
            result["interfaces"][iface.id] = {"data": telemetry.poll_interface_counters(snapshot, iface)}
        except SnmpError as e:
            result["interfaces"][iface.id] = {"error": str(e)}
    return result


def _apply_fetch_result(device, result):
    """Writes a _fetch_device_telemetry result onto the real ORM objects.
    Main thread only — this is the only place poll_all_devices touches
    db.session for per-device state."""
    device.last_polled_at = datetime.utcnow()
    device.reachable = result["reachable"]
    if not device.reachable:
        return  # circuit state handled via device.reachable check below

    now = datetime.utcnow()
    ifaces_by_id = {i.id: i for i in device.interfaces}
    for iface_id, outcome in result["interfaces"].items():
        iface = ifaces_by_id.get(iface_id)
        if iface is None:
            continue  # interface removed between snapshot and apply — rare, nothing to write to

        if "error" in outcome:
            log.info("Skipping %s (ifIndex %s) on %s this cycle: %s",
                      iface.if_descr or f"ifIndex {iface.if_index}", iface.if_index, device.hostname,
                      outcome["error"])
            continue  # transient GET failure — leave stale, next poll will retry

        data = outcome["data"]
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


def _poll_device(device, monitored_ids=None):
    """Single-device poll, synchronous, no thread pool — used for the manual
    "Repoll" button (poll_device_now) where there's only one device and
    concurrency buys nothing. Built on the same snapshot/fetch/apply split
    poll_all_devices uses, so both paths share identical fetch/apply logic."""
    if monitored_ids is None:
        monitored_ids = _monitored_interface_ids()
    snapshot = _snapshot_device(device, monitored_ids)
    result = _fetch_device_telemetry(snapshot)
    _apply_fetch_result(device, result)


def _interface_pair_target_state(ifaces):
    devices = [i.device for i in ifaces]
    if any(not d.reachable for d in devices):
        return "unreachable"
    if any(i.admin_status == "down" for i in ifaces):
        return "admin_down"
    if any(i.oper_status == "down" for i in ifaces):
        return "down"
    return "up"


def _leaf_target_state(circuit):
    """What state a leaf circuit's raw telemetry says right now, before
    debounce is applied."""
    return _interface_pair_target_state([circuit.interface_a, circuit.interface_b])


def _lag_target_state(circuit):
    """Same idea as _leaf_target_state, but for a bundle's own optional
    LAG/port-channel interface pair (Circuit.lag_interface_a/b) rather than
    a leaf's endpoints. Returns None if the bundle doesn't have one
    configured — recompute_bundle_state then falls back to pure
    member-based rollup, exactly like before this existed."""
    if not circuit.lag_interface_a_id or not circuit.lag_interface_b_id:
        return None
    return _interface_pair_target_state([circuit.lag_interface_a, circuit.lag_interface_b])


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
        history = CircuitStatusHistory(circuit_id=circuit.id, state="down", started_at=datetime.utcnow())
        db.session.add(history)
        db.session.flush()  # need history.id assigned before it can go into incident_number
        prefix = Setting.get("incident_number_prefix")
        history.incident_number = f"{prefix}-{history.id:06d}"
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
    # lag_state only depends on the bundle's own interfaces (device
    # reachability + oper/admin status), never on other bundles, so it's
    # safe to compute once up front rather than inside the multi-pass loop.
    lag_states = {bundle.id: _lag_target_state(bundle) for bundle in bundles}
    # Multiple passes handle bundles-of-bundles without needing a full topo sort.
    for _ in range(3):
        for bundle in bundles:
            recompute_bundle_state(bundle, lag_state=lag_states[bundle.id])


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
        job_id = job_log.start_job("Poll cycle")
        job_log.run_job(job_id, poll_all_devices, app)

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
