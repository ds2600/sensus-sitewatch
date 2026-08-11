"""SNMP get/walk wrappers over pysnmp's synchronous hlapi.

Written against pysnmp 4.4.x hlapi (see requirements.txt pin). If you
upgrade pysnmp, this module is the one place that needs rework — the
v6.x API replaces these calls with an async style.
"""
from pysnmp.hlapi import (
    SnmpEngine, CommunityData, UsmUserData, UdpTransportTarget, ContextData,
    ObjectType, ObjectIdentity, getCmd, nextCmd,
    usmHMACSHAAuthProtocol, usmHMACMD5AuthProtocol, usmNoAuthProtocol,
    usmAesCfb128Protocol, usmDESPrivProtocol, usmNoPrivProtocol,
)

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
        mp_model = 0 if device.snmp_version == "v1" else 1
        return CommunityData(device.snmp_community, mpModel=mp_model)
    return UsmUserData(
        device.snmpv3_username,
        authKey=device.snmpv3_auth_key,
        privKey=device.snmpv3_priv_key,
        authProtocol=_AUTH_PROTOCOLS.get(device.snmpv3_auth_protocol, usmNoAuthProtocol),
        privProtocol=_PRIV_PROTOCOLS.get(device.snmpv3_priv_protocol, usmNoPrivProtocol),
    )


def snmp_get(device, oid, timeout=3, retries=1):
    engine = SnmpEngine()
    iterator = getCmd(
        engine, _auth_data(device), UdpTransportTarget((device.mgmt_ip, 161), timeout=timeout, retries=retries),
        ContextData(), ObjectType(ObjectIdentity(oid)),
    )
    error_indication, error_status, _, var_binds = next(iterator)
    if error_indication or error_status:
        raise SnmpError(str(error_indication or error_status))
    return var_binds[0][1]


def snmp_walk(device, base_oid, timeout=3, retries=1):
    """Returns list of (index, value) tuples, index parsed as int from the
    trailing OID component."""
    engine = SnmpEngine()
    results = []
    iterator = nextCmd(
        engine, _auth_data(device), UdpTransportTarget((device.mgmt_ip, 161), timeout=timeout, retries=retries),
        ContextData(), ObjectType(ObjectIdentity(base_oid)), lexicographicMode=False,
    )
    for error_indication, error_status, _, var_binds in iterator:
        if error_indication or error_status:
            raise SnmpError(str(error_indication or error_status))
        for oid, value in var_binds:
            index = int(str(oid).split(".")[-1])
            results.append((index, value))
    return results


def check_reachable(device):
    try:
        snmp_get(device, OID_SYS_UPTIME)
        return True
    except SnmpError:
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
