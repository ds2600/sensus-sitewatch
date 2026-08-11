"""Pull-only NetBox sync. Nothing here ever writes back to NetBox.

Deleted-in-NetBox records are never auto-deleted locally — they're flagged
out_of_sync=True so a human decides whether to remove them. NetBox isn't
fully populated yet on the deploying org's end, so silent deletes would be
actively dangerous here.
"""
import os
import pynetbox

from sitewatch.extensions import db
from sitewatch.models import Site, Device


def _client():
    url = os.environ.get("NETBOX_URL")
    token = os.environ.get("NETBOX_TOKEN")
    if not url or not token:
        raise RuntimeError("NETBOX_URL / NETBOX_TOKEN not set — sync disabled")
    return pynetbox.api(url, token=token)


def sync_sites():
    nb = _client()
    seen_ids = set()

    for nb_site in nb.dcim.sites.all():
        if nb_site.latitude is None or nb_site.longitude is None:
            continue  # can't place it on the map, skip rather than guess
        seen_ids.add(nb_site.id)
        site = Site.query.filter_by(netbox_id=nb_site.id).first()
        if site is None:
            site = Site(netbox_id=nb_site.id, source="netbox")
            db.session.add(site)
        site.name = nb_site.name
        site.lat = float(nb_site.latitude)
        site.lon = float(nb_site.longitude)
        site.out_of_sync = False

    for site in Site.query.filter_by(source="netbox").all():
        if site.netbox_id not in seen_ids:
            site.out_of_sync = True

    db.session.commit()


def sync_devices():
    nb = _client()
    seen_ids = set()

    for nb_device in nb.dcim.devices.all():
        if nb_device.primary_ip is None:
            continue  # no mgmt IP, nothing to poll
        site = Site.query.filter_by(netbox_id=nb_device.site.id).first()
        if site is None:
            continue  # sync sites first

        seen_ids.add(nb_device.id)
        device = Device.query.filter_by(netbox_id=nb_device.id).first()
        if device is None:
            device = Device(netbox_id=nb_device.id, source="netbox", vendor="ios-xe")
            db.session.add(device)
        device.hostname = nb_device.name
        device.mgmt_ip = str(nb_device.primary_ip).split("/")[0]
        device.site_id = site.id
        device.out_of_sync = False
        # SNMP credentials are never pulled from NetBox — set manually per device.

    for device in Device.query.filter_by(source="netbox").all():
        if device.netbox_id not in seen_ids:
            device.out_of_sync = True

    db.session.commit()
