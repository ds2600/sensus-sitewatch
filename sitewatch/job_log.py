"""In-memory log capture for background jobs — both the scheduled poll
cycle and one-off UI-triggered jobs (device walk/repoll) — so the browser
can watch real progress instead of just spinning until a redirect, and so
there's a place to look at what the background poller has actually been
doing (Settings page's "Background activity" list).

Jobs are kept in memory only (no DB, no persistence across restarts) and
capped by count, oldest evicted first — this is "recent activity", not an
audit log. CircuitStatusHistory/AlertMute already cover the durable record
of what a poll found.
"""
import logging
import threading
import time
import uuid
from collections import deque
from datetime import datetime

_MAX_JOBS = 300
_MAX_LINES_PER_JOB = 2000

log = logging.getLogger(__name__)

_jobs = {}
_lock = threading.Lock()
_current_job_id = threading.local()


class JobLogHandler(logging.Handler):
    """Routes log records to whichever job the CURRENT THREAD is running,
    via _current_job_id — set for the life of run_job()'s call to fn(). Log
    calls anywhere in the walk/repoll/telemetry/snmp code path pick this up
    automatically since they propagate up to the "sitewatch" logger this
    handler is attached to; nothing below has to know a job is watching."""

    def emit(self, record):
        job_id = getattr(_current_job_id, "value", None)
        if job_id is None:
            return
        _append(job_id, self.format(record))


def _append(job_id, line):
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job["lines"].append(line)


def install():
    handler = JobLogHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
    sitewatch_logger = logging.getLogger("sitewatch")
    sitewatch_logger.addHandler(handler)

    # Flask names app.logger after the app's import name, which is
    # "sitewatch" here — so the handler just added above is now attached to
    # the same logger Flask itself logs unhandled request exceptions
    # through. Python's logging only falls back to its default stderr
    # handler when a logger has NO handler anywhere in its chain; since we
    # just gave it one (which silently no-ops unless a job is watching —
    # see JobLogHandler.emit), a real 500 during normal request handling
    # would otherwise vanish with no trace anywhere. Add a plain
    # WARNING+ stderr handler back explicitly so that can't happen.
    if not any(isinstance(h, logging.StreamHandler) for h in sitewatch_logger.handlers):
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(logging.WARNING)
        sitewatch_logger.addHandler(stream_handler)

    if sitewatch_logger.level == logging.NOTSET or sitewatch_logger.level > logging.INFO:
        sitewatch_logger.setLevel(logging.INFO)


def start_job(label, total=None):
    """total: for a job that repeats a step over a known-size batch (e.g.
    repoll-all-unreachable), the number of items — drives the Tail Modal's
    completed/total counter via set_progress()/get_job(). None (default)
    for an ordinary single-step job; the modal shows no counter then."""
    with _lock:
        while len(_jobs) >= _MAX_JOBS:
            _jobs.pop(next(iter(_jobs)))
        job_id = uuid.uuid4().hex
        _jobs[job_id] = {
            "label": label,
            "lines": deque(maxlen=_MAX_LINES_PER_JOB),
            "done": False,
            "success": None,
            "cancelled": False,
            "cancel_requested": False,
            "error": None,
            "started_at": time.time(),
            "finished_at": None,
            "completed": 0,
            "total": total,
        }
    return job_id


def request_cancel(job_id):
    """The Tail Modal's Stop button. Purely a flag — nothing here forcibly
    kills a thread (Python can't do that safely) or interrupts a blocking
    SNMP call already in flight. What it DOES do: any loop that checks
    cancel_requested() between steps (poll_all_devices between devices,
    repoll_unreachable between devices, repoll_circuit between its 1-2
    devices) stops moving on to the next one, and poll_all_devices also
    cancels every not-yet-started device fetch outright. For the exact
    scenario this exists for — a sweep grinding through many SNMP timeouts
    because you're on the wrong network — that turns "wait out the whole
    sweep" into "wait out whatever's already in flight, then stop"."""
    with _lock:
        job = _jobs.get(job_id)
        if job is not None and not job["done"]:
            job["cancel_requested"] = True


def cancel_requested(job_id=None):
    """job_id omitted: checks the CALLING THREAD's own job (same
    _current_job_id trick as current_job_id()) — lets polling code deep in
    poller.py/settings.py check without threading job_id through every
    call. Pass job_id explicitly when checking from outside that thread
    (e.g. poll_all_devices checking on behalf of its worker-pool futures)."""
    jid = job_id if job_id is not None else getattr(_current_job_id, "value", None)
    if jid is None:
        return False
    with _lock:
        job = _jobs.get(jid)
        return bool(job and job["cancel_requested"])


