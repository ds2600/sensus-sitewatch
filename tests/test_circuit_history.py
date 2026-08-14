"""The shared "Down history" table (circuit_history_table macro) embedded
on Circuit/Site/Device detail pages. CircuitStatusHistory is only ever
written for LEAF circuits (a bundle's own current_state changes via
recompute_bundle_state directly, never through poller.py's _transition),
so every one of these routes has to expand a bundle into its members to
find any history at all — that expansion is the thing worth testing here,
not just "does a row show up."
"""
from datetime import datetime, timedelta

from sitewatch.extensions import db
from sitewatch.models import CircuitStatusHistory
from tests.factories import make_site, make_device, make_interface, make_role, make_circuit, make_bundle


def _history_row(circuit, started_at=None, cleared_at=None, ticket=None):
    row = CircuitStatusHistory(
        circuit_id=circuit.id, state="down",
        started_at=started_at or (datetime.utcnow() - timedelta(hours=1)),
        cleared_at=cleared_at, incident_number=f"INC-{circuit.id:06d}", external_ticket=ticket,
    )
    db.session.add(row)
    return row


def test_circuit_detail_shows_own_history(app, admin_client):
    with app.app_context():
        site_a, site_b = make_site(name="A"), make_site(name="B")
        dev_a, dev_b = make_device(site_a), make_device(site_b)
        circuit = make_circuit(make_interface(dev_a), make_interface(dev_b), name="History Circuit")
        _history_row(circuit, ticket="NOC-123")
        db.session.commit()
        circuit_id = circuit.id

    resp = admin_client.get(f"/circuits/{circuit_id}")
    assert resp.status_code == 200
    assert f"INC-{circuit_id:06d}".encode() in resp.data
    assert b"NOC-123" in resp.data


def test_bundle_detail_shows_member_history_with_circuit_column(app, admin_client):
    with app.app_context():
        site_a, site_b = make_site(name="A"), make_site(name="B")
        dev_a, dev_b = make_device(site_a), make_device(site_b)
        role = make_role()
        bundle = make_bundle(role=role, name="History Bundle")
        member = make_circuit(make_interface(dev_a), make_interface(dev_b), role=role,
                               name="History Member", parent=bundle)
        _history_row(member)
        db.session.commit()
        bundle_id = bundle.id

    resp = admin_client.get(f"/circuits/{bundle_id}")
    assert resp.status_code == 200
    assert b"History Member" in resp.data  # circuit column shown, names the actual member


def test_site_detail_shows_history_for_touching_circuits_including_bundle_members(app, admin_client):
    with app.app_context():
        site_a, site_b = make_site(name="Hist Site A"), make_site(name="Hist Site B")
        dev_a, dev_b = make_device(site_a), make_device(site_b)
        role = make_role()
        bundle = make_bundle(role=role, name="Site Bundle")
        # Bundle itself must actually touch site_a/site_b for site_detail's
        # root_circuits filter to pick it up — borrowed from the first
        # member with real endpoints, same as everywhere else in the model.
        member = make_circuit(make_interface(dev_a), make_interface(dev_b), role=role,
                               name="Site Bundle Member", parent=bundle)
        _history_row(member, ticket="NOC-SITE-1")
        db.session.commit()
        site_a_id = site_a.id

    resp = admin_client.get(f"/sites/{site_a_id}")
    assert resp.status_code == 200
    assert b"Site Bundle Member" in resp.data
    assert b"NOC-SITE-1" in resp.data


def test_device_detail_shows_history_for_its_circuits(app, admin_client):
    with app.app_context():
        site_a, site_b = make_site(name="Dev Site A"), make_site(name="Dev Site B")
        dev_a, dev_b = make_device(site_a, hostname="hist-dev-a"), make_device(site_b)
        circuit = make_circuit(make_interface(dev_a), make_interface(dev_b), name="Dev History Circuit")
        _history_row(circuit, ticket="NOC-DEV-1")
        db.session.commit()
        dev_a_id = dev_a.id

    resp = admin_client.get(f"/devices/{dev_a_id}")
    assert resp.status_code == 200
    assert b"Dev History Circuit" in resp.data
    assert b"NOC-DEV-1" in resp.data


def test_ongoing_incident_shows_badge_not_a_cleared_date(app, admin_client):
    with app.app_context():
        site_a, site_b = make_site(name="A"), make_site(name="B")
        dev_a, dev_b = make_device(site_a), make_device(site_b)
        circuit = make_circuit(make_interface(dev_a), make_interface(dev_b), name="Still Down Circuit")
        _history_row(circuit, cleared_at=None)
        db.session.commit()
        circuit_id = circuit.id

    resp = admin_client.get(f"/circuits/{circuit_id}")
    assert b"Ongoing" in resp.data


def test_unrelated_circuits_history_does_not_leak_into_device_page(app, admin_client):
    with app.app_context():
        site_a, site_b = make_site(name="A"), make_site(name="B")
        dev_a, dev_b = make_device(site_a, hostname="own-device"), make_device(site_b)
        own_circuit = make_circuit(make_interface(dev_a), make_interface(dev_b), name="Own Circuit")
        _history_row(own_circuit)

        site_c, site_d = make_site(name="C"), make_site(name="D")
        dev_c, dev_d = make_device(site_c), make_device(site_d)
        unrelated_circuit = make_circuit(make_interface(dev_c), make_interface(dev_d), name="Unrelated Circuit")
        _history_row(unrelated_circuit)
        db.session.commit()
        dev_a_id = dev_a.id

    resp = admin_client.get(f"/devices/{dev_a_id}")
    assert b"Own Circuit" in resp.data
    assert b"Unrelated Circuit" not in resp.data
