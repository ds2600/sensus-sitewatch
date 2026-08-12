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


def start_job(label):
    with _lock:
        while len(_jobs) >= _MAX_JOBS:
            _jobs.pop(next(iter(_jobs)))
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
    thread's target for UI-triggered jobs, or directly for the scheduled
    poll job (which already runs on APScheduler's own background thread)."""
    _current_job_id.value = job_id
    try:
        with app.app_context():
            try:
                fn()
                _finish(job_id, success=True)
            except Exception as e:
                log.exception("Background job failed")
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
                "elapsed": round((job["finished_at"] or time.time()) - job["started_at"], 1),
            }
            for job_id, job in items
        ]
