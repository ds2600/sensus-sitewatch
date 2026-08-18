"""HTTP surface a standalone probe (sitewatch/probe.py) talks to — entirely
separate from api.py, which is all browser-session (@login_required) auth.
Every route here uses @probe_required (auth.py) instead: an Authorization:
Bearer <key> header checked against Probe.api_key, with g.probe set on
success. See the module docstrings on poller.py (apply_probe_report) and
models.py (Probe, ProbeAction) for the design this implements.

Must be served over TLS in production — same reverse-proxy deployment
guidance (README) as the rest of the app; nothing new needed in-app.
"""
from datetime import datetime

from flask import Blueprint, jsonify, request, g, abort

from sitewatch.extensions import db
from sitewatch.auth import probe_required
from sitewatch.models import Device, ProbeAction
from sitewatch.poller import apply_probe_report, monitored_interface_ids
from sitewatch.discovery import apply_walk_result
from sitewatch import job_log

probe_api_bp = Blueprint("probe_api", __name__, url_prefix="/api/probe")


def _credential_fields(device):
    return {
        "snmp_version": device.snmp_version, "snmp_community": device.snmp_community,
        "snmpv3_username": device.snmpv3_username, "snmpv3_auth_protocol": device.snmpv3_auth_protocol,
        "snmpv3_auth_key": device.snmpv3_auth_key, "snmpv3_priv_protocol": device.snmpv3_priv_protocol,
        "snmpv3_priv_key": device.snmpv3_priv_key,
    }


def _iface_fields(iface):
    return {"id": iface.id, "if_index": iface.if_index, "if_descr": iface.if_descr,
            "if_alias": iface.if_alias, "if_speed_bps": iface.if_speed_bps}


@probe_api_bp.route("/devices")
@probe_required
def probe_devices():
    """The probe's assigned devices, with decrypted credentials and their
    monitored (circuit-linked) interfaces — everything sitewatch/probe.py
    needs to build its own telemetry.DeviceSnapshot/IfaceSnapshot and poll
    locally. Pulled at startup and periodically (probe's own timer) to
    pick up reassignment/credential changes without a probe restart."""
    monitored_ids = monitored_interface_ids()
    devices = Device.query.filter_by(probe_id=g.probe.id).all()
    out = [
        {"id": d.id, "hostname": d.hostname, "mgmt_ip": d.mgmt_ip, **_credential_fields(d),
         "interfaces": [_iface_fields(i) for i in d.interfaces if i.id in monitored_ids]}
        for d in devices
    ]
    return jsonify({"devices": out})


@probe_api_bp.route("/report", methods=["POST"])
@probe_required
def probe_report():
    """Regular telemetry check-in — body: {"devices": [{"device_id",
    "reachable", "interfaces": {if_id: {"data"|"error": ...}}, "action_id"
    (optional)}, ...]}, each device entry the same shape telemetry.
    fetch_device_telemetry() returns (JSON round-trips dict keys as
    strings, so interface ids are cast back to int here). action_id, when
    present, closes out a queued "repoll" ProbeAction/Tail-Modal job —
    see routes/devices.py's repoll_device for where it's queued."""
    data = request.get_json(force=True) or {}
    entries = data.get("devices", [])
    reports = [
        {"device_id": entry["device_id"], "reachable": entry["reachable"],
         "interfaces": {int(k): v for k, v in entry.get("interfaces", {}).items()}}
        for entry in entries
    ]
    apply_probe_report(g.probe, reports)

    for entry in entries:
        action_id = entry.get("action_id")
        if not action_id:
            continue
        action = ProbeAction.query.get(action_id)
        if action is None or action.probe_id != g.probe.id or action.completed_at is not None:
            continue
        action.completed_at = datetime.utcnow()
        job_log.log_line(action.job_id, "Repoll complete.")
        job_log.finish_job(action.job_id, success=True)
    db.session.commit()
    return jsonify({"ok": True})


@probe_api_bp.route("/pending-actions")
@probe_required
def probe_pending_actions():
    """Polled frequently (~10s, the probe's own fast timer, separate from
    its coarser telemetry-report interval) so a Walk/Repoll click still
    feels close to the few-seconds responsiveness a directly-polled
    device has today. Returns this probe's not-yet-completed ProbeAction
    rows, each bundled with the target device's credentials (and, for a
    "repoll", its monitored interfaces — a "walk" re-discovers everything
    fresh, so it doesn't need an interface list at all)."""
    monitored_ids = monitored_interface_ids()
    actions = ProbeAction.query.filter_by(probe_id=g.probe.id, completed_at=None).all()
    out = []
    for action in actions:
        device = action.device
        payload = {"id": device.id, "hostname": device.hostname, "mgmt_ip": device.mgmt_ip,
                   **_credential_fields(device)}
        if action.action == "repoll":
            payload["interfaces"] = [_iface_fields(i) for i in device.interfaces if i.id in monitored_ids]
        out.append({"action_id": action.id, "action": action.action, "device": payload})
    return jsonify({"actions": out})


@probe_api_bp.route("/walk-result", methods=["POST"])
@probe_required
def probe_walk_result():
    """Closes out a queued "walk" ProbeAction — body: {"action_id",
    "device_id", "interfaces": {if_index: {"if_descr", "if_alias",
    "if_speed_bps", "oper_status", "admin_status"}}}, the same shape
    telemetry.walk_interfaces() returns (if_index keys cast back to int,
    same JSON round-trip as /report's interface ids)."""
    data = request.get_json(force=True) or {}
    action = ProbeAction.query.get_or_404(data["action_id"])
    if action.probe_id != g.probe.id:
        abort(403)
    device = Device.query.get_or_404(data["device_id"])
    discovered = {int(k): v for k, v in data.get("interfaces", {}).items()}
    count = apply_walk_result(device, discovered)
    action.completed_at = datetime.utcnow()
    g.probe.last_seen_at = datetime.utcnow()
    db.session.commit()
    job_log.log_line(action.job_id, f"Walk complete: {count} interface(s).")
    job_log.finish_job(action.job_id, success=True)
    return jsonify({"ok": True})
