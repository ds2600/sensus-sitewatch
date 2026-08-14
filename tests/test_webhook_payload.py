from sitewatch.extensions import db
from sitewatch.integrations.webhook_payload import build_payload, build_test_payload
from sitewatch.models import Setting
from tests.factories import make_site, make_device, make_interface, make_role, make_circuit


def test_build_payload_groups_multiple_circuits_into_one_message(app):
    with app.app_context():
        site_a, site_b = make_site(name="Chicago DC"), make_site(name="Denver DC")
        dev_a1, dev_b1 = make_device(site_a, hostname="a1"), make_device(site_b, hostname="b1")
        dev_a2, dev_b2 = make_device(site_a, hostname="a2"), make_device(site_b, hostname="b2")
        role = make_role(name="core-webhook", tier="critical")
        c1 = make_circuit(make_interface(dev_a1), make_interface(dev_b1), role=role,
                           current_state="down", name="Circuit One")
        c2 = make_circuit(make_interface(dev_a2), make_interface(dev_b2), role=role,
                           current_state="down", name="Circuit Two")
        db.session.commit()

        payload = build_payload([c1, c2])
        text = payload["text"]

        assert "2 circuits down" in text
        assert "Circuit One" in text
        assert "Circuit Two" in text
        assert "Chicago DC" in text and "Denver DC" in text
        # Both sites should show as red in the impact section since their
        # only critical circuits are both down.
        assert text.count("Chicago DC") >= 2  # once in circuit list, once in site impact
        assert "down" in text.lower()


def test_build_payload_singular_count_wording(app):
    with app.app_context():
        site_a, site_b = make_site(name="A"), make_site(name="B")
        dev_a, dev_b = make_device(site_a), make_device(site_b)
        role = make_role(tier="critical")
        c1 = make_circuit(make_interface(dev_a), make_interface(dev_b), role=role,
                           current_state="down", name="Solo Circuit")
        db.session.commit()
        payload = build_payload([c1])
        assert "1 circuit down" in payload["text"]
        assert "1 circuits down" not in payload["text"]


def test_build_payload_site_impact_only_covers_sites_the_batch_actually_touches(app):
    """An unrelated site with its own problems (from circuits NOT in this
    batch) must never leak into the impact section — only the sites the
    down circuits in THIS batch actually connect to."""
    with app.app_context():
        site_a, site_b = make_site(name="Batch Site A"), make_site(name="Batch Site B")
        dev_a, dev_b = make_device(site_a), make_device(site_b)
        role = make_role(tier="critical")
        in_batch = make_circuit(make_interface(dev_a), make_interface(dev_b), role=role,
                                 current_state="down", name="In Batch")

        # A wholly unrelated site/circuit, also down, but NOT part of this
        # webhook's batch (e.g. it went down in a previous poll cycle).
        site_c, site_d = make_site(name="Unrelated C"), make_site(name="Unrelated D")
        dev_c, dev_d = make_device(site_c), make_device(site_d)
        make_circuit(make_interface(dev_c), make_interface(dev_d), role=role, current_state="down")
        db.session.commit()

        payload = build_payload([in_batch])
        assert "Batch Site A" in payload["text"]
        assert "Unrelated C" not in payload["text"]
        assert "Unrelated D" not in payload["text"]


def test_build_payload_appends_sitewatch_link_when_configured(app):
    with app.app_context():
        Setting.set("sitewatch_url", "https://example.test/sitewatch")
        db.session.commit()
        site_a, site_b = make_site(name="A"), make_site(name="B")
        dev_a, dev_b = make_device(site_a), make_device(site_b)
        c1 = make_circuit(make_interface(dev_a), make_interface(dev_b), current_state="down")
        db.session.commit()
        payload = build_payload([c1])
        assert "<https://example.test/sitewatch|Open SiteWatch>" in payload["text"]


def test_build_payload_omits_link_when_not_configured(app):
    with app.app_context():
        site_a, site_b = make_site(name="A"), make_site(name="B")
        dev_a, dev_b = make_device(site_a), make_device(site_b)
        c1 = make_circuit(make_interface(dev_a), make_interface(dev_b), current_state="down")
        db.session.commit()
        payload = build_payload([c1])
        assert "Open SiteWatch" not in payload["text"]


def test_build_test_payload_is_clearly_marked_as_a_test(app):
    with app.app_context():
        payload = build_test_payload()
        assert "Test alert" in payload["text"]
        assert "circuits down" in payload["text"]
