from sitewatch.extensions import db
from sitewatch.models import User


def test_audit_page_requires_admin(app, client):
    with app.app_context():
        u = User(username="viewer", role="read_only")
        u.set_password("viewerpass")
        db.session.add(u)
        db.session.commit()

    client.post("/login", data={"username": "viewer", "password": "viewerpass"})
    resp = client.get("/audit/")
    assert resp.status_code in (302, 403)


def test_audit_page_lists_recorded_actions(app, admin_client):
    admin_client.post("/sites/add", data={"name": "Audit Route Test", "lat": "1", "lon": "1", "site_type": "site"})
    resp = admin_client.get("/audit/")
    assert resp.status_code == 200
    assert b"Audit Route Test" in resp.data
    assert b"admin" in resp.data


def test_audit_page_filters_by_action(app, admin_client):
    admin_client.post("/sites/add", data={"name": "Filter Test Site", "lat": "1", "lon": "1", "site_type": "site"})
    with app.app_context():
        from sitewatch.models import Site
        site = Site.query.filter_by(name="Filter Test Site").first()
        site_id = site.id
    admin_client.post(f"/sites/{site_id}/delete")

    resp_create = admin_client.get("/audit/?action=create")
    assert b"Filter Test Site" in resp_create.data

    resp_delete = admin_client.get("/audit/?action=delete")
    assert b"Filter Test Site" in resp_delete.data

    resp_mute = admin_client.get("/audit/?action=mute")
    assert b"Filter Test Site" not in resp_mute.data


def test_audit_page_pagination_respects_per_page(app, admin_client):
    for i in range(5):
        admin_client.post("/sites/add", data={"name": f"Page Site {i}", "lat": "1", "lon": "1", "site_type": "site"})

    resp = admin_client.get("/audit/?per_page=25")
    assert resp.status_code == 200
    # 5 creates recorded, well under 25 -> should all be on page 1
    assert resp.data.count(b"create") >= 5
