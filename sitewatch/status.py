"""Status rollup logic. This is the core of the whole app — read it before
touching poller.py or the circuit/site routes.

Rollup rule (applies at both bundle-circuit and site level):
  - 1 relevant child, down            -> down
  - 2+ relevant children, some down   -> degraded
  - all relevant children down        -> down
  - all up                            -> up
`admin_down` and `unreachable` children are excluded from the down/degraded
count (they're not "this link failed", they're "this link isn't a normal
telemetry source right now") but are surfaced separately by callers that
need to flag them.
"""
from sitewatch.models import Circuit, Site


def rollup_degree_status(child_states):
    """child_states: list of strings from CIRCUIT_STATES. Returns up/degraded/down."""
    relevant = [s for s in child_states if s in ("up", "down", "degraded")]
    if not relevant:
        return "up"
    down_count = sum(1 for s in relevant if s == "down")
    degraded_count = sum(1 for s in relevant if s == "degraded")
    total = len(relevant)
    if down_count == total:
        return "down"
    if down_count > 0 or degraded_count > 0:
        return "degraded"
    return "up"


def recompute_bundle_state(bundle_circuit):
    """Bundles have no interfaces of their own — state is purely derived
    from children. Called bottom-up after leaf states are set."""
    child_states = [c.current_state for c in bundle_circuit.children]
    bundle_circuit.current_state = rollup_degree_status(child_states)
    return bundle_circuit.current_state


def compute_site_status(site):
    """Returns 'green' | 'yellow' | 'red' | 'blue' | 'passthrough'. Blue means
    every device at the site is failing SNMP reachability — distinct from a
    confirmed down circuit, since the site itself can't be assessed, not just
    one of its links. Root circuits only count toward red/yellow (no parent) —
    bundle members don't get counted twice against the site.

    Passthrough sites (site_type == 'passthrough') carry no equipment by
    design, so there's nothing to poll or roll up — they're a map waypoint,
    not a monitored location."""
    if site.site_type == "passthrough":
        return "passthrough"
    if site.devices and all(not d.reachable for d in site.devices):
        return "blue"

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

    if ext_crit_status == "down":
        return "red"
    if ext_crit_status == "degraded" or int_crit_status != "up":
        return "yellow"
    if ext_aux_status != "up" or int_aux_status != "up":
        return "yellow"
    return "green"


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
