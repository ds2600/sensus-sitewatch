"""SNMP get/walk wrappers over pysnmp's synchronous hlapi.

Written against pysnmp 4.4.x hlapi (see requirements.txt pin). If you
upgrade pysnmp, this module is the one place that needs rework — the
v6.x API replaces these calls with an async style.

Every GET/WALK logs at INFO — this is deliberately chatty. It's what feeds
the live log tail in the walk/repoll modal (see job_log.py): each line here
is a real wire-level operation (target, OID, outcome, timing), not a
generic "polling device..." status message.
"""
import logging
import time

from pysnmp.error import PySnmpError
from pysnmp.proto.rfc1905 import NoSuchInstance, NoSuchObject, EndOfMibView
from pysnmp.hlapi import (
    SnmpEngine, CommunityData, UsmUserData, UdpTransportTarget, ContextData,
    ObjectType, ObjectIdentity, getCmd, nextCmd,
    usmHMACSHAAuthProtocol, usmHMACMD5AuthProtocol, usmNoAuthProtocol,
    usmAesCfb128Protocol, usmDESPrivProtocol, usmNoPrivProtocol,
)

# pysnmp's stand-in values for "this OID doesn't exist here" — returned in the
# varbind itself rather than as an error_indication/error_status, so callers
# that don't check for these end up trying to int()/str() a sentinel object
# (e.g. NoSuchInstance stringifies to '', which crashes int('')).
_MISSING_VALUE_TYPES = (NoSuchInstance, NoSuchObject, EndOfMibView)

log = logging.getLogger(__name__)

# Human-readable names for the OIDs this module ever queries, purely for
# log output — falls back to the raw OID for anything not in this table.
_OID_NAMES = {
    "1.3.6.1.2.1.1.3.0": "sysUpTime",
    "1.3.6.1.2.1.2.2.1.2": "ifDescr",
    "1.3.6.1.2.1.31.1.1.1.18": "ifAlias",
    "1.3.6.1.2.1.2.2.1.5": "ifSpeed",
    "1.3.6.1.2.1.2.2.1.8": "ifOperStatus",
    "1.3.6.1.2.1.2.2.1.7": "ifAdminStatus",
    "1.3.6.1.2.1.31.1.1.1.6": "ifHCInOctets",
    "1.3.6.1.2.1.31.1.1.1.10": "ifHCOutOctets",
}


def _oid_label(oid):
    base = oid.rsplit(".", 1)[0] if oid[-1].isdigit() and oid not in _OID_NAMES else oid
    name = _OID_NAMES.get(oid) or _OID_NAMES.get(base)
    return f"{name} ({oid})" if name else oid

# IF-MIB OIDs used throughout the app
OID_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_ALIAS = "1.3.6.1.2.1.31.1.1.1.18"
OID_IF_SPEED = "1.3.6.1.2.1.2.2.1.5"
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
OID_IF_ADMIN_STATUS = "1.3.6.1.2.1.2.2.1.7"
OID_IF_HC_IN_OCTETS = "1.3.6.1.2.1.31.1.1.1.6"
OID_IF_HC_OUT_OCTETS = "1.3.6.1.2.1.31.1.1.1.10"

_AUTH_PROTOCOLS = {"SHA": usmHMACSHAAuthProtocol, "MD5": usmHMACMD5AuthProtocol, "none": usmNoAuthProtocol}
_PRIV_PROTOCOLS = {"AES": usmAesCfb128Protocol, "DES": usmDESPrivProtocol, "none": usmNoPrivProtocol}


class SnmpError(Exception):
    pass


def _auth_data(device):
    if device.snmp_version in ("v1", "v2c"):
        if not device.snmp_community:
            raise SnmpError("No SNMP community configured for this device.")
        mp_model = 0 if device.snmp_version == "v1" else 1
        return CommunityData(device.snmp_community, mpModel=mp_model)
    if not device.snmpv3_username:
        raise SnmpError("No SNMPv3 credentials configured for this device.")
    return UsmUserData(
        device.snmpv3_username,
        authKey=device.snmpv3_auth_key,
        privKey=device.snmpv3_priv_key,
        authProtocol=_AUTH_PROTOCOLS.get(device.snmpv3_auth_protocol, usmNoAuthProtocol),
        privProtocol=_PRIV_PROTOCOLS.get(device.snmpv3_priv_protocol, usmNoPrivProtocol),
    )


