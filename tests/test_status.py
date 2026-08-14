"""status.py is "the core business logic of the app" per CLAUDE.md — this
covers the rollup truth table and compute_site_status()'s red/yellow/green
rules directly, since a silent regression here is the worst kind (wrong
color on the map, nobody notices until an outage).
"""
import pytest

from sitewatch.status import rollup_degree_status, compute_site_status, recompute_bundle_state, site_degree_breakdown
from tests.factories import (
    make_site, make_device, make_interface, make_role, make_circuit, make_bundle, link_devices,
)


# --- rollup_degree_status: the shared degree-rollup truth table ---

@pytest.mark.parametrize("states,expected", [
    ([], "up"),
    (["up"], "up"),
    (["up", "up", "up"], "up"),
    (["down"], "down"),
    (["down", "down"], "down"),
    (["unreachable", "unreachable"], "down"),  # unreachable counts as down for the "all down" case
    (["up", "down"], "degraded"),
    (["up", "up", "down"], "degraded"),
    (["degraded"], "degraded"),
    (["up", "unreachable"], "degraded"),  # one neighbor dark, others fine — degraded, not silently dropped
    (["admin_down"], "up"),  # admin_down isn't in the "relevant" set — excluded entirely, not counted either way
    (["admin_down", "down"], "down"),
])
def test_rollup_degree_status(states, expected):
    assert rollup_degree_status(states) == expected


# --- compute_site_status: passthrough / blue (all-unreachable) ---

def test_passthrough_site_has_no_status(app):
    with app.app_context():
        site = make_site(site_type="passthrough")
        assert compute_site_status(site) == "passthrough"


def test_site_with_all_devices_unreachable_is_blue(app):
    with app.app_context():
        site = make_site()
        make_device(site, reachable=False)
        make_device(site, hostname="dev2", reachable=False)
        assert compute_site_status(site) == "blue"


def test_site_with_one_reachable_device_is_not_blue(app):
    with app.app_context():
        site = make_site()
        make_device(site, hostname="dev1", reachable=False)
        make_device(site, hostname="dev2", reachable=True)
        # No circuits at all -> green (nothing down), specifically NOT blue
        # since not every device is unreachable.
        assert compute_site_status(site) != "blue"


# --- compute_site_status: tier-driven red/yellow/green ---

def test_critical_external_circuit_down_is_red(app):
    with app.app_context():
        site_a = make_site(name="A")
        site_b = make_site(name="B")
        critical = make_role(tier="critical")
        link_devices(site_a, site_b, role=critical, current_state="down")
        assert compute_site_status(site_a) == "red"
        assert compute_site_status(site_b) == "red"


def test_auxiliary_circuit_down_is_red_when_its_the_sites_only_external_circuit(app):
    """The exact footgun the minor-site conversation flagged: a site's
    sole external connection tagged auxiliary instead of critical (an easy
    mistake — "minor site" sounds unimportant) must still read red when
    that link is 100% down, since the site genuinely has zero external
    connectivity. See compute_site_status()'s ext_all safety net."""
    with app.app_context():
        site_a = make_site(name="A")
        site_b = make_site(name="B")
        aux = make_role(tier="auxiliary")
        link_devices(site_a, site_b, role=aux, current_state="down")
        assert compute_site_status(site_a) == "red"


def test_auxiliary_circuit_down_still_caps_at_yellow_when_critical_stays_up(app):
    """The safety net only fires when a site has NO external connectivity
    left at all — a healthy critical circuit alongside a separate down
    auxiliary one is still just "degraded", not "down", so this must not
    regress to red via auxiliary traffic alone."""
    with app.app_context():
        site_a = make_site(name="A")
        site_b, site_c = make_site(name="B"), make_site(name="C")
        critical, aux = make_role(tier="critical"), make_role(tier="auxiliary")
        link_devices(site_a, site_b, role=critical, current_state="up")
        link_devices(site_a, site_c, role=aux, current_state="down")
        assert compute_site_status(site_a) == "yellow"


