"""Single entry point poller.py and the walk route use to reach devices.
Routes to simulator.py or snmp.py based on SITEWATCH_SIMULATE — nothing
outside this module should import snmp.py or simulator.py directly, so
switching backends never means touching call sites.
"""
import os
from sitewatch import snmp, simulator


def _simulate():
    return os.environ.get("SITEWATCH_SIMULATE") == "1"


def check_reachable(device):
    return simulator.check_reachable(device) if _simulate() else snmp.check_reachable(device)


def walk_interfaces(device):
    return simulator.walk_interfaces(device) if _simulate() else snmp.walk_interfaces(device)


def poll_interface_counters(device, iface):
    if _simulate():
        return simulator.poll_interface_counters(device, iface)
    return snmp.poll_interface_counters(device, iface.if_index)
