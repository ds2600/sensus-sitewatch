"""Single entry point poller.py, the walk route, and the standalone probe
daemon (sitewatch/probe.py) use to reach devices. Routes to simulator.py
or snmp.py based on SITEWATCH_SIMULATE — nothing outside this module
should import snmp.py or simulator.py directly, so switching backends
never means touching call sites.

Deliberately has zero Flask/SQLAlchemy/APScheduler coupling — no db.session
access anywhere in this file — so sitewatch.probe can import it (and
transitively sitewatch.snmp) as a plain library on a remote box with none
of the web app's dependencies. DeviceSnapshot/IfaceSnapshot/
fetch_device_telemetry live here rather than in poller.py for the same
reason: they're the shared "fetch telemetry for a device" unit both the
in-process poller and a standalone probe need identically.
"""
import os
from dataclasses import dataclass

from sitewatch import snmp, simulator
from sitewatch.snmp import SnmpError
from sitewatch.snmp import new_engine as new_snmp_engine


def _simulate():
    return os.environ.get("SITEWATCH_SIMULATE") == "1"


def check_reachable(device, engine=None):
    return simulator.check_reachable(device) if _simulate() else snmp.check_reachable(device, engine=engine)


def walk_interfaces(device, engine=None):
    return simulator.walk_interfaces(device) if _simulate() else snmp.walk_interfaces(device, engine=engine)


def poll_interface_counters(device, iface, engine=None):
    if _simulate():
        return simulator.poll_interface_counters(device, iface)
    return snmp.poll_interface_counters(device, iface.if_index, engine=engine)


@dataclass
class IfaceSnapshot:
    """Plain-data mirror of the Interface columns this module actually
    reads. Callers (poller.py's worker threads, sitewatch/probe.py) pass
    one of these instead of a real ORM object — duck-types the same
    attributes, so this module needs no changes, but carries no session/
    engine reference an unsafe context (a worker thread, a whole separate
    process) could touch."""
    id: int
    if_index: int
    if_descr: str
    if_alias: str
    if_speed_bps: int
    last_counter_at: object
    last_in_octets: object
    last_out_octets: object


@dataclass
class DeviceSnapshot:
    """Plain-data mirror of the Device columns this module reads —
    credentials already decrypted. poller.py builds this from the ORM
    (decrypt() is pure computation, safe off the main thread, but the
    Device ORM object and its session are not); sitewatch/probe.py builds
    it from routes/probe_api.py's /api/probe/devices response, which
    hands back already-decrypted credentials the same shape."""
    id: int
    hostname: str
    mgmt_ip: str
    snmp_version: str
    snmp_community: str
    snmpv3_username: str
    snmpv3_auth_protocol: str
    snmpv3_auth_key: str
    snmpv3_priv_protocol: str
    snmpv3_priv_key: str
    interfaces: list


def fetch_device_telemetry(snapshot):
    """Network I/O only — reachability + one GET per monitored interface.
    Safe to run in a worker thread or a standalone process: touches
    nothing but the snapshot and snmp.py/simulator.py's own locals, no
    ORM/db.session access at all. Returns plain data; poller.py's
    _apply_fetch_result (or routes/probe_api.py's report handler, for a
    probe-reported result of this exact shape) does the actual ORM writes.

    One SnmpEngine is built here and reused for every GET this device
    makes this cycle (reachability + each interface's counters), instead
    of snmp.py's default of one per GET — SnmpEngine() construction is
    CPU-heavy (dispatcher/security/MIB subsystem setup), and with several
    devices doing that concurrently every cycle, it was enough GIL
    contention to make the whole process — including the web UI's request
    thread — lag during a sweep. Each caller here only ever handles one
    device at a time, so a fresh engine per call is thread-safe without
    any locking. Skipped entirely under SITEWATCH_SIMULATE=1 — the
    simulator backend ignores the engine param and never touches the
    network, so building a real one there is pure waste."""
    engine = None if _simulate() else new_snmp_engine()
    reachable = check_reachable(snapshot, engine=engine)
    result = {"reachable": reachable, "interfaces": {}}
    if not reachable:
        return result  # interfaces stay at last-known values, same as before
    for iface in snapshot.interfaces:
        try:
            result["interfaces"][iface.id] = {"data": poll_interface_counters(snapshot, iface, engine=engine)}
        except SnmpError as e:
            result["interfaces"][iface.id] = {"error": str(e)}
    return result
