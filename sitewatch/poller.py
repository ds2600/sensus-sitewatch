"""Background polling job. One pass = check every device's reachability,
GET each circuit-linked interface's counters/status, apply the
down-threshold debounce, roll bundle/site status up, and fire alerts on new
down events.

Full interface discovery (walk_interfaces) does NOT happen here — that's
triggered manually per device, and discovers every interface on the box.
This loop deliberately polls a much smaller set: only interfaces actually
wired into a circuit (see monitored_interface_ids). A device can easily
have hundreds of interfaces from a walk but only a handful ever become
circuits — polling all of them every cycle was most of what made poll
cycles slow for no operational benefit, since sitewatch has no use for
telemetry on an interface no circuit references.

Devices are polled concurrently (Settings: poller_max_workers, default 8) —
each device is an independent SNMP target, so device-level concurrency is
the safe axis: no single device gets hit harder than a serial poll would,
only the wall-clock time for the whole sweep drops. The concurrency is
strictly I/O: worker threads (telemetry.fetch_device_telemetry) only ever
touch a plain-data DeviceSnapshot, never the ORM/db.session — all writes
happen back on the poll loop's own thread (_apply_fetch_result), so
there's no need for per-thread sessions or app contexts, and no risk of
concurrent SQLite writers.

apply_probe_report() is a third entry point into the same apply/debounce/
alert pipeline poll_all_devices()/poll_device_now() use, for telemetry a
standalone probe (sitewatch/probe.py) fetched itself and POSTed back via
routes/probe_api.py — see that module for why some devices are polled
this way instead of directly.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import logging
import os
import time

from sitewatch.extensions import db, scheduler
from sitewatch.models import Device, Interface, Circuit, CircuitStatusHistory, Setting, Site, UtilizationRollup, Probe
from sitewatch import telemetry
from sitewatch.status import recompute_bundle_state, rollup_degree_status
from sitewatch.integrations.webhook_payload import (
    send_down_alerts, send_poller_backoff_alert, send_poller_recovered_alert,
    send_probe_stale_alert, send_probe_recovered_alert,
)
from sitewatch import job_log, audit_log

log = logging.getLogger(__name__)

POLLER_JOB_ID = "poll_all_devices"
STARTUP_POLL_JOB_ID = "poll_all_devices_startup"
AUDIT_PRUNE_JOB_ID = "audit_log_prune"
PROBE_WATCHDOG_JOB_ID = "probe_staleness_watchdog"

# Set for the duration of an actual sweep so get_poller_status() can tell
# "waiting for next cycle" apart from "sweep in progress right now" —
# APScheduler's job/next_run_time state alone doesn't capture that.
_currently_polling = False

# job_log id of the most recently started sweep (scheduled or manual
# "Poll all now"), kept around after it finishes too — not just while
# _currently_polling — so the Poller tab's "View poller log" button always
# has something to open. In-memory like the rest of job_log, so it resets
# on process restart.
_last_poll_job_id = None


def poll_all_devices():
    """One full sweep: every device's SNMP fetch runs concurrently (up to
    poller_max_workers at once — device-level parallelism, since each
    device is an independent target, see module docstring), but the
    resulting ORM writes/debounce/commit happen back on this thread, one
    device at a time as its fetch completes. Timing is recorded (Settings:
    last_poll_duration_seconds/last_poll_finished_at, shown on the Settings
    page) so the configured polling_interval_minutes can be checked against
    how long a sweep actually takes."""
    global _currently_polling, _last_poll_job_id
    _currently_polling = True
    try:
        started = time.monotonic()
        device_count = 0
        reachable_count = 0
        # Every circuit that goes down anywhere in this sweep lands here,
        # then one grouped webhook goes out at the very end — never one
        # per device/circuit. See webhook_payload.py's module docstring.
        alert_batch = []
        cancelled = False
        monitored_ids = monitored_interface_ids()
        # probe_id IS NULL only — a probe-owned device is polled by that
        # probe instead (see apply_probe_report), never by this sweep too.
        devices_by_id = {d.id: d for d in Device.query.filter_by(probe_id=None).all()}
        snapshots = {device_id: _snapshot_device(device, monitored_ids)
                     for device_id, device in devices_by_id.items()}

        max_workers = max(1, Setting.get_int("poller_max_workers"))
        job_id = job_log.current_job_id()  # propagated into worker threads below — see job_log.run_in_job
        _last_poll_job_id = job_id
        # Total wasn't known back when start_job() handed out job_id (device
        # count depends on the query above) — set_total() fills it in now so
        # the Tail Modal/Poller tab can show a completed/total counter.
        job_log.set_total(job_id, len(devices_by_id))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(job_log.run_in_job, job_id, telemetry.fetch_device_telemetry, snapshot): device_id
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
                    cancelled = True
                    break
                device = devices_by_id[futures[future]]
                _apply_fetch_result(device, future.result())
                if device.reachable:
                    reachable_count += 1
                # Recompute circuit/bundle state after every device, not once at
                # the end of the whole sweep — otherwise a link that goes down on
                # the very first device to finish wouldn't show as down until
                # every other device in the sweep finished too, which for a real
                # multi-device deployment can be a long wait for what should be
                # near-real-time. Commit here as well, both so the fresher state
                # is actually visible to other requests right away and so SQLite
                # doesn't hold one write transaction open for the whole sweep
                # (see the "database is locked" fix — same reasoning applies).
                _recompute_all_circuit_states(alert_batch)
                db.session.commit()
                device_count += 1
                job_log.set_progress(job_id, device_count)

        duration = time.monotonic() - started
        Setting.set("last_poll_duration_seconds", f"{duration:.1f}")
        Setting.set("last_poll_finished_at", datetime.utcnow().isoformat())
        if not cancelled:
            _apply_failure_backoff(device_count, reachable_count)
        db.session.commit()
        send_down_alerts(alert_batch)
        log.info("Poll cycle finished in %.1fs (%d devices, up to %d concurrent)",
                  duration, device_count, max_workers)
    finally:
        _currently_polling = False


def _apply_failure_backoff(device_count, reachable_count):
    """Circuit breaker for a network that's entirely down: once
    poller_failure_threshold consecutive sweeps in a row see zero
    reachable devices, backs the schedule off to poller_backoff_minutes
    instead of continuing to hammer every device at the normal cadence,
    and fires one Google Chat alert on the transition into backoff.
    Recovers automatically — back to polling_interval_minutes, one more
    alert — the moment any later sweep sees even a single device reachable
    again. A cancelled sweep (see the `cancelled` flag in poll_all_devices)
    never reaches this function at all: an incomplete sweep isn't a
    reliable signal that every device actually failed. An empty device
    list is also skipped — no devices means nothing to fail, not an
    outage."""
    if device_count == 0:
        return
    was_backed_off = Setting.get("poller_backed_off", "0") == "1"
    if reachable_count == 0:
        failures = Setting.get_int("poller_consecutive_failures", 0) + 1
        Setting.set("poller_consecutive_failures", failures)
        threshold = Setting.get_int("poller_failure_threshold")
        if not was_backed_off and failures >= threshold:
            Setting.set("poller_backed_off", "1")
            reschedule_poller(Setting.get_int("poller_backoff_minutes"))
            log.warning("Poller backed off after %d consecutive fully-failed cycles.", failures)
            send_poller_backoff_alert(failures)
    else:
        Setting.set("poller_consecutive_failures", 0)
        if was_backed_off:
            Setting.set("poller_backed_off", "0")
            reschedule_poller(Setting.get_int("polling_interval_minutes"))
            log.info("Poller recovered — a device responded again. Resuming normal interval.")
            send_poller_recovered_alert()


def monitored_interface_ids():
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


def _snapshot_device(device, monitored_ids):
    """Builds the plain-data snapshot passed to a worker thread. Must run on
    the main thread (touches the ORM/session-backed device.interfaces and
    the credential-decrypting properties)."""
    ifaces = [
        telemetry.IfaceSnapshot(
            id=i.id, if_index=i.if_index, if_descr=i.if_descr, if_alias=i.if_alias,
            if_speed_bps=i.if_speed_bps, last_counter_at=i.last_counter_at,
            last_in_octets=i.last_in_octets, last_out_octets=i.last_out_octets,
        )
        for i in device.interfaces if i.id in monitored_ids
    ]
    return telemetry.DeviceSnapshot(
        id=device.id, hostname=device.hostname, mgmt_ip=device.mgmt_ip,
        snmp_version=device.snmp_version, snmp_community=device.snmp_community,
        snmpv3_username=device.snmpv3_username, snmpv3_auth_protocol=device.snmpv3_auth_protocol,
        snmpv3_auth_key=device.snmpv3_auth_key, snmpv3_priv_protocol=device.snmpv3_priv_protocol,
        snmpv3_priv_key=device.snmpv3_priv_key, interfaces=ifaces,
    )


def _apply_fetch_result(device, result):
    """Writes a telemetry.fetch_device_telemetry-shaped result onto the
    real ORM objects — called for a directly-polled device's own fetch
    result, or for a probe-reported result of the same shape (see
    apply_probe_report). Main thread only — the only place device state
    gets written from raw telemetry."""
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
                _update_utilization_rollup(iface, now)

        iface.last_in_octets = data["in_octets"]
        iface.last_out_octets = data["out_octets"]
        iface.last_counter_at = now
        iface.oper_status = data["oper_status"]
        iface.admin_status = data["admin_status"]
        iface.last_polled_at = now


def _update_utilization_rollup(iface, now):
    """Feeds this poll's freshly-computed last_in_bps/last_out_bps into the
    current hour's UtilizationRollup row for this interface, creating that
    row if this is the hour's first sample. See UtilizationRollup's
    docstring (models.py) for why this happens here, incrementally, rather
    than via a separate scheduled aggregation job."""
    period_start = now.replace(minute=0, second=0, microsecond=0)
    row = UtilizationRollup.query.filter_by(
        interface_id=iface.id, period_type="hourly", period_start=period_start
    ).first()
    if row is None:
        # sample_count=0 explicitly — the column's default=0 only applies
        # at INSERT flush time, not to this in-memory object yet, and it's
        # read immediately below.
        row = UtilizationRollup(interface_id=iface.id, period_type="hourly", period_start=period_start,
                                 avg_in_bps=0.0, avg_out_bps=0.0, peak_in_bps=0.0, peak_out_bps=0.0,
                                 sample_count=0)
        db.session.add(row)

    count = row.sample_count
    row.avg_in_bps = (row.avg_in_bps * count + iface.last_in_bps) / (count + 1)
    row.avg_out_bps = (row.avg_out_bps * count + iface.last_out_bps) / (count + 1)
    row.peak_in_bps = max(row.peak_in_bps, iface.last_in_bps)
    row.peak_out_bps = max(row.peak_out_bps, iface.last_out_bps)
    row.sample_count = count + 1


def _poll_device(device, monitored_ids=None):
    """Single-device poll, synchronous, no thread pool — used for the manual
    "Repoll" button (poll_device_now) where there's only one device and
    concurrency buys nothing. Built on the same snapshot/fetch/apply split
    poll_all_devices uses, so both paths share identical fetch/apply logic."""
    if monitored_ids is None:
        monitored_ids = monitored_interface_ids()
    snapshot = _snapshot_device(device, monitored_ids)
    result = telemetry.fetch_device_telemetry(snapshot)
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


def _apply_debounce(circuit, target_state, alert_batch):
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
            _transition(circuit, "down", alert_batch)
    else:  # up
        circuit.consecutive_success_count += 1
        circuit.consecutive_fail_count = 0
        if circuit.current_state == "down" and circuit.consecutive_success_count >= threshold:
            _transition(circuit, "up", alert_batch)
        elif circuit.current_state in ("admin_down", "unreachable"):
            circuit.current_state = "up"


def _transition(circuit, new_state, alert_batch):
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
            # Not sent here — appended to the caller's batch so every
            # circuit that goes down in the same poll cycle/repoll action
            # ends up in one grouped webhook instead of one apiece. See
            # webhook_payload.py's module docstring.
            alert_batch.append(circuit)
    elif old_state == "down" and new_state == "up":
        open_record = (CircuitStatusHistory.query
                       .filter_by(circuit_id=circuit.id, cleared_at=None)
                       .order_by(CircuitStatusHistory.started_at.desc()).first())
        if open_record:
            open_record.cleared_at = datetime.utcnow()


def _recompute_all_circuit_states(alert_batch):
    # Leaf circuits first (bottom-up), then bundles, since bundle state
    # depends on children already being current.
    leaves = [c for c in Circuit.query.all() if not c.is_bundle]
    for circuit in leaves:
        target = _leaf_target_state(circuit)
        _apply_debounce(circuit, target, alert_batch)

    bundles = [c for c in Circuit.query.all() if c.is_bundle]
    # lag_state only depends on the bundle's own interfaces (device
    # reachability + oper/admin status), never on other bundles, so it's
    # safe to compute once up front rather than inside the multi-pass loop.
    lag_states = {bundle.id: _lag_target_state(bundle) for bundle in bundles}
    # Multiple passes handle bundles-of-bundles without needing a full topo sort.
    for _ in range(3):
        for bundle in bundles:
            recompute_bundle_state(bundle, lag_state=lag_states[bundle.id])


def poll_device_now(device, alert_batch=None):
    """Manual single-device repoll — the device detail page's "Repoll" button.
    Runs the same telemetry + debounce logic as a scheduled pass, just scoped
    to one device's reachability/interfaces instead of waiting for the next
    cycle. Circuit/bundle states are recomputed across the board afterward
    since this device's interfaces may affect circuits touching other sites.

    alert_batch: pass a shared list when calling this in a loop over
    several devices for one logical action (repoll_circuit, repoll-all-
    unreachable) so everything that goes down across the whole loop sends
    one grouped webhook instead of one per device — the caller flushes it
    once after the loop via send_down_alerts(). Left as None for a
    standalone single-device repoll, which then sends its own grouped
    alert here covering however many circuits that one device touches."""
    log.info("Repolling %s (%s) — %d known interface(s)...",
              device.hostname, device.mgmt_ip, len(device.interfaces))
    own_batch = alert_batch if alert_batch is not None else []
    _poll_device(device)
    _recompute_all_circuit_states(own_batch)
    db.session.commit()
    if alert_batch is None:
        send_down_alerts(own_batch)
    log.info("Repoll of %s complete: %s.", device.hostname, "reachable" if device.reachable else "unreachable")


def apply_probe_report(probe, reports):
    """Feeds a probe's batch of already-fetched telemetry through the same
    apply/debounce/alert path poll_all_devices()/poll_device_now() use for
    directly-polled devices — the acquisition method differs (a standalone
    probe fetched this itself, over SNMP it has local access to that the
    main server doesn't — see sitewatch/probe.py), everything downstream
    is identical. `reports`: list of {"device_id", "reachable",
    "interfaces"} dicts, each the same shape telemetry.fetch_device_
    telemetry() returns for one device — routes/probe_api.py's report
    endpoint is the only caller. Ownership (device.probe_id == probe.id)
    is checked by the caller before this runs; a report for a device this
    probe doesn't own is silently skipped here too, as a second line of
    defense.

    Also bumps probe.last_seen_at — the staleness watchdog's signal this
    probe is alive, regardless of whether anything it reported actually
    changed state — and clears Probe.stale + fires the one recovery alert
    if the watchdog had already marked this probe stale (see
    check_probe_staleness)."""
    probe.last_seen_at = datetime.utcnow()
    was_stale = probe.stale
    probe.stale = False
    alert_batch = []
    for entry in reports:
        device = Device.query.get(entry["device_id"])
        if device is None or device.probe_id != probe.id:
            continue
        _apply_fetch_result(device, entry)
    _recompute_all_circuit_states(alert_batch)
    db.session.commit()
    send_down_alerts(alert_batch)
    if was_stale:
        send_probe_recovered_alert(probe)


def check_probe_staleness():
    """Runs every minute (see start_poller) — the inverse of poll_all_
    devices'/`_apply_failure_backoff`'s circuit breaker: instead of
    detecting a live poller failing to reach devices, this detects a
    probe going quiet entirely (no report in probe_stale_after_minutes).
    Marks that probe's devices unreachable and fires one Google Chat
    alert on the transition into staleness — Probe.stale is what makes
    this a one-time transition rather than re-alerting every pass while
    a probe stays down. Recovery (clearing Probe.stale, the matching
    recovery alert) happens the moment that probe's next report actually
    arrives — see apply_probe_report."""
    threshold_minutes = Setting.get_int("probe_stale_after_minutes")
    cutoff = datetime.utcnow() - timedelta(minutes=threshold_minutes)
    for probe in Probe.query.all():
        last_seen = probe.last_seen_at or probe.created_at
        if probe.stale or last_seen is None or last_seen >= cutoff:
            continue
        probe.stale = True
        for device in probe.devices:
            device.reachable = False
        alert_batch = []
        _recompute_all_circuit_states(alert_batch)
        db.session.commit()
        send_down_alerts(alert_batch)
        send_probe_stale_alert(probe)
        log.warning("Probe %s marked stale (no report in over %d minutes).", probe.name, threshold_minutes)


def start_poller(app):
    with app.app_context():
        interval = Setting.query.get("polling_interval_minutes")
        minutes = int(interval.value) if interval else 2
        enabled = Setting.get("poller_enabled", "1") != "0"
        poll_on_startup = Setting.get("poll_on_startup", "0") == "1"

    def job():
        job_id = job_log.start_job("Poll cycle")
        job_log.run_job(job_id, poll_all_devices, app)

    scheduler.add_job(job, "interval", minutes=minutes, id=POLLER_JOB_ID, replace_existing=True)

    def audit_prune():
        job_id = job_log.start_job("Audit log prune")
        job_log.run_job(job_id, audit_log.prune_old_entries, app)

    # Fixed off-peak time, not an "interval, days=1" trigger — that would
    # drift to whatever time-of-day the process last happened to restart
    # at instead of running at a consistent, predictable hour.
    scheduler.add_job(audit_prune, "cron", hour=3, minute=30, id=AUDIT_PRUNE_JOB_ID, replace_existing=True)

    def probe_watchdog():
        with app.app_context():
            check_probe_staleness()

    # Every minute regardless of poller_enabled/pause state below — probe
    # staleness is about whether a REMOTE process is still checking in,
    # unrelated to whether this server's own in-process sweep is running.
    scheduler.add_job(probe_watchdog, "interval", minutes=1, id=PROBE_WATCHDOG_JOB_ID, replace_existing=True)
    scheduler.start()
    if not enabled:
        # Restore whatever start/stop state was last set from the UI —
        # otherwise a stopped poller would silently come back on restart.
        scheduler.pause_job(POLLER_JOB_ID)
    elif poll_on_startup:
        # IntervalTrigger's first fire is a full interval out, not
        # immediate (confirmed against APScheduler's own
        # get_next_fire_time) — after a long outage that means staring at
        # stale data for up to polling_interval_minutes after the app is
        # already back up. This one-off "date" job runs once, shortly
        # after startup, independent of the regular interval schedule
        # (which keeps ticking on its own normal cadence regardless).
        scheduler.add_job(job, "date", run_date=datetime.now() + timedelta(seconds=15),
                           id=STARTUP_POLL_JOB_ID, replace_existing=True)


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
    when the next cycle is due instead of a vague "waiting". job_id/progress
    identify the most recently started sweep's job_log entry (running or
    already finished), so the UI can offer a "View poller log" button and,
    while state is "polling", show real completed/total progress instead of
    the next-run countdown running past zero and calling an in-progress
    sweep "overdue". `backed_off`/`consecutive_failures` reflect the
    failure-backoff circuit breaker (see _apply_failure_backoff) — `state`
    itself stays waiting/polling either way, since backoff only changes the
    cadence, not what's actually happening right now. `last_poll_finished_at`
    is the dashboard's "Last updated" display — set regardless of state, so
    a disabled/stopped poller still shows how stale the data actually is
    instead of going blank."""
    progress = None
    if _last_poll_job_id:
        job_entry = job_log.get_job(_last_poll_job_id)
        if job_entry:
            progress = {"completed": job_entry["completed"], "total": job_entry["total"]}
    backed_off = Setting.get("poller_backed_off", "0") == "1"
    consecutive_failures = Setting.get_int("poller_consecutive_failures", 0)
    last_poll_finished_at = Setting.get("last_poll_finished_at")

    if not poller_enabled_for_process():
        return {"state": "disabled", "label": "Not running in this process", "active": False,
                "next_run_at": None, "interval_minutes": None,
                "job_id": _last_poll_job_id, "progress": progress,
                "backed_off": backed_off, "consecutive_failures": consecutive_failures,
                "last_poll_finished_at": last_poll_finished_at}
    if not scheduler.running:
        return {"state": "stopped", "label": "Stopped", "active": False,
                "next_run_at": None, "interval_minutes": None,
                "job_id": _last_poll_job_id, "progress": progress,
                "backed_off": backed_off, "consecutive_failures": consecutive_failures,
                "last_poll_finished_at": last_poll_finished_at}
    job = scheduler.get_job(POLLER_JOB_ID)
    interval_minutes = (Setting.get_int("poller_backoff_minutes") if backed_off
                         else Setting.get_int("polling_interval_minutes"))
    if job is None or job.next_run_time is None:
        return {"state": "stopped", "label": "Stopped", "active": False,
                "next_run_at": None, "interval_minutes": interval_minutes,
                "job_id": _last_poll_job_id, "progress": progress,
                "backed_off": backed_off, "consecutive_failures": consecutive_failures,
                "last_poll_finished_at": last_poll_finished_at}
    # APScheduler's next_run_time is tz-aware in the local system timezone;
    # normalize to UTC to match last_poll_finished_at's naive-UTC format
    # elsewhere on the Settings page.
    next_run_at = job.next_run_time.astimezone(timezone.utc).isoformat()
    backoff_suffix = " (backed off)" if backed_off else ""
    if _currently_polling:
        return {"state": "polling", "label": "Polling now" + backoff_suffix, "active": True,
                "next_run_at": next_run_at, "interval_minutes": interval_minutes,
                "job_id": _last_poll_job_id, "progress": progress,
                "backed_off": backed_off, "consecutive_failures": consecutive_failures,
                "last_poll_finished_at": last_poll_finished_at}
    return {"state": "waiting", "label": "Waiting for next cycle" + backoff_suffix, "active": True,
            "next_run_at": next_run_at, "interval_minutes": interval_minutes,
            "job_id": _last_poll_job_id, "progress": progress,
            "backed_off": backed_off, "consecutive_failures": consecutive_failures,
            "last_poll_finished_at": last_poll_finished_at}