def run_in_background(job_id, work, app):
    """Starts work() (no args) on a daemon thread with `app` bound and its
    log output routed into job_id (see run_job). The caller returns job_id
    to the browser immediately — the Tail Modal polls for progress rather
    than the request blocking for however long work() takes. Shared by
    every route that kicks off a walk/repoll/repoll-sweep; don't duplicate
    this thread-spawn in the route modules themselves."""
    threading.Thread(target=run_job, args=(job_id, work, app), daemon=True).start()


def set_progress(job_id, completed):
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job["completed"] = completed


def set_total(job_id, total):
    """Companion to set_progress(), for a job whose batch size isn't known
    until after start_job() already handed back a job_id — poll_all_devices
    doesn't know the device count until its own query runs, so it can't
    pass total= to start_job() up front the way repoll-all-unreachable
    does."""
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job["total"] = total


def run_job(job_id, fn, app):
    """Runs fn() (no args) on the calling thread with app context pushed and
    this thread's log records routed into job_id's buffer. Call this as a
    thread's target for UI-triggered jobs, or directly for the scheduled
    poll job (which already runs on APScheduler's own background thread).

    fn() doesn't need to raise anything special to report a stop — it just
    needs to check cancel_requested() at its own safe points and return
    early like normal completion. Whether that happened is checked here,
    after fn() returns, so every job type gets consistent "stopped" vs
    "completed" bookkeeping for free instead of each fn() having to report
    it itself."""
    _current_job_id.value = job_id
    try:
        with app.app_context():
            try:
                fn()
                if cancel_requested(job_id):
                    _finish(job_id, success=False, cancelled=True, error="Stopped by user.")
                else:
                    _finish(job_id, success=True)
            except Exception as e:
                log.exception("Background job failed")
                _finish(job_id, success=False, error=str(e))
    finally:
        _current_job_id.value = None


def current_job_id():
    """The job the CALLING thread is running, if any — for code that wants
    to hand its job context to a worker thread it's about to spawn (see
    run_in_job)."""
    return getattr(_current_job_id, "value", None)


def run_in_job(job_id, fn, *args, **kwargs):
    """Runs fn(*args, **kwargs) on the calling thread with its log records
    routed into job_id's buffer. _current_job_id is per-OS-thread, so a
    thread pool's worker threads don't inherit it from whatever thread
    called run_job() — poller.py's device-concurrency pool uses this to
    make SNMP GET/WALK log lines from worker threads still show up in the
    poll cycle's/repoll's live log tail instead of silently vanishing."""
    _current_job_id.value = job_id
    try:
        return fn(*args, **kwargs)
    finally:
        _current_job_id.value = None


def log_line(job_id, message):
    _append(job_id, message)


def finish_job(job_id, success, error=None):
    """Public counterpart to run_job()'s automatic finish, for a job that
    isn't run-to-completion on one thread — routes/probe_api.py starts a
    Walk/Repoll job when a probe-owned device's button is clicked, then
    finishes it here from a LATER, unrelated request once the probe
    actually reports the result back. Every other Tail Modal job finishes
    itself via run_job(); this is the one exception."""
    _finish(job_id, success=success, error=error)


def _finish(job_id, success, error=None, cancelled=False):
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job["done"] = True
            job["success"] = success
            job["cancelled"] = cancelled
            job["error"] = error
            job["finished_at"] = time.time()


def get_job(job_id, since=0):
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        lines = list(job["lines"])
        new_lines = lines[since:]
        elapsed = (job["finished_at"] or time.time()) - job["started_at"]
        return {
            "label": job["label"],
            "lines": new_lines,
            "next_index": since + len(new_lines),
            "done": job["done"],
            "success": job["success"],
            "cancelled": job["cancelled"],
            "error": job["error"],
            "elapsed": round(elapsed, 1),
            "completed": job["completed"],
            "total": job["total"],
        }


def list_jobs(limit=30):
    """Newest-first summaries for the Settings page's activity list — no
    log lines, just enough to show what ran, when, and how it went."""
    with _lock:
        items = list(_jobs.items())[-limit:][::-1]
        return [
            {
                "id": job_id,
                "label": job["label"],
                "started_at": datetime.utcfromtimestamp(job["started_at"]).isoformat(),
                "done": job["done"],
                "success": job["success"],
                "cancelled": job["cancelled"],
                "elapsed": round((job["finished_at"] or time.time()) - job["started_at"], 1),
            }
            for job_id, job in items
        ]
