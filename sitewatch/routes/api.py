"""JSON endpoints for the map and the in-browser alert widget."""
from flask import Blueprint, jsonify, request
from flask_login import login_required

from sitewatch.models import Site, Circuit, CircuitStatusHistory, AlertMute
from sitewatch.status import compute_site_status
from sitewatch.poller import get_poller_status
from sitewatch import job_log

api_bp = Blueprint("api", __name__, url_prefix="/api")


@api_bp.route("/map")
@login_required
def map_data():
    sites = [{"id": s.id, "name": s.name, "lat": s.lat, "lon": s.lon,
              "status": compute_site_status(s), "site_type": s.site_type} for s in Site.query.all()]

    lines = []
    for c in Circuit.query.filter_by(parent_circuit_id=None).all():
        if c.is_intra_site:
            continue  # intra-site circuits render in the site detail panel, not the map
        a, b = c.site_a, c.site_b
        if not a or not b:
            continue
        lines.append({
            "id": c.id, "name": c.name, "state": c.current_state,
            "role": c.role.name, "tier": c.role.tier,
            "site_a": {"lat": a.lat, "lon": a.lon}, "site_b": {"lat": b.lat, "lon": b.lon},
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


@api_bp.route("/jobs")
@login_required
def jobs():
    """Recent background activity (poll cycles, manual walk/repoll) for the
    Settings page's activity list."""
    return jsonify({"jobs": job_log.list_jobs()})


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
