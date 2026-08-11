"""Fake SNMP backend for testing without real device access. Activated by
SITEWATCH_SIMULATE=1 (see telemetry.py, the only module that imports this).

Scenario convention: an interface's if_alias can carry a tag like
"[sim:down]" — seed_demo.py sets these to script specific demo behavior.
Untagged interfaces default to "normal" (up, ~35% utilization).
"""
import random
import re
import time
from datetime import datetime

SCENARIOS = {
    "normal": {"oper": "up", "admin": "up", "util_pct": 0.35},
    "near_capacity": {"oper": "up", "admin": "up", "util_pct": 0.92},
    "down": {"oper": "down", "admin": "up", "util_pct": 0.0},
    "admin_down": {"oper": "down", "admin": "down", "util_pct": 0.0},
    "flapping": {"oper": "up", "admin": "up", "util_pct": 0.4},
}
SIM_TAG_RE = re.compile(r"\[sim:(\w+)\]")


def _scenario_for(iface):
    match = SIM_TAG_RE.search(iface.if_alias or "")
    key = match.group(1) if match else "normal"
    return key, SCENARIOS.get(key, SCENARIOS["normal"])


def check_reachable(device):
    # Demo convention: tag a device's hostname with "[sim:unreachable]" to
    # exercise the blue/unreachable state.
    return "[sim:unreachable]" not in (device.hostname or "")


def walk_interfaces(device):
    """Same four interfaces for every device, untagged (normal) by default.
    seed_demo.py tags specific ones after creation to script scenarios."""
    speed = 10_000_000_000  # 10G
    return {
        i: {"if_descr": f"GigabitEthernet0/0/{i}", "if_alias": "", "if_speed_bps": speed,
            "oper_status": "up", "admin_status": "up"}
        for i in range(1, 5)
    }


def poll_interface_counters(device, iface):
    key, scenario = _scenario_for(iface)
    now = time.time()
    oper = scenario["oper"]

    if key == "flapping":
        # Down for one third of every 6-minute window, deterministic so
        # every poll cycle within the window agrees.
        oper = "down" if int(now // 120) % 3 == 0 else "up"

    # Byte delta must scale with real elapsed time — poller.py computes bps
    # as (octet delta / real elapsed), so a wrong assumption here produces
    # wildly wrong utilization the moment poll cadence isn't exactly 120s
    # (e.g. running poll_all_devices() manually in a tight test loop).
    if iface.last_counter_at:
        elapsed_seconds = max(1, (datetime.utcnow() - iface.last_counter_at).total_seconds())
    else:
        elapsed_seconds = 1  # first poll ever: negligible delta, bps settles in on the next poll

    speed = iface.if_speed_bps or 1_000_000_000
    jitter = random.uniform(0.85, 1.15)
    bytes_added = int(speed * scenario["util_pct"] * jitter * elapsed_seconds / 8) if oper == "up" else 0

    return {
        "oper_status": oper,
        "admin_status": scenario["admin"],
        "in_octets": (iface.last_in_octets or 0) + bytes_added,
        "out_octets": (iface.last_out_octets or 0) + bytes_added,
    }
