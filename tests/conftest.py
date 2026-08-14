"""Shared pytest fixtures. Every test gets its own throwaway SQLite file
(not :memory: — Flask-SQLAlchemy's connection pooling doesn't play well
with a shared in-memory DB across the requests a test client makes) and
SITEWATCH_SIMULATE=1 so nothing here ever touches real SNMP/network
device access. See README/CLAUDE.md section on running locally for what
these env vars normally come from (.env) — tests set fixed values instead
so a run never depends on (or clobbers) a developer's real .env.
"""
import os
import tempfile

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-not-for-real-use")
os.environ["SITEWATCH_SIMULATE"] = "1"

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "test-admin-password"


@pytest.fixture
def app():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["DATABASE_PATH"] = db_path

    from sitewatch import create_app
    from sitewatch.extensions import db
    from sitewatch.models import User, Setting

    flask_app = create_app()
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    with flask_app.app_context():
        admin = User(username=ADMIN_USERNAME)
        admin.set_password(ADMIN_PASSWORD)
        db.session.add(admin)
        Setting.seed_defaults()
        db.session.commit()

    yield flask_app

    try:
        os.remove(db_path)
    except OSError:
        pass


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin_client(client):
    """A test client already logged in as the seeded admin user — most
    route tests want this, not a bare client, since nearly every mutating
    route is @admin_required."""
    client.post("/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}, follow_redirects=True)
    return client
