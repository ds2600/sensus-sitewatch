"""Shared walk logic — used by the manual "Walk now" route and by
seed_demo.py, so both go through one code path."""
import logging
from datetime import datetime
from sitewatch.extensions import db
from sitewatch.models import Interface
from sitewatch import telemetry

log = logging.getLogger(__name__)


def perform_walk(device):
    log.info("Starting walk of %s (%s)...", device.hostname, device.mgmt_ip)
    discovered = telemetry.walk_interfaces(device)
    existing = {i.if_index: i for i in device.interfaces}
    new_count = 0
    for idx, data in discovered.items():
        if idx in existing:
            iface = existing[idx]
            iface.if_descr = data["if_descr"]
            iface.if_alias = data["if_alias"]
            iface.if_speed_bps = data["if_speed_bps"]
        else:
            db.session.add(Interface(device_id=device.id, if_index=idx, **data))
            new_count += 1
    device.last_walked_at = datetime.utcnow()
    log.info("Walk of %s complete: %d interface(s) total (%d new).",
              device.hostname, len(discovered), new_count)
    return len(discovered)