def snmp_get(device, oid, timeout=3, retries=1):
    t0 = time.monotonic()
    log.info("GET %s from %s (%s, timeout=%ss retries=%s)",
              _oid_label(oid), device.mgmt_ip, device.snmp_version, timeout, retries)
    engine = SnmpEngine()
    iterator = getCmd(
        engine, _auth_data(device), UdpTransportTarget((device.mgmt_ip, 161), timeout=timeout, retries=retries),
        ContextData(), ObjectType(ObjectIdentity(oid)),
    )
    try:
        error_indication, error_status, _, var_binds = next(iterator)
    except PySnmpError as e:
        # Malformed/missing credentials (e.g. a device imported from NetBox,
        # which never carries SNMP credentials) surface here as pysnmp
        # internals, not as error_indication — normalize to SnmpError so
        # every caller only has one exception type to handle.
        log.info("  -> FAILED after %.2fs: %s", time.monotonic() - t0, e)
        raise SnmpError(str(e)) from e
    if error_indication or error_status:
        log.info("  -> FAILED after %.2fs: %s", time.monotonic() - t0, error_indication or error_status)
        raise SnmpError(str(error_indication or error_status))
    value = var_binds[0][1]
    if isinstance(value, _MISSING_VALUE_TYPES):
        log.info("  -> no such instance (%.2fs)", time.monotonic() - t0)
        raise SnmpError(f"{oid} not present on this device (no such instance).")
    log.info("  -> %s (%.2fs)", value, time.monotonic() - t0)
    return value


def snmp_walk(device, base_oid, timeout=3, retries=1):
    """Returns list of (index, value) tuples, index parsed as int from the
    trailing OID component."""
    t0 = time.monotonic()
    log.info("WALK %s from %s (%s, timeout=%ss retries=%s)",
              _oid_label(base_oid), device.mgmt_ip, device.snmp_version, timeout, retries)
    engine = SnmpEngine()
    results = []
    iterator = nextCmd(
        engine, _auth_data(device), UdpTransportTarget((device.mgmt_ip, 161), timeout=timeout, retries=retries),
        ContextData(), ObjectType(ObjectIdentity(base_oid)), lexicographicMode=False,
    )
    try:
        for error_indication, error_status, _, var_binds in iterator:
            if error_indication or error_status:
                log.info("  -> FAILED after %.2fs: %s", time.monotonic() - t0, error_indication or error_status)
                raise SnmpError(str(error_indication or error_status))
            for oid, value in var_binds:
                if isinstance(value, _MISSING_VALUE_TYPES):
                    continue
                index = int(str(oid).split(".")[-1])
                results.append((index, value))
    except PySnmpError as e:
        log.info("  -> FAILED after %.2fs: %s", time.monotonic() - t0, e)
        raise SnmpError(str(e)) from e
    log.info("  -> %d result(s) (%.2fs)", len(results), time.monotonic() - t0)
    return results


def check_reachable(device):
    log.info("Checking reachability of %s (%s)...", device.hostname, device.mgmt_ip)
    try:
        snmp_get(device, OID_SYS_UPTIME)
        log.info("%s is reachable.", device.hostname)
        return True
    except SnmpError as e:
        log.info("%s is unreachable: %s", device.hostname, e)
        return False


def walk_interfaces(device):
    """Full IF-MIB walk. Returns dict keyed by if_index."""
    descr = dict(snmp_walk(device, OID_IF_DESCR))
    alias = dict(snmp_walk(device, OID_IF_ALIAS))
    speed = dict(snmp_walk(device, OID_IF_SPEED))
    oper = dict(snmp_walk(device, OID_IF_OPER_STATUS))
    admin = dict(snmp_walk(device, OID_IF_ADMIN_STATUS))

    out = {}
    for idx, val in descr.items():
        out[idx] = {
            "if_descr": str(val),
            "if_alias": str(alias.get(idx, "")),
            "if_speed_bps": int(speed[idx]) if idx in speed else None,
            "oper_status": "up" if str(oper.get(idx)) == "1" else "down",
            "admin_status": "up" if str(admin.get(idx)) == "1" else "down",
        }
    log.info("Discovered %d interface(s) on %s.", len(out), device.hostname)
    return out


def poll_interface_counters(device, if_index):
    """GET (not walk) for a single interface's live status + counters. Used
    on every regular poll cycle — full walks only happen on manual re-walk."""
    oper = snmp_get(device, f"{OID_IF_OPER_STATUS}.{if_index}")
    admin = snmp_get(device, f"{OID_IF_ADMIN_STATUS}.{if_index}")
    in_octets = snmp_get(device, f"{OID_IF_HC_IN_OCTETS}.{if_index}")
    out_octets = snmp_get(device, f"{OID_IF_HC_OUT_OCTETS}.{if_index}")
    return {
        "oper_status": "up" if str(oper) == "1" else "down",
        "admin_status": "up" if str(admin) == "1" else "down",
        "in_octets": int(in_octets),
        "out_octets": int(out_octets),
    }
