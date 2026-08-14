from sitewatch.extensions import db
from sitewatch.models import Site, AuditLog
from tests.factories import make_site, make_device


def test_add_site(app, admin_client):
    resp = admin_client.post("/sites/add", data={
        "name": "New Site", "lat": "10.0", "lon": "-20.0", "site_type": "site",
    }, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        site = Site.query.filter_by(name="New Site").first()
        assert site is not None
        entry = AuditLog.query.filter_by(object_type="Site", action="create").first()
        assert entry is not None
        assert entry.object_id == site.id
        assert entry.username == "admin"


def test_edit_site_records_diff(app, admin_client):
    with app.app_context():
        site = make_site(name="Before Name")
        db.session.commit()
        site_id = site.id

    admin_client.post(f"/sites/{site_id}/edit", data={
        "name": "After Name", "lat": "10.0", "lon": "-20.0", "site_type": "site",
    }, follow_redirects=True)

    with app.app_context():
        site = Site.query.get(site_id)
        assert site.name == "After Name"
        entry = AuditLog.query.filter_by(object_type="Site", object_id=site_id, action="update").first()
        assert entry is not None
        assert "Before Name" in entry.details
        assert "After Name" in entry.details


def test_edit_site_with_no_changes_is_not_audited(app, admin_client):
    with app.app_context():
        site = make_site(name="Unchanged")
        db.session.commit()
        site_id, name, lat, lon = site.id, site.name, site.lat, site.lon

    admin_client.post(f"/sites/{site_id}/edit", data={
        "name": name, "lat": str(lat), "lon": str(lon), "site_type": "site",
    }, follow_redirects=True)

    with app.app_context():
        assert AuditLog.query.filter_by(object_type="Site", object_id=site_id, action="update").count() == 0


def test_delete_site_blocked_when_devices_assigned(app, admin_client):
    with app.app_context():
        site = make_site()
        make_device(site)
        db.session.commit()
        site_id = site.id

    admin_client.post(f"/sites/{site_id}/delete", follow_redirects=True)

    with app.app_context():
        assert Site.query.get(site_id) is not None  # not deleted
        assert AuditLog.query.filter_by(object_type="Site", action="delete").count() == 0


def test_delete_site_records_audit_entry(app, admin_client):
    with app.app_context():
        site = make_site(name="Doomed Site")
        db.session.commit()
        site_id = site.id

    admin_client.post(f"/sites/{site_id}/delete", follow_redirects=True)

    with app.app_context():
        assert Site.query.get(site_id) is None
        entry = AuditLog.query.filter_by(object_type="Site", object_id=site_id, action="delete").first()
        assert entry is not None
        assert entry.label == "Doomed Site"


def test_non_admin_cannot_add_site(app, client):
    with app.app_context():
        from sitewatch.models import User
        u = User(username="viewer", role="read_only")
        u.set_password("viewerpass")
        db.session.add(u)
        db.session.commit()

    client.post("/login", data={"username": "viewer", "password": "viewerpass"})
    resp = client.post("/sites/add", data={
        "name": "Nope", "lat": "1", "lon": "1", "site_type": "site",
    })
    assert resp.status_code in (302, 403)
    with app.app_context():
        assert Site.query.filter_by(name="Nope").first() is None
