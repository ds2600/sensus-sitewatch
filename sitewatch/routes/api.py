"""JSON endpoints for the map and the in-browser alert widget."""
from flask import Blueprint, jsonify
from flask_login import login_required

from sitewatch.models import Site, Circuit, CircuitStatusHistory, AlertMute
from sitewatch.status import compute_site_status

api_bp = Blueprint("api", __name__, url_prefix="/api")


@api_bp.route("/map")
@login_required
def map_data():
    sites = [{"id": s.id, "name": s.name, "lat": s.lat, "lon": s.lon,
              "status": compute_site_status(s)} for s in Site.query.all()]

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


@api_bp.route("/alerts")
@login_required
def alerts():
    down = CircuitStatusHistory.query.join(Circuit).filter(CircuitStatusHistory.cleared_at.is_(None)).all()
    unmuted = [h for h in down if not AlertMute.is_muted(h.circuit_id)]
    return jsonify({
        "count": len(unmuted),
        "circuits": [{"id": h.circuit_id, "name": h.circuit.name, "since": h.started_at.isoformat()}
                     for h in unmuted],
    })
