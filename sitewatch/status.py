"""Status rollup logic. This is the core of the whole app — read it before
touching poller.py or the circuit/site routes.

Rollup rule (applies at both bundle-circuit and site level):
  - 1 relevant child, down            -> down
  - 2+ relevant children, some down   -> degraded
  - all relevant children down        -> down
  - all up                            -> up
`unreachable` children count toward the down side of this rule — fail-safe:
if a whole neighbor site drops off SNMP, its circuits shouldn't just get
silently excluded from whoever's rollup they feed into (a site could
otherwise show green after losing an entire neighbor). This is deliberately
NOT "infer from the near-end interface's own oper status instead" — that
reading is untrustworthy when there's optical transport gear (DWDM, media
converters) in between, since it can hold the physical layer up/up
regardless of whether the remote device is actually alive. Better to
assume the worst when we can't confirm than risk masking a real outage.
`admin_down` stays excluded — that's a deliberate, expected state (someone
shut the interface on purpose), not a failure to route around. Both are
still surfaced as their own distinct state to callers that need to flag
them separately from a confirmed interface-down.
"""
from sitewatch.models import Circuit, Site


def rollup_degree_status(child_states):
    """child_states: list of strings from CIRCUIT_STATES. Returns up/degraded/down."""
    relevant = [s for s in child_states if s in ("up", "down", "degraded", "unreachable")]
    if not relevant:
        return "up"
    down_count = sum(1 for s in relevant if s in ("down", "unreachable"))
    degraded_count = sum(1 for s in relevant if s == "degraded")
    total = len(relevant)
    if down_count == total:
        return "down"
    if down_count > 0 or degraded_count > 0:
        return "degraded"
    return "up"


def recompute_bundle_state(bundle_circuit, lag_state=None):
    """Bundles have no interfaces of their own by default — state is
    derived from children. Called bottom-up after leaf states are set.

    lag_state is the raw target state (up/down/admin_down/unreachable/None)
    read off the bundle's own optional LAG/port-channel interface pair, if
    configured (see poller.py's _lag_target_state) — computed there, not
    here, since this module is pure rollup math with no telemetry/device
    concerns. A hard failure there (down/admin_down/unreachable) wins
    outright: it catches things member-link analysis alone can't, like an
    LACP negotiation failure with every physical member still up. A
    healthy or absent lag_state defers entirely to the member rollup, so
    the degraded granularity (2-of-4 members down) isn't lost — IF-MIB's
    port-channel interface has no "degraded", only up/down."""
    child_states = [c.current_state for c in bundle_circuit.children]
    member_state = rollup_degree_status(child_states)
    if lag_state in ("down", "admin_down", "unreachable"):
        bundle_circuit.current_state = lag_state
    else:
        bundle_circuit.current_state = member_state
    return bundle_circuit.current_state


