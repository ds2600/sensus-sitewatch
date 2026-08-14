def test_app_boots(app):
    assert app is not None


def test_login_page_loads(client):
    resp = client.get("/login")
    assert resp.status_code == 200


def test_admin_client_can_reach_settings(admin_client):
    resp = admin_client.get("/settings/")
    assert resp.status_code == 200


def test_unauthenticated_redirected(client):
    resp = client.get("/settings/", follow_redirects=False)
    assert resp.status_code in (302, 401, 403)
