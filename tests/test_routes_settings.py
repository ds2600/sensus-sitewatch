from sitewatch.extensions import db
from sitewatch.models import CircuitRole, Region, User, Setting, AuditLog
from tests.factories import make_site, make_device, make_interface, make_circuit, make_role


def test_settings_batch_save_records_one_diff_row(app, admin_client):
    # Every value matches Setting.DEFAULTS except mute_max_minutes, so the
    # diff should contain exactly that one changed key.
    resp = admin_client.post("/settings/", data={
        "polling_interval_minutes": "2", "down_threshold_count": "3", "mute_max_minutes": "45",
        "google_chat_webhook_url": "", "sitewatch_url": "", "status_history_retention_days": "30",
        "audit_log_retention_days": "90", "poller_max_workers": "8", "incident_number_prefix": "INC",
    }, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert Setting.get("mute_max_minutes") == "45"
        entries = AuditLog.query.filter_by(object_type="Setting", action="update").all()
        assert len(entries) == 1
        assert "mute_max_minutes" in entries[0].details
        assert "polling_interval_minutes" not in entries[0].details  # unchanged, not in the diff


def test_delete_role_blocked_when_in_use(app, admin_client):
    with app.app_context():
        role = make_role(name="in-use-role")
        site_a, site_b = make_site(name="A"), make_site(name="B")
        dev_a, dev_b = make_device(site_a), make_device(site_b)
        iface_a, iface_b = make_interface(dev_a), make_interface(dev_b)
        make_circuit(iface_a, iface_b, role=role)
        db.session.commit()
        role_id = role.id

    admin_client.post(f"/settings/roles/{role_id}/delete", follow_redirects=True)

    with app.app_context():
        assert CircuitRole.query.get(role_id) is not None
        assert AuditLog.query.filter_by(object_type="CircuitRole", action="delete").count() == 0


def test_delete_region_blocked_when_in_use(app, admin_client):
    with app.app_context():
        db.session.add(Region(name="Used Region"))
        db.session.commit()
        region = Region.query.filter_by(name="Used Region").first()
        region_id = region.id
        site = make_site()
        site.region_id = region_id
        db.session.commit()

    admin_client.post(f"/settings/site-regions/{region_id}/delete", follow_redirects=True)

    with app.app_context():
        assert Region.query.get(region_id) is not None


def test_user_cannot_demote_self(app, admin_client):
    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        admin_id = admin.id

    admin_client.post(f"/settings/users/{admin_id}/role", data={"role": "read_only"}, follow_redirects=True)

    with app.app_context():
        admin = User.query.get(admin_id)
        assert admin.role == "admin"


def test_user_cannot_delete_self(app, admin_client):
    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        admin_id = admin.id

    admin_client.post(f"/settings/users/{admin_id}/delete", follow_redirects=True)

    with app.app_context():
        assert User.query.get(admin_id) is not None


def test_add_user_and_delete_records_audit_entries(app, admin_client):
    admin_client.post("/settings/users/add", data={
        "username": "seconduser", "password": "somepassword", "role": "read_only",
    }, follow_redirects=True)
    with app.app_context():
        user = User.query.filter_by(username="seconduser").first()
        assert user is not None
        user_id = user.id
        assert AuditLog.query.filter_by(object_type="User", object_id=user_id, action="create").count() == 1

    admin_client.post(f"/settings/users/{user_id}/delete", follow_redirects=True)
    with app.app_context():
        assert User.query.get(user_id) is None
        assert AuditLog.query.filter_by(object_type="User", object_id=user_id, action="delete").count() == 1


def test_reset_password_never_logs_the_password(app, admin_client):
    admin_client.post("/settings/users/add", data={
        "username": "thirduser", "password": "initialpw", "role": "read_only",
    })
    with app.app_context():
        user_id = User.query.filter_by(username="thirduser").first().id

    admin_client.post(f"/settings/users/{user_id}/password", data={"password": "brandnewsecret"},
                       follow_redirects=True)

    with app.app_context():
        entry = AuditLog.query.filter_by(object_type="User", object_id=user_id, action="update").first()
        assert entry is not None
        assert "brandnewsecret" not in entry.details
        assert "password_reset" in entry.details
