"""Per-target rate limit for anything that triggers real SNMP traffic to a
device (walk, repoll, the circuit-page repoll, the repoll-all sweep). One
60s cooldown bucket per device id — a Walk and a Repoll on the same device
share the window (both hit that device's network path), but a different
device is unaffected. Server-side and authoritative; job-modal.js also
disables the clicked button client-side as a courtesy, but this is what
actually stops a click from starting a job.

In-memory only, like job_log.py's job store — a process restart clears
it, which is fine, the cost of a false negative here is a person clicking
too fast right after a restart, not a real network-hammering risk.
"""
import threading
import time

COOLDOWN_SECONDS = 60

_last_triggered = {}
_lock = threading.Lock()


def remaining(target_id):
    """Peek only, no side effect — seconds left on target_id's cooldown, or
    None if it's clear. For a caller that needs to check SEVERAL targets
    before committing to any of them (repoll_circuit: reject the whole
    action if any one device is still cooling down, don't half-start it)."""
    now = time.monotonic()
    with _lock:
        last = _last_triggered.get(target_id)
        if last is not None and now - last < COOLDOWN_SECONDS:
            return round(COOLDOWN_SECONDS - (now - last))
        return None


def start(target_id):
    with _lock:
        _last_triggered[target_id] = time.monotonic()


def check_and_start(target_id):
    """remaining() + start() combined, for the common single-target case
    (walk_device/repoll_device) where there's nothing to pre-check first —
    returns None if OK to proceed (and records this attempt), or the whole
    number of seconds still remaining otherwise."""
    now = time.monotonic()
    with _lock:
        last = _last_triggered.get(target_id)
        if last is not None and now - last < COOLDOWN_SECONDS:
            return round(COOLDOWN_SECONDS - (now - last))
        _last_triggered[target_id] = now
        return None
