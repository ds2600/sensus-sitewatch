"""Builds a small realistic topology using the simulator backend, so the
app can be exercised end to end without real device access. Run via
`flask --app app seed-demo`, then run the server with SITEWATCH_SIMULATE=1
so the poller reads simulated telemetry back for these devices.

Scenarios scripted here (see simulator.py for what each tag does):
  - CHI-DEN-Core (bundle, critical): both members up. Chicago/Denver core
    path stays fully green. One member tagged near_capacity to show
    utilization without affecting status.
  - CHI-DEN-Office (auxiliary): tagged down. Demonstrates yellow, not red —
    the critical core bundle above is untouched.
  - CHI-DEN-Office-Backup (auxiliary): tagged admin_down. Demonstrates gray,
    excluded from status math entirely.
  - CHI-AUS-Core (critical, single degree): tagged down. Austin has only
    this one path out, so this demonstrates red.
  - DEN-PHX-Core (critical, single degree): Phoenix's device is tagged
    unreachable, so this circuit reads unreachable — demonstrates a real
    line on the map going blue, not just an isolated site marker.
  - Phoenix device hostname tagged [sim:unreachable]: demonstrates blue,
    both at the device and site level.

Runs several real poll cycles at the end (see _drive_poll_cycles below) so
the map/dashboard show correct states the moment you load them — you don't
need SITEWATCH_RUN_POLLER=1 running for the demo data itself to look right,
though you do need it if you want states to keep updating live afterward.
"""
from datetime import timedelta
from sitewatch.extensions import db
from sitewatch.models import Site, Device, Circuit, CircuitRole, Interface, Setting
from sitewatch.discovery import perform_walk


def _iface(device, descr):
    return Interface.query.filter_by(device_id=device.id, if_descr=descr).first()


def _tag(iface, scenario):
    iface.if_alias = f"[sim:{scenario}]"


def run():
    core_role = CircuitRole.query.filter_by(name="core").first()
    if core_role is None:
        core_role = CircuitRole(name="core", tier="critical")
        db.session.add(core_role)
    office_role = CircuitRole.query.filter_by(name="office").first()
    if office_role is None:
        office_role = CircuitRole(name="office", tier="auxiliary")
        db.session.add(office_role)
    db.session.flush()

    chicago = Site(name="Chicago DC", lat=41.8781, lon=-87.6298, source="manual")
    denver = Site(name="Denver DC", lat=39.7392, lon=-104.9903, source="manual")
    phoenix = Site(name="Phoenix DC", lat=33.4484, lon=-112.0740, source="manual")
    austin = Site(name="Austin DC", lat=30.2672, lon=-97.7431, source="manual")
    db.session.add_all([chicago, denver, phoenix, austin])
    db.session.flush()

    def make_device(site, hostname, ip, vendor):
        d = Device(site_id=site.id, hostname=hostname, mgmt_ip=ip, vendor=vendor, snmp_version="v2c")
        d.snmp_community = "public"
        db.session.add(d)
        return d

    chi_core = make_device(chicago, "chi-core-01", "10.0.1.1", "ios-xe")
    chi_office = make_device(chicago, "chi-office-sw01", "10.0.1.2", "junos")
    den_core = make_device(denver, "den-core-01", "10.0.2.1", "ios-xr")
    den_office = make_device(denver, "den-office-sw01", "10.0.2.2", "junos")
    phx_core = make_device(phoenix, "phx-core-01 [sim:unreachable]", "10.0.3.1", "junos")
    aus_core = make_device(austin, "aus-core-01", "10.0.4.1", "ios-xe")
    db.session.flush()

    for d in [chi_core, chi_office, den_core, den_office, phx_core, aus_core]:
        perform_walk(d)
    db.session.flush()

    # Core bundle: both members up, one near capacity (up, high utilization).
    bundle = Circuit(name="CHI-DEN-Core", role_id=core_role.id)
    db.session.add(bundle)
    db.session.flush()
    member1_a = _iface(chi_core, "GigabitEthernet0/0/1")
    member2_a = _iface(chi_core, "GigabitEthernet0/0/2")
    _tag(member2_a, "near_capacity")
    db.session.add(Circuit(name="CHI-DEN-Core-1", role_id=core_role.id, parent_circuit_id=bundle.id,
                            interface_a_id=member1_a.id, interface_b_id=_iface(den_core, "GigabitEthernet0/0/1").id))
    db.session.add(Circuit(name="CHI-DEN-Core-2", role_id=core_role.id, parent_circuit_id=bundle.id,
                            interface_a_id=member2_a.id, interface_b_id=_iface(den_core, "GigabitEthernet0/0/2").id))

    # Auxiliary office link, down — site should read yellow, not red.
    office_a = _iface(chi_office, "GigabitEthernet0/0/1")
    _tag(office_a, "down")
    db.session.add(Circuit(name="CHI-DEN-Office", role_id=office_role.id,
                            interface_a_id=office_a.id, interface_b_id=_iface(den_office, "GigabitEthernet0/0/1").id))

    # Auxiliary link, admin-down — should render gray, excluded from status.
    office_backup_a = _iface(chi_office, "GigabitEthernet0/0/3")
    _tag(office_backup_a, "admin_down")
    db.session.add(Circuit(name="CHI-DEN-Office-Backup", role_id=office_role.id,
                            interface_a_id=office_backup_a.id, interface_b_id=_iface(den_office, "GigabitEthernet0/0/2").id))

    # Chicago intra-site link, left up.
    db.session.add(Circuit(name="CHI-Internal", role_id=core_role.id,
                            interface_a_id=_iface(chi_core, "GigabitEthernet0/0/3").id,
                            interface_b_id=_iface(chi_office, "GigabitEthernet0/0/2").id))

    # Single critical degree to Austin, down — Austin should read red.
    aus_a = _iface(chi_core, "GigabitEthernet0/0/4")
    _tag(aus_a, "down")
    db.session.add(Circuit(name="CHI-AUS-Core", role_id=core_role.id,
                            interface_a_id=aus_a.id, interface_b_id=_iface(aus_core, "GigabitEthernet0/0/1").id))

    # Single critical degree to Phoenix. Phoenix's device is unreachable, so
    # this circuit reads unreachable regardless of any tag on its interfaces
    # — shown on the map as a blue line, not just a blue dot with nothing
    # connecting to it.
    db.session.add(Circuit(name="DEN-PHX-Core", role_id=core_role.id,
                            interface_a_id=_iface(den_core, "GigabitEthernet0/0/3").id,
                            interface_b_id=_iface(phx_core, "GigabitEthernet0/0/1").id))

    db.session.commit()
    _drive_poll_cycles()


def _drive_poll_cycles():
    """Runs poll_all_devices() enough times to clear the down-threshold
    debounce, so seeded scenarios show their real state immediately. Then
    backdates interface counters by one polling interval and polls once
    more, so utilization numbers reflect a real interval's worth of traffic
    instead of the near-zero delta you'd get from calling the poller
    several times in a tight loop with no real time elapsed between calls.
    """
    from sitewatch.poller import poll_all_devices  # imported here to avoid loading it for every seed_demo import

    threshold = Setting.get_int("down_threshold_count")
    for _ in range(threshold):
        poll_all_devices()

    interval_minutes = Setting.get_int("polling_interval_minutes")
    for iface in Interface.query.all():
        if iface.last_counter_at:
            iface.last_counter_at -= timedelta(minutes=interval_minutes)
    db.session.commit()
    poll_all_devices()
