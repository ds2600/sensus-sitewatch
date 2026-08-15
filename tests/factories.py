"""Minimal ORM object builders shared across test files — keeps individual
tests focused on the behavior under test instead of repeating verbose
Site/Device/Interface/Circuit setup. Every helper flushes so the returned
object has a real id, but never commits — callers/fixtures own the
transaction boundary.
"""
from sitewatch.extensions import db
from sitewatch.models import Site, Device, Interface, CircuitRole, Circuit, Layer, CustomFieldDefinition

_lat = [40.0]  # cheap incrementing coordinate so sites never collide


def make_site(name="Test Site", site_type="site", parent_site=None, reachable_devices=True, layer=None):
    _lat[0] += 0.1
    site = Site(name=name, lat=_lat[0], lon=-90.0, site_type=site_type,
                parent_site_id=parent_site.id if parent_site else None,
                layer_id=layer.id if layer else None)
    db.session.add(site)
    db.session.flush()
    return site


_layer_seq = [0]


def make_layer(name=None):
    _layer_seq[0] += 1
    layer = Layer(name=name or f"layer-{_layer_seq[0]}")
    db.session.add(layer)
    db.session.flush()
    return layer


_field_seq = [0]


def make_custom_field(object_type, name=None):
    _field_seq[0] += 1
    field = CustomFieldDefinition(name=name or f"field-{_field_seq[0]}", object_type=object_type)
    db.session.add(field)
    db.session.flush()
    return field


def make_device(site, hostname=None, reachable=True, layer=None):
    hostname = hostname or f"{site.name.lower().replace(' ', '-')}-dev"
    device = Device(site_id=site.id, hostname=hostname, mgmt_ip="10.0.0.1",
                     vendor="ios-xe", snmp_version="v2c", reachable=reachable,
                     layer_id=layer.id if layer else None)
    db.session.add(device)
    db.session.flush()
    return device


_if_index = [0]


def make_interface(device, oper_status="up", admin_status="up"):
    _if_index[0] += 1
    iface = Interface(device_id=device.id, if_index=_if_index[0], if_descr=f"Gi0/{_if_index[0]}",
                       oper_status=oper_status, admin_status=admin_status)
    db.session.add(iface)
    db.session.flush()
    return iface


_role_seq = [0]


def make_role(name=None, tier="critical"):
    _role_seq[0] += 1
    name = name or f"role-{tier}-{_role_seq[0]}"
    role = CircuitRole(name=name, tier=tier)
    db.session.add(role)
    db.session.flush()
    return role


def make_circuit(interface_a, interface_b, role=None, current_state="up", name=None, parent=None, layer=None):
    role = role or make_role()
    circuit = Circuit(
        name=name or f"{interface_a.device.hostname}-{interface_b.device.hostname}",
        role_id=role.id, interface_a_id=interface_a.id, interface_b_id=interface_b.id,
        current_state=current_state, parent_circuit_id=parent.id if parent else None,
        layer_id=layer.id if layer else None,
    )
    db.session.add(circuit)
    db.session.flush()
    return circuit


def make_bundle(role=None, current_state="up", name="bundle"):
    role = role or make_role()
    bundle = Circuit(name=name, role_id=role.id, current_state=current_state)
    db.session.add(bundle)
    db.session.flush()
    return bundle


def link_devices(site_a, site_b, role=None, current_state="up", name=None):
    """The common case: one circuit between one device at each of two
    sites — returns the Circuit."""
    dev_a = make_device(site_a)
    dev_b = make_device(site_b)
    iface_a = make_interface(dev_a)
    iface_b = make_interface(dev_b)
    return make_circuit(iface_a, iface_b, role=role, current_state=current_state, name=name)
