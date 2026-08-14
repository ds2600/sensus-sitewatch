"""Read-side utilization history — turns UtilizationRollup rows (written
incrementally by poller.py's _update_utilization_rollup, one hour bucket
at a time) into a circuit-level percentage time series for the circuit
detail page's history chart. See UtilizationRollup's docstring in
models.py for why rollups are hourly-only with no separate daily table.
"""
from datetime import datetime, timedelta

from sitewatch.models import UtilizationRollup


def _canonical_interface_ids(circuit):
    """The same interface(s) Circuit.current_in_bps/current_out_bps
    (models.py) read from — a leaf's interface_a, a bundle's own LAG
    interface if wired, else every member's canonical interface(s)
    recursively. A bundle backed by multiple members has multiple ids
    here; everything else has exactly one."""
    if circuit.is_bundle:
        if circuit.lag_interface_a_id:
            return [circuit.lag_interface_a_id]
        ids = []
        for child in circuit.children:
            ids.extend(_canonical_interface_ids(child))
        return ids
    return [circuit.interface_a_id] if circuit.interface_a_id else []


def circuit_utilization_history(circuit, hours=24, bucket="hour"):
    """List of {period_start (ISO string), avg_pct, peak_pct}, oldest
    first. bucket="hour" returns one point per hour (the 24h view);
    bucket="day" collapses those further into one point per day (the 7d
    view) rather than reading from a separate daily rollup — there isn't
    one. Percentages are max(in, out)/effective_capacity_bps, same
    formula as the live util_pct macro / api.py's _util_pct, applied to
    the rolled-up bps figures instead of the latest single poll."""
    capacity = circuit.effective_capacity_bps
    interface_ids = _canonical_interface_ids(circuit)
    if not capacity or not interface_ids:
        return []

    cutoff = datetime.utcnow() - timedelta(hours=hours)
    rows = (UtilizationRollup.query
            .filter(UtilizationRollup.interface_id.in_(interface_ids))
            .filter(UtilizationRollup.period_type == "hourly")
            .filter(UtilizationRollup.period_start >= cutoff)
            .order_by(UtilizationRollup.period_start)
            .all())

    # Pass 1: circuit-level bps per hour. SUMMED across interfaces (a
    # bundle's throughput is its members' combined — same as
    # Circuit.current_in_bps), never averaged; a leaf or a LAG-wired
    # bundle only ever has one interface id here, so this is a no-op
    # single-term sum for those.
    hourly = {}
    for row in rows:
        h = hourly.setdefault(row.period_start, {"avg_in": 0.0, "avg_out": 0.0, "peak_in": 0.0, "peak_out": 0.0})
        h["avg_in"] += row.avg_in_bps or 0
        h["avg_out"] += row.avg_out_bps or 0
        h["peak_in"] += row.peak_in_bps or 0
        h["peak_out"] += row.peak_out_bps or 0

    if bucket == "hour":
        buckets = dict(hourly)
    else:
        # Pass 2 (7d view): collapse hours into days — mean of the hourly
        # circuit-level avgs, max of the hourly circuit-level peaks.
        # Summing peaks across bundle members already treats "peak" as a
        # conservative worst-case (each member's own busiest minute may
        # not have landed at the exact same instant) rather than a
        # strictly simultaneous reading — fine for "did we get close to
        # capacity," which is the point of this view, not a precise
        # forensic reconstruction.
        buckets = {}
        counts = {}
        for ts, v in hourly.items():
            day = ts.date()
            b = buckets.setdefault(day, {"avg_in": 0.0, "avg_out": 0.0, "peak_in": 0.0, "peak_out": 0.0})
            b["avg_in"] += v["avg_in"]
            b["avg_out"] += v["avg_out"]
            b["peak_in"] = max(b["peak_in"], v["peak_in"])
            b["peak_out"] = max(b["peak_out"], v["peak_out"])
            counts[day] = counts.get(day, 0) + 1
        for day, b in buckets.items():
            b["avg_in"] /= counts[day]
            b["avg_out"] /= counts[day]

    points = []
    for key in sorted(buckets):
        b = buckets[key]
        points.append({
            "period_start": key.isoformat(),
            "avg_pct": round(max(b["avg_in"], b["avg_out"]) / capacity * 100, 1),
            "peak_pct": round(max(b["peak_in"], b["peak_out"]) / capacity * 100, 1),
        })
    return points