def test_all_up_is_green(app):
    with app.app_context():
        site_a = make_site(name="A")
        site_b = make_site(name="B")
        link_devices(site_a, site_b, current_state="up")
        assert compute_site_status(site_a) == "green"


def test_degraded_critical_bundle_state_is_yellow(app):
    with app.app_context():
        site_a = make_site(name="A")
        site_b = make_site(name="B")
        critical = make_role(tier="critical")
        link_devices(site_a, site_b, role=critical, current_state="degraded")
        assert compute_site_status(site_a) == "yellow"


def test_intra_site_critical_circuit_down_caps_at_yellow(app):
    """Only a fully-down critical EXTERNAL circuit set can push a site to
    red (CLAUDE.md's status model section) — an intra-site (internal)
    circuit going down, even critical-tier, caps at yellow. This is the
    documented rule, not a bug — worth its own test since it's easy to
    "fix" by mistake."""
    with app.app_context():
        site = make_site()
        dev1 = make_device(site, hostname="dev1")
        dev2 = make_device(site, hostname="dev2")
        iface1 = make_interface(dev1)
        iface2 = make_interface(dev2)
        critical = make_role(tier="critical")
        make_circuit(iface1, iface2, role=critical, current_state="down")
        assert compute_site_status(site) == "yellow"


# --- compute_site_status: minor-site parent cascade ---

def test_minor_site_forced_red_when_parent_is_red(app):
    with app.app_context():
        major = make_site(name="Major")
        neighbor = make_site(name="Neighbor")
        critical = make_role(tier="critical")
        # Major goes red via its own critical link to some other site.
        link_devices(major, neighbor, role=critical, current_state="down")

        minor = make_site(name="Minor", site_type="minor", parent_site=major)
        # Minor's OWN circuit (to a third, unrelated site) is fine.
        other = make_site(name="Other")
        link_devices(minor, other, role=critical, current_state="up")

        assert compute_site_status(major) == "red"
        assert compute_site_status(minor) == "red"  # forced by the parent cascade, not its own circuits


def test_minor_site_not_forced_when_parent_is_only_yellow(app):
    """Cascade only fires for red/blue parents — a merely-degraded parent
    must NOT drag a minor site down too (that would wrongly treat a plain
    != 'green' check as the cascade condition)."""
    with app.app_context():
        major = make_site(name="Major")
        neighbor_a, neighbor_b = make_site(name="Neighbor A"), make_site(name="Neighbor B")
        aux = make_role(tier="auxiliary")
        # One aux circuit up, one down -> partial external degradation
        # (yellow), NOT a fully-down external set (which would be red
        # under the safety net — see test_status.py's auxiliary tests).
        link_devices(major, neighbor_a, role=aux, current_state="up")
        link_devices(major, neighbor_b, role=aux, current_state="down")

        minor = make_site(name="Minor", site_type="minor", parent_site=major)
        other = make_site(name="Other")
        critical = make_role(tier="critical")
        link_devices(minor, other, role=critical, current_state="up")

        assert compute_site_status(major) == "yellow"
        assert compute_site_status(minor) == "green"


# --- recompute_bundle_state ---

def test_bundle_state_is_pure_member_rollup_with_no_lag(app):
    with app.app_context():
        site_a, site_b = make_site(name="A"), make_site(name="B")
        bundle = make_bundle()
        dev_a, dev_b = make_device(site_a), make_device(site_b)
        for _ in range(2):
            iface_a, iface_b = make_interface(dev_a), make_interface(dev_b)
            make_circuit(iface_a, iface_b, current_state="up", parent=bundle)
        db_state = recompute_bundle_state(bundle)
        assert db_state == "up"


