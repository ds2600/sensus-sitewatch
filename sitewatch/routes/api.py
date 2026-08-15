"""JSON endpoints for the map and the in-browser alert widget."""
from flask import Blueprint, jsonify, request
from flask_login import login_required
from sqlalchemy import or_

from sitewatch.models import Site, Device, Circuit, CircuitStatusHistory, AlertMute, Region
from sitewatch.status import compute_site_status
from sitewatch.poller import get_poller_status
from sitewatch import job_log

api_bp = Blueprint("api", __name__, url_prefix="/api")


def _util_pct(circuit):
    """Same formula as the util_pct Jinja macro (_macros.html) — max of
    in/out over effective capacity — kept in sync by hand since one's
    Python and the other's Jinja. None if there's no capacity set or no
    numeric reading yet, matching the macro's "-" case."""
    capacity = circuit.effective_capacity_bps
    peak = [v for v in (circuit.current_in_bps, circuit.current_out_bps) if v is not None]
    if not capacity or not peak:
        return None
    return round(max(peak) / capacity * 100)


@api_bp.route("/map")
@login_required
def map_data():
    # Layer is purely a map-visibility filter (Settings -> Layers) — it
    # never touches polling/status/alerting, which stay computed and
    # running against everything regardless of what's selected here.
    # Untagged (layer_id is None) sites/circuits are shared/core
    # infrastructure and show up on every layer view; a tagged one shows
    # up only on its own. No filter at all (layer_id absent/0) means "All".
    layer_id = request.args.get("layer_id", type=int)

    site_query = Site.query
    circuit_query = Circuit.query.filter_by(parent_circuit_id=None)
    if layer_id:
        site_query = site_query.filter(or_(Site.layer_id.is_(None), Site.layer_id == layer_id))
        circuit_query = circuit_query.filter(or_(Circuit.layer_id.is_(None), Circuit.layer_id == layer_id))

    sites = [{"id": s.id, "name": s.name, "lat": s.lat, "lon": s.lon,
              "status": compute_site_status(s), "site_type": s.site_type} for s in site_query.all()]

    lines = []
    for c in circuit_query.all():
        if c.is_intra_site:
            continue  # intra-site circuits render in the site detail panel, not the map
        a, b = c.site_a, c.site_b
        if not a or not b:
            continue
        lines.append({
            "id": c.id, "name": c.name, "state": c.current_state,
            "role": c.role.name, "tier": c.role.tier, "util_pct": _util_pct(c),
            "site_a": {"lat": a.lat, "lon": a.lon}, "site_b": {"lat": b.lat, "lon": b.lon},
            # Cosmetic only — bends the drawn line through these points, in
            # order, between site_a and site_b. Doesn't affect status/roll-up.
            # A bundle without its own waypoints falls back to a member's.
            "waypoints": [{"lat": w.site.lat, "lon": w.site.lon} for w in c.effective_waypoints],
        })
    return jsonify({"sites": sites, "lines": lines})


@api_bp.route("/status")
@login_required
def status():
    """Single poll target for the header: alert count/list + poller state,
    so the browser makes one background request instead of two."""
    down = CircuitStatusHistory.query.join(Circuit).filter(CircuitStatusHistory.cleared_at.is_(None)).all()
    unmuted = [h for h in down if not AlertMute.is_muted(h.circuit_id)]
    return jsonify({
        "alerts": {
            "count": len(unmuted),
            "circuits": [{"id": h.circuit_id, "name": h.circuit.name, "since": h.started_at.isoformat()}
                         for h in unmuted],
        },
        "poller": get_poller_status(),
    })


@api_bp.route("/search")
@login_required
def search():
    """Navbar quick search — substring match across sites/devices/circuits/
    incidents, grouped and capped per type so the dropdown stays short.
    Sites also match on their region's name, so "search for sites" covers
    finding a region's whole member list, not just a site by its own name.
    Incidents match on either SiteWatch's own incident_number or the NOC's
    external_ticket, open or closed — the result links to the owning
    circuit's detail page, same as a plain circuit-name match would."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify({"sites": [], "devices": [], "circuits": [], "incidents": []})
    like = f"%{q}%"
    sites = (Site.query.outerjoin(Region).filter(or_(Site.name.ilike(like), Region.name.ilike(like)))
             .order_by(Site.name).limit(5).all())
    devices = Device.query.filter(Device.hostname.ilike(like)).order_by(Device.hostname).limit(5).all()
    circuits = Circuit.query.filter(Circuit.name.ilike(like)).order_by(Circuit.name).limit(5).all()
    incidents = (CircuitStatusHistory.query.join(Circuit)
                 .filter(or_(CircuitStatusHistory.incident_number.ilike(like),
                             CircuitStatusHistory.external_ticket.ilike(like)))
                 .order_by(CircuitStatusHistory.started_at.desc()).limit(5).all())
    return jsonify({
        "sites": [{"id": s.id, "label": s.name} for s in sites],
        "devices": [{"id": d.id, "label": d.hostname} for d in devices],
        "circuits": [{"id": c.id, "label": c.name} for c in circuits],
        "incidents": [{"id": h.circuit_id, "label": f"{h.incident_number} — {h.circuit.name}"} for h in incidents],
    })


@api_bp.route("/jobs")
@login_required
def jobs():
    """Recent background activity (poll cycles, manual walk/repoll) for the
    standalone activity page. `limit` lets that page ask for more than the
    small default (job_log's in-memory store caps at 300 anyway)."""
    limit = request.args.get("limit", 30, type=int)
    return jsonify({"jobs": job_log.list_jobs(limit=limit)})


@api_bp.route("/jobs/<job_id>/log")
@login_required
def job_log_tail(job_id):
    """Polled by the walk/repoll modal. `since` is the next_index from the
    previous response — lets the client fetch only new lines each tick
    instead of re-sending the whole log."""
    since = request.args.get("since", 0, type=int)
    job = job_log.get_job(job_id, since=since)
    if job is None:
        return jsonify({"error": "Job not found or expired."}), 404
    return jsonify(job)


@api_bp.route("/jobs/<job_id>/cancel", methods=["POST"])
@login_required
def job_cancel(job_id):
    """The Tail Modal's Stop button — see job_log.request_cancel for what
    this can and can't actually interrupt."""
    job_log.request_cancel(job_id)
    return jsonify({"ok": True})
