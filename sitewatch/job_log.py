"""In-memory log capture for one-off background jobs (device walk/repoll)
triggered from the UI, so the browser can tail real progress in a modal
instead of just spinning until a redirect. Not a task queue — jobs run on a
plain daemon thread and this module only exists to buffer the log lines
that thread produces, keyed by a job id, for the browser to poll.

Jobs are kept in memory only (no DB, no persistence across restarts) and
pruned after a few minutes — this is "tail what's happening right now",
not an audit log. CircuitStatusHistory/AlertMute already cover the durable
record of what a poll found.
"""
import logging
import threading
import time
import uuid
from collections import deque

_JOB_TTL_SECONDS = 300
_MAX_LINES_PER_JOB = 2000

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
    if sitewatch_logger.level == logging.NOTSET or sitewatch_logger.level > logging.INFO:
        sitewatch_logger.setLevel(logging.INFO)


def _prune_expired():
    cutoff = time.time() - _JOB_TTL_SECONDS
    for job_id in [jid for jid, j in _jobs.items() if j["finished_at"] and j["finished_at"] < cutoff]:
        del _jobs[job_id]


def start_job(label):
    with _lock:
        _prune_expired()
        job_id = uuid.uuid4().hex
        _jobs[job_id] = {
            "label": label,
            "lines": deque(maxlen=_MAX_LINES_PER_JOB),
            "done": False,
            "success": None,
            "error": None,
            "started_at": time.time(),
            "finished_at": None,
        }
    return job_id


def run_job(job_id, fn, app):
    """Runs fn() (no args) on the calling thread with app context pushed and
    this thread's log records routed into job_id's buffer. Call this as a
    thread's target — it's synchronous/blocking by design."""
    _current_job_id.value = job_id
    try:
        with app.app_context():
            try:
                fn()
                _finish(job_id, success=True)
            except Exception as e:
                _append(job_id, f"ERROR: {e}")
                _finish(job_id, success=False, error=str(e))
    finally:
        _current_job_id.value = None


def log_line(job_id, message):
    _append(job_id, message)


def _finish(job_id, success, error=None):
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job["done"] = True
            job["success"] = success
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
            "error": job["error"],
            "elapsed": round(elapsed, 1),
        }
