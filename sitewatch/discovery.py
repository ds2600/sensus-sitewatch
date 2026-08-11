"""Shared walk logic — used by the manual "Walk now" route and by
seed_demo.py, so both go through one code path."""
from datetime import datetime
from sitewatch.extensions import db
from sitewatch.models import Interface
from sitewatch import telemetry


def perform_walk(device):
    discovered = telemetry.walk_interfaces(device)
    existing = {i.if_index: i for i in device.interfaces}
    for idx, data in discovered.items():
        if idx in existing:
            iface = existing[idx]
            iface.if_descr = data["if_descr"]
            iface.if_alias = data["if_alias"]
            iface.if_speed_bps = data["if_speed_bps"]
        else:
            db.session.add(Interface(device_id=device.id, if_index=idx, **data))
    device.last_walked_at = datetime.utcnow()
    return len(discovered)
