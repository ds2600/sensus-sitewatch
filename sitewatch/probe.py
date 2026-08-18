"""Standalone remote poller — the Zabbix-proxy-style piece of SiteWatch.
Run this on a box that has local network access to devices the main
SiteWatch server can't reach directly (NAT/firewall/VPN segmentation):

    PROBE_SERVER_URL=https://sitewatch.example.com \\
    PROBE_API_KEY=<from Settings -> Probes> \\
    python -m sitewatch.probe

Deliberately imports nothing from sitewatch.extensions/sitewatch.models —
no Flask, no SQLAlchemy, no APScheduler. Only sitewatch.telemetry (and
transitively sitewatch.snmp/sitewatch.simulator) plus `requests`, so this
runs on a remote box with none of the web app's dependencies installed.
Same repo/checkout as the main app otherwise (this project's normal
`git pull`-based deployment) — just a different entrypoint, and this
process never sets SITEWATCH_RUN_POLLER (that flag starts the *in-process*
poller/scheduler, which has nothing to do with this script).

Two cadences, one plain loop (no threads — a single script doing one
HTTP-bound thing at a time doesn't need real concurrency, and avoiding it
keeps this file simple to read end to end):
  - Pending actions (routes/probe_api.py's /api/probe/pending-actions),
    checked every PROBE_ACTION_POLL_SECONDS (default 10) — a queued
    Walk/Repoll from the dashboard's Tail Modal, fulfilled here and
    reported back so the modal closes out promptly.
  - A full telemetry report, every PROBE_INTERVAL_MINUTES (default 2,
    matching this app's own polling_interval_minutes default) — pulls
    the current device/credential list fresh every time (so reassignment
    or a credential change on the server needs no probe restart), fetches
    each device via telemetry.fetch_device_telemetry, POSTs the batch to
    /api/probe/report.

SITEWATCH_SIMULATE=1 works here exactly like it does for the main app —
telemetry.py routes through simulator.py either way — so this can be
exercised end to end with zero real devices.
"""
import logging
import os
import sys
import time

import requests

from sitewatch import telemetry

log = logging.getLogger("sitewatch.probe")

DEFAULT_INTERVAL_MINUTES = 2
DEFAULT_ACTION_POLL_SECONDS = 10
HTTP_TIMEOUT = 30


class ProbeConfig:
    def __init__(self):
        self.server_url = os.environ["PROBE_SERVER_URL"].rstrip("/")
        self.api_key = os.environ["PROBE_API_KEY"]
        self.interval_seconds = int(os.environ.get("PROBE_INTERVAL_MINUTES", DEFAULT_INTERVAL_MINUTES)) * 60
        self.action_poll_seconds = int(os.environ.get("PROBE_ACTION_POLL_SECONDS", DEFAULT_ACTION_POLL_SECONDS))

    def headers(self):
        return {"Authorization": f"Bearer {self.api_key}"}


def _snapshot_from_payload(device_json):
    """Builds a telemetry.DeviceSnapshot from a device object as returned
    by /api/probe/devices or embedded in a /api/probe/pending-actions
    entry — same field names either way. last_counter_at/last_in_octets/
    last_out_octets aren't part of the wire payload (fetch_device_
    telemetry never reads them — that's server-side delta bookkeeping),
    so they're filled with None placeholders here just to satisfy the
    dataclass shape."""
    ifaces = [
        telemetry.IfaceSnapshot(
            id=i["id"], if_index=i["if_index"], if_descr=i["if_descr"], if_alias=i["if_alias"],
            if_speed_bps=i["if_speed_bps"], last_counter_at=None, last_in_octets=None, last_out_octets=None,
        )
        for i in device_json.get("interfaces", [])
    ]
    return telemetry.DeviceSnapshot(
        id=device_json["id"], hostname=device_json["hostname"], mgmt_ip=device_json["mgmt_ip"],
        snmp_version=device_json["snmp_version"], snmp_community=device_json["snmp_community"],
        snmpv3_username=device_json["snmpv3_username"], snmpv3_auth_protocol=device_json["snmpv3_auth_protocol"],
        snmpv3_auth_key=device_json["snmpv3_auth_key"], snmpv3_priv_protocol=device_json["snmpv3_priv_protocol"],
        snmpv3_priv_key=device_json["snmpv3_priv_key"], interfaces=ifaces,
    )


def run_telemetry_cycle(config):
    resp = requests.get(f"{config.server_url}/api/probe/devices", headers=config.headers(), timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    devices = resp.json()["devices"]
    log.info("Polling %d assigned device(s)...", len(devices))

    reports = []
    for device_json in devices:
        snapshot = _snapshot_from_payload(device_json)
        result = telemetry.fetch_device_telemetry(snapshot)
        reports.append({"device_id": device_json["id"], **result})

    if reports:
        resp = requests.post(f"{config.server_url}/api/probe/report", headers=config.headers(),
                              json={"devices": reports}, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
    log.info("Reported %d device(s).", len(reports))


def run_pending_actions(config):
    resp = requests.get(f"{config.server_url}/api/probe/pending-actions", headers=config.headers(),
                         timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    actions = resp.json()["actions"]
    for action in actions:
        action_id = action["action_id"]
        device_json = action["device"]
        snapshot = _snapshot_from_payload(device_json)
        if action["action"] == "walk":
            log.info("Walking %s (action %s)...", device_json["hostname"], action_id)
            discovered = telemetry.walk_interfaces(snapshot)
            resp = requests.post(f"{config.server_url}/api/probe/walk-result", headers=config.headers(),
                                  json={"action_id": action_id, "device_id": device_json["id"],
                                        "interfaces": discovered},
                                  timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
        else:  # repoll
            log.info("Repolling %s (action %s)...", device_json["hostname"], action_id)
            result = telemetry.fetch_device_telemetry(snapshot)
            resp = requests.post(f"{config.server_url}/api/probe/report", headers=config.headers(),
                                  json={"devices": [{"device_id": device_json["id"], "action_id": action_id,
                                                      **result}]},
                                  timeout=HTTP_TIMEOUT)
            resp.raise_for_status()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = ProbeConfig()
    log.info("Probe starting — server=%s, interval=%ds, action poll=%ds",
              config.server_url, config.interval_seconds, config.action_poll_seconds)

    next_report_due = 0.0
    while True:
        now = time.monotonic()
        try:
            if now >= next_report_due:
                run_telemetry_cycle(config)
                next_report_due = now + config.interval_seconds
        except requests.RequestException as e:
            log.warning("Telemetry cycle failed: %s", e)
        try:
            run_pending_actions(config)
        except requests.RequestException as e:
            log.warning("Pending-actions check failed: %s", e)
        time.sleep(config.action_poll_seconds)


if __name__ == "__main__":
    try:
        main()
    except KeyError as e:
        print(f"Missing required environment variable: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass
