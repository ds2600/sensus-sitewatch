"""Layers: a pure map-visibility filter (Settings -> Layers). Untagged
sites/circuits show on every layer view; tagged ones show only on their
own. Doesn't touch status/polling/alerting — those are covered
separately (status.py already runs fully regardless of layer). These
tests focus on the two things that actually matter: the /api/map filter
logic, and the delete-guard/audit trail around the Layer definitions
themselves.
"""
from sitewatch.extensions import db
from sitewatch.models import Layer, Site
from tests.factories import make_site, make_device, make_interface, make_circuit, make_layer


def test_add_and_delete_layer(app, admin_client):
    admin_client.post("/settings/layers/add", data={"name": "Team A"}, follow_redirects=True)
    with app.app_context():
        layer = Layer.query.filter_by(name="Team A").first()
        assert layer is not None
        layer_id = layer.id

    admin_client.post(f"/settings/layers/{layer_id}/delete", follow_redirects=True)
    with app.app_context():
        assert Layer.query.get(layer_id) is None


def test_delete_layer_blocked_when_a_site_uses_it(app, admin_client):
    with app.app_context():
        layer = make_layer(name="In Use")
        make_site(layer=layer)
        db.session.commit()
        layer_id = layer.id

    admin_client.post(f"/settings/layers/{layer_id}/delete", follow_redirects=True)
    with app.app_context():
        assert Layer.query.get(layer_id) is not None


def test_delete_layer_blocked_when_a_device_uses_it(app, admin_client):
    with app.app_context():
        layer = make_layer(name="Device Layer")
        site = make_site()
        make_device(site, layer=layer)
        db.session.commit()
        layer_id = layer.id

    admin_client.post(f"/settings/layers/{layer_id}/delete", follow_redirects=True)
    with app.app_context():
        assert Layer.query.get(layer_id) is not None


def test_delete_layer_blocked_when_a_circuit_uses_it(app, admin_client):
    with app.app_context():
        layer = make_layer(name="Circuit Layer")
        site_a, site_b = make_site(name="A"), make_site(name="B")
        dev_a, dev_b = make_device(site_a), make_device(site_b)
        make_circuit(make_interface(dev_a), make_interface(dev_b), layer=layer)
        db.session.commit()
        layer_id = layer.id

    admin_client.post(f"/settings/layers/{layer_id}/delete", follow_redirects=True)
    with app.app_context():
        assert Layer.query.get(layer_id) is not None


def test_map_api_no_filter_shows_everything(app, admin_client):
    with app.app_context():
        layer_a = make_layer(name="Filter A")
        make_site(name="Tagged Site", layer=layer_a)
        make_site(name="Untagged Site")
        db.session.commit()

    resp = admin_client.get("/api/map")
    names = {s["name"] for s in resp.get_json()["sites"]}
    assert {"Tagged Site", "Untagged Site"}.issubset(names)


def test_map_api_layer_filter_shows_tagged_plus_untagged_only(app, admin_client):
    with app.app_context():
        layer_a = make_layer(name="Filter B")
        layer_b = make_layer(name="Filter C")
        make_site(name="Layer B Site", layer=layer_a)
        make_site(name="Layer C Site", layer=layer_b)
        make_site(name="Shared Site")  # untagged
        db.session.commit()
        layer_a_id = layer_a.id

    resp = admin_client.get(f"/api/map?layer_id={layer_a_id}")
    names = {s["name"] for s in resp.get_json()["sites"]}
    assert "Layer B Site" in names
    assert "Shared Site" in names
    assert "Layer C Site" not in names


def test_map_api_layer_filter_applies_to_circuits_independently_of_sites(app, admin_client):
    """A circuit's own layer tag controls its visibility regardless of
    whether its endpoint sites are tagged at all — sites and circuits are
    independently taggable, as specified."""
    with app.app_context():
        layer_a = make_layer(name="Filter D")
        site_a, site_b = make_site(name="A"), make_site(name="B")  # untagged sites
        dev_a, dev_b = make_device(site_a), make_device(site_b)
        tagged_circuit = make_circuit(make_interface(dev_a), make_interface(dev_b),
                                       name="Tagged Circuit", layer=layer_a)
        untagged_circuit = make_circuit(make_interface(dev_a), make_interface(dev_b), name="Untagged Circuit")

        layer_b = make_layer(name="Filter E")
        other_circuit = make_circuit(make_interface(dev_a), make_interface(dev_b),
                                      name="Other Layer Circuit", layer=layer_b)
        db.session.commit()
        layer_a_id = layer_a.id

    resp = admin_client.get(f"/api/map?layer_id={layer_a_id}")
    names = {l["name"] for l in resp.get_json()["lines"]}
    assert "Tagged Circuit" in names
    assert "Untagged Circuit" in names
    assert "Other Layer Circuit" not in names


def test_layer_edit_recorded_in_site_audit_diff(app, admin_client):
    from sitewatch.models import AuditLog
    with app.app_context():
        layer = make_layer(name="Audit Layer")
        site = make_site(name="Audit Site")
        db.session.commit()
        site_id, layer_id, name, lat, lon = site.id, layer.id, site.name, site.lat, site.lon

    admin_client.post(f"/sites/{site_id}/edit", data={
        "name": name, "lat": str(lat), "lon": str(lon), "site_type": "site", "layer_id": str(layer_id),
    }, follow_redirects=True)

    with app.app_context():
        entry = AuditLog.query.filter_by(object_type="Site", object_id=site_id, action="update").first()
        assert entry is not None
        assert "layer_id" in entry.details