def test_bundle_state_degraded_when_some_members_down(app):
    with app.app_context():
        site_a, site_b = make_site(name="A"), make_site(name="B")
        bundle = make_bundle()
        dev_a, dev_b = make_device(site_a), make_device(site_b)
        iface_a1, iface_b1 = make_interface(dev_a), make_interface(dev_b)
        make_circuit(iface_a1, iface_b1, current_state="up", parent=bundle)
        iface_a2, iface_b2 = make_interface(dev_a), make_interface(dev_b)
        make_circuit(iface_a2, iface_b2, current_state="down", parent=bundle)
        assert recompute_bundle_state(bundle) == "degraded"


def test_bundle_lag_hard_failure_overrides_healthy_members(app):
    """A LAG/port-channel hard failure (down/admin_down/unreachable) wins
    outright even if every physical member circuit still looks up — it
    catches things member analysis alone can't (e.g. an LACP negotiation
    failure with every physical link still up)."""
    with app.app_context():
        site_a, site_b = make_site(name="A"), make_site(name="B")
        bundle = make_bundle()
        dev_a, dev_b = make_device(site_a), make_device(site_b)
        iface_a, iface_b = make_interface(dev_a), make_interface(dev_b)
        make_circuit(iface_a, iface_b, current_state="up", parent=bundle)
        assert recompute_bundle_state(bundle, lag_state="down") == "down"


def test_bundle_healthy_lag_defers_to_member_rollup(app):
    with app.app_context():
        site_a, site_b = make_site(name="A"), make_site(name="B")
        bundle = make_bundle()
        dev_a, dev_b = make_device(site_a), make_device(site_b)
        iface_a, iface_b = make_interface(dev_a), make_interface(dev_b)
        make_circuit(iface_a, iface_b, current_state="down", parent=bundle)
        # lag_state itself healthy ("up") -> falls through to member rollup,
        # which is "down" (the bundle's one member is down).
        assert recompute_bundle_state(bundle, lag_state="up") == "down"


# --- site_degree_breakdown ---

def test_site_degree_breakdown_groups_by_neighbor(app):
    with app.app_context():
        site = make_site(name="Hub")
        neighbor_a = make_site(name="Neighbor A")
        neighbor_b = make_site(name="Neighbor B")
        link_devices(site, neighbor_a, current_state="up", name="link-1")
        link_devices(site, neighbor_a, current_state="down", name="link-2")
        link_devices(site, neighbor_b, current_state="up", name="link-3")

        breakdown = site_degree_breakdown(site)
        by_name = {d["site"].name: d for d in breakdown}

        assert set(by_name) == {"Neighbor A", "Neighbor B"}
        assert len(by_name["Neighbor A"]["circuits"]) == 2
        assert by_name["Neighbor A"]["state"] == "degraded"  # one up, one down
        assert by_name["Neighbor B"]["state"] == "up"


def test_site_degree_breakdown_excludes_intra_site_circuits(app):
    with app.app_context():
        site = make_site()
        dev1, dev2 = make_device(site, hostname="d1"), make_device(site, hostname="d2")
        iface1, iface2 = make_interface(dev1), make_interface(dev2)
        make_circuit(iface1, iface2, current_state="down")
        assert site_degree_breakdown(site) == []


def test_minor_site_own_critical_circuit_down_is_red_independent_of_parent(app):
    """The scenario from the minor-site design discussion: a minor site's
    own sole critical circuit going down makes IT red regardless of the
    parent's (unrelated) status — own_status is computed independently,
    the parent cascade is a fallback, never a cap."""
    with app.app_context():
        major = make_site(name="Major")
        neighbor = make_site(name="Neighbor")
        critical = make_role(tier="critical")
        link_devices(major, neighbor, role=critical, current_state="up")  # major stays green

        minor = make_site(name="Minor", site_type="minor", parent_site=major)
        other = make_site(name="Other")
        link_devices(minor, other, role=critical, current_state="down")

        assert compute_site_status(major) == "green"
        assert compute_site_status(minor) == "red"