def compute_site_status(site):
    """Returns 'green' | 'yellow' | 'red' | 'blue' | 'passthrough'. Blue means
    every device at the site is failing SNMP reachability — distinct from a
    confirmed down circuit, since the site itself can't be assessed, not just
    one of its links. Root circuits only count toward red/yellow (no parent) —
    bundle members don't get counted twice against the site.

    Passthrough sites (site_type == 'passthrough') carry no equipment by
    design, so there's nothing to poll or roll up — they're a map waypoint,
    not a monitored location.

    Minor sites (site_type == 'minor') compute their own status exactly like
    any other site below, but a fully-down or unreachable parent Major Site
    forces this site to red too, regardless of its own circuits — a minor
    site genuinely can't be up if its uplink site is dark. A merely-degraded
    parent does NOT cascade — only 'red'/'blue' do (never a plain != 'green'
    check here, which would wrongly also cascade 'yellow'). Recursing into
    compute_site_status(site.parent_site) is safe from infinite recursion
    only because routes/sites.py enforces one level of nesting — a parent
    must itself be a plain Major Site with no parent_site_id of its own.

    Red normally requires a fully-down critical-tier external circuit set
    (see the ext_crit_status check below) — but a site whose EVERY external
    circuit is down, critical or auxiliary alike, is red regardless of how
    those circuits happen to be tagged. This is a safety net, not a second
    way to reach red through auxiliary traffic alone: a site with a healthy
    critical circuit and a separate down auxiliary one still only caps at
    yellow, same as always (the combined rollup below is "degraded", not
    "down", when anything external is still up). It only fires when a site
    has gone from some external connectivity to none — the case that
    matters is a minor site whose sole uplink got tagged auxiliary instead
    of critical (an easy mistake — "minor site" sounds unimportant) and
    that link fully fails: the site is unambiguously down, not "degraded",
    no matter what its one circuit was labeled. Deliberately excludes
    internal (intra-site) circuits — those still can never push a site to
    red on their own, same as always."""
    if site.site_type == "passthrough":
        return "passthrough"
    if site.devices and all(not d.reachable for d in site.devices):
        own_status = "blue"
    else:
        root_circuits = [c for c in Circuit.query.filter_by(parent_circuit_id=None).all()
                          if _touches_site(c, site.id)]

        ext_critical, ext_aux, int_critical, int_aux = [], [], [], []
        for c in root_circuits:
            bucket_list = _pick_bucket(c, site.id, ext_critical, ext_aux, int_critical, int_aux)
            bucket_list.append(c.current_state)

        ext_crit_status = rollup_degree_status(ext_critical)
        int_crit_status = rollup_degree_status(int_critical)
        ext_aux_status = rollup_degree_status(ext_aux)
        int_aux_status = rollup_degree_status(int_aux)
        ext_all = ext_critical + ext_aux

        if ext_crit_status == "down" or (ext_all and rollup_degree_status(ext_all) == "down"):
            own_status = "red"
        elif ext_crit_status == "degraded" or int_crit_status != "up":
            own_status = "yellow"
        elif ext_aux_status != "up" or int_aux_status != "up":
            own_status = "yellow"
        else:
            own_status = "green"

    if site.site_type == "minor" and site.parent_site is not None:
        if compute_site_status(site.parent_site) in ("red", "blue"):
            return "red"
    return own_status


def site_degree_breakdown(site):
    """External root circuits touching this site, grouped by the neighbor
    site on the other end ("degree" in the graph sense — one entry per
    distinct neighbor), each with its own rolled-up state. Purely a display
    aid for site_detail.html ("yellow because of Denver") — doesn't feed
    compute_site_status(), which already gets the same red/yellow/green
    answer from one flat rollup: nesting rollup_degree_status by neighbor
    first and then across neighbors is mathematically identical to
    flattening, since its down/up boundaries (unanimous failure/success)
    compose the same way either way. This just keeps the grouping instead
    of collapsing it, for the human reading the page."""
    root_circuits = [c for c in Circuit.query.filter_by(parent_circuit_id=None).all()
                      if _touches_site(c, site.id) and not c.is_intra_site]

    by_neighbor = {}
    for c in root_circuits:
        neighbor = c.site_b if c.site_a_id_safe() == site.id else c.site_a
        if neighbor is None:
            continue
        by_neighbor.setdefault(neighbor, []).append(c)

    return [
        {"site": neighbor, "state": rollup_degree_status([c.current_state for c in circuits]),
         "circuits": circuits}
        for neighbor, circuits in sorted(by_neighbor.items(), key=lambda kv: kv[0].name)
    ]


def _touches_site(circuit, site_id):
    if circuit.is_bundle:
        return any(_touches_site(c, site_id) for c in circuit.children)
    return circuit.site_a_id_safe() == site_id or circuit.site_b_id_safe() == site_id


def _pick_bucket(circuit, site_id, ext_critical, ext_aux, int_critical, int_aux):
    is_internal = circuit.is_intra_site
    is_critical = circuit.role.tier == "critical"
    if is_internal:
        return int_critical if is_critical else int_aux
    return ext_critical if is_critical else ext_aux
