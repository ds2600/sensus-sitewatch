"""Shared extension instances. Imported by __init__.py and models to avoid circular imports."""
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()
login_manager = LoginManager()
scheduler = BackgroundScheduler(daemon=True)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """The poller runs on a background thread in the same process as the web
    app, sharing one SQLite file. Python's sqlite3 default busy timeout is
    only 5s, but a poll sweep can legitimately hold a write transaction
    longer than that (several devices, each doing a few SNMP GETs) — without
    this, a device save landing mid-sweep fails with "database is locked"
    instead of just waiting. WAL also lets reads proceed without blocking on
    that writer. No-ops on non-SQLite connections (module 3rd-party
    integrations don't use this DB, but keep this defensive)."""
    if type(dbapi_connection).__module__ != "sqlite3":
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()
