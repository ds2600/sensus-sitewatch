from datetime import datetime, timedelta
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from sitewatch.extensions import db
from sitewatch.crypto import encrypt, decrypt

# --- Reference values -------------------------------------------------
# Kept as plain strings (not DB enums) so SQLite stays simple. Validated
# in code wherever they're set.
SNMP_VERSIONS = ("v1", "v2c", "v3")
VENDORS = ("ios-xe", "ios-xr", "junos")
SOURCES = ("manual", "netbox")
CIRCUIT_TIERS = ("critical", "auxiliary")
CIRCUIT_STATES = ("up", "degraded", "down", "admin_down", "unreachable")
SITE_TYPES = ("site", "passthrough", "minor")
USER_ROLES = ("admin", "read_only")


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    # NOT NULL with server_default (not just default=) — schema_sync.py adds
    # this column via raw ALTER TABLE, and SQLite refuses a NOT NULL column
    # add with no DDL-level default on a non-empty table. server_default
    # makes SQLite backfill every existing row (the admin seeded by
    # `flask init-db` on any pre-roles deployment) with "admin" for free.
    role = db.Column(db.String(20), nullable=False, default="admin", server_default="admin")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == "admin"


class Setting(db.Model):
    """Single-row key/value settings table. Access via Setting.get/set."""
    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.String(255))

    DEFAULTS = {
        "polling_interval_minutes": "2",
        "down_threshold_count": "3",
        "mute_max_minutes": "60",
        "google_chat_webhook_url": "",
        "sitewatch_url": "",
        "status_history_retention_days": "30",
        "audit_log_retention_days": "90",
        "poller_max_workers": "8",
        "incident_number_prefix": "INC",
        "poll_on_startup": "0",
    }

    @staticmethod
    def get(key, default=None):
        row = Setting.query.get(key)
        if row is None:
            return default if default is not None else Setting.DEFAULTS.get(key)
        return row.value

    @staticmethod
    def get_int(key, default=None):
        return int(Setting.get(key, default))

    @staticmethod
    def set(key, value):
        row = Setting.query.get(key)
        if row is None:
            row = Setting(key=key, value=str(value))
            db.session.add(row)
        else:
            row.value = str(value)

    @staticmethod
    def seed_defaults():
        for k, v in Setting.DEFAULTS.items():
            if Setting.query.get(k) is None:
                db.session.add(Setting(key=k, value=v))


class Region(db.Model):
    """Organizational grouping on Site — for sorting/filtering/search
    (Sites list, quick search). Unrelated to MapRegion, which is a saved
    map camera position with no site membership; the map's region picker
    auto-fits to a Region's member sites instead of storing its own
    lat/lon/zoom, so the two never need to be the same model."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Site(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    lat = db.Column(db.Float, nullable=False)
    lon = db.Column(db.Float, nullable=False)
    site_type = db.Column(db.String(20), default="site")  # site | passthrough | minor — see SITE_TYPES
    # Only meaningful when site_type == "minor" — the Major Site (site_type
    # == "site") this one's status can cascade from. One level only: a
    # minor site's parent must itself be a plain Major Site, never another
    # minor site or a passthrough (enforced in routes/sites.py, not here) —
    # that's what keeps compute_site_status()'s parent-status recursion
    # (status.py) safely bounded to a single hop.
    parent_site_id = db.Column(db.Integer, db.ForeignKey("site.id"), nullable=True)
    region_id = db.Column(db.Integer, db.ForeignKey("region.id"), nullable=True)
    netbox_id = db.Column(db.Integer, nullable=True)
    source = db.Column(db.String(20), default="manual")
    out_of_sync = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    devices = db.relationship("Device", backref="site", lazy=True)
    region = db.relationship("Region", backref="sites")
    minor_sites = db.relationship("Site", backref=db.backref("parent_site", remote_side=[id]), lazy=True)


class Device(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    site_id = db.Column(db.Integer, db.ForeignKey("site.id"), nullable=False)
    hostname = db.Column(db.String(120), nullable=False)
    mgmt_ip = db.Column(db.String(64), nullable=False)
    vendor = db.Column(db.String(20), nullable=False)

    snmp_version = db.Column(db.String(8), default="v2c")
    snmp_community_enc = db.Column(db.String(255), nullable=True)
    snmpv3_username = db.Column(db.String(80), nullable=True)
    snmpv3_auth_protocol = db.Column(db.String(20), nullable=True)
    snmpv3_auth_key_enc = db.Column(db.String(255), nullable=True)
    snmpv3_priv_protocol = db.Column(db.String(20), nullable=True)
    snmpv3_priv_key_enc = db.Column(db.String(255), nullable=True)

    ssh_username = db.Column(db.String(80), nullable=True)
    ssh_password_enc = db.Column(db.String(255), nullable=True)

    netbox_id = db.Column(db.Integer, nullable=True)
    source = db.Column(db.String(20), default="manual")
    out_of_sync = db.Column(db.Boolean, default=False)

    reachable = db.Column(db.Boolean, default=True)
    last_walked_at = db.Column(db.DateTime, nullable=True)
    last_polled_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    interfaces = db.relationship("Interface", backref="device", lazy=True,
                                  cascade="all, delete-orphan")

    # --- credential helpers: callers never touch *_enc columns directly ---
    @property
    def snmp_community(self):
        return decrypt(self.snmp_community_enc)

    @snmp_community.setter
    def snmp_community(self, value):
        self.snmp_community_enc = encrypt(value)

    @property
    def snmpv3_auth_key(self):
        return decrypt(self.snmpv3_auth_key_enc)

    @snmpv3_auth_key.setter
    def snmpv3_auth_key(self, value):
        self.snmpv3_auth_key_enc = encrypt(value)

    @property
    def snmpv3_priv_key(self):
        return decrypt(self.snmpv3_priv_key_enc)

    @snmpv3_priv_key.setter
    def snmpv3_priv_key(self, value):
        self.snmpv3_priv_key_enc = encrypt(value)

    @property
    def ssh_password(self):
        return decrypt(self.ssh_password_enc)

    @ssh_password.setter
    def ssh_password(self, value):
        self.ssh_password_enc = encrypt(value)


class Interface(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.Integer, db.ForeignKey("device.id"), nullable=False)
    if_index = db.Column(db.Integer, nullable=False)
    if_descr = db.Column(db.String(120))
    if_alias = db.Column(db.String(200))
    if_speed_bps = db.Column(db.BigInteger)  # from ifSpeed at walk time, reference only
    oper_status = db.Column(db.String(20), default="unknown")   # up | down | unknown
    admin_status = db.Column(db.String(20), default="unknown")  # up | down | unknown

    last_in_octets = db.Column(db.BigInteger, nullable=True)
    last_out_octets = db.Column(db.BigInteger, nullable=True)
    last_counter_at = db.Column(db.DateTime, nullable=True)

    last_in_bps = db.Column(db.Float, nullable=True)
    last_out_bps = db.Column(db.Float, nullable=True)
    last_polled_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (db.UniqueConstraint("device_id", "if_index"),)


class CircuitRole(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    tier = db.Column(db.String(20), default="critical")  # critical | auxiliary

    @staticmethod
    def default_role():
        """Fail-safe fallback: unmapped/new roles are critical until set otherwise."""
        role = CircuitRole.query.filter_by(name="core").first()
        if role is None:
            role = CircuitRole(name="core", tier="critical")
            db.session.add(role)
            db.session.flush()
        return role


class Circuit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    role_id = db.Column(db.Integer, db.ForeignKey("circuit_role.id"), nullable=False)
    parent_circuit_id = db.Column(db.Integer, db.ForeignKey("circuit.id"), nullable=True)

    # Leaf circuits reference two interfaces. Bundles leave these null and
    # derive status from child circuits instead.
    interface_a_id = db.Column(db.Integer, db.ForeignKey("interface.id"), nullable=True)
    interface_b_id = db.Column(db.Integer, db.ForeignKey("interface.id"), nullable=True)

    # Optional, bundles only: the port-channel/Bundle-Ether/ae logical
    # interface itself at each end, if the user has walked and wired one up.
    # is_bundle stays purely "interface_a/b are null" — these are additive,
    # not a replacement for the member-based rollup. See recompute_bundle_state.
    lag_interface_a_id = db.Column(db.Integer, db.ForeignKey("interface.id"), nullable=True)
    lag_interface_b_id = db.Column(db.Integer, db.ForeignKey("interface.id"), nullable=True)

    capacity_bps_override = db.Column(db.BigInteger, nullable=True)

    # Null = not monitored for utilization. Not acted on yet (no incident
    # gets raised when crossed) — settable now so it's ready for that once
    # built; see util_pct in routes/api.py for the same "current usage /
    # effective capacity" math this will eventually compare against.
    utilization_threshold_pct = db.Column(db.Integer, nullable=True)

    current_state = db.Column(db.String(20), default="up")
    consecutive_fail_count = db.Column(db.Integer, default=0)
    consecutive_success_count = db.Column(db.Integer, default=0)
    state_changed_at = db.Column(db.DateTime, default=datetime.utcnow)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    role = db.relationship("CircuitRole")
    interface_a = db.relationship("Interface", foreign_keys=[interface_a_id])
    interface_b = db.relationship("Interface", foreign_keys=[interface_b_id])
    lag_interface_a = db.relationship("Interface", foreign_keys=[lag_interface_a_id])
    lag_interface_b = db.relationship("Interface", foreign_keys=[lag_interface_b_id])
    children = db.relationship("Circuit", backref=db.backref("parent", remote_side=[id]))
    status_history = db.relationship("CircuitStatusHistory", backref="circuit",
                                      cascade="all, delete-orphan")
    alert_mute = db.relationship("AlertMute", backref="circuit",
                                  cascade="all, delete-orphan", uselist=False)
    waypoints = db.relationship("CircuitWaypoint", backref="circuit", order_by="CircuitWaypoint.position",
                                 cascade="all, delete-orphan")

    @property
    def is_bundle(self):
        return self.interface_a_id is None and self.interface_b_id is None

    @property
    def site_a(self):
        if self.is_bundle:
            # A bundle has no interfaces of its own — borrow the first
            # member's endpoint, same idea as is_intra_site's recursion.
            return next((c.site_a for c in self.children if c.site_a), None)
        return self.interface_a.device.site if self.interface_a else None

    @property
    def site_b(self):
        if self.is_bundle:
            return next((c.site_b for c in self.children if c.site_b), None)
        return self.interface_b.device.site if self.interface_b else None

    @property
    def is_intra_site(self):
        if self.is_bundle:
            # a bundle is intra-site only if ALL its members are
            return all(c.is_intra_site for c in self.children) if self.children else False
        return self.site_a_id_safe() == self.site_b_id_safe()

    def site_a_id_safe(self):
        site = self.site_a
        return site.id if site else None

    def site_b_id_safe(self):
        site = self.site_b
        return site.id if site else None

    @property
    def effective_capacity_bps(self):
        """A bundle's capacity is the sum of its members' — 2x100G members
        make a 200G bundle, even though each member circuit is still
        individually a 100G link. A leaf circuit's capacity is capped by
        its slower end, same idea as a cable only running as fast as its
        weakest link.

        If the bundle has its own LAG interface polled (lag_interface_a),
        prefer its reported speed over the member sum — the device's own
        ifHighSpeed on the port-channel already accounts for the LAG's
        *currently active* members, so it self-corrects when one drops,
        where summing static member speeds would keep overstating capacity
        until someone notices."""
        if self.capacity_bps_override:
            return self.capacity_bps_override
        if self.is_bundle:
            if self.lag_interface_a and self.lag_interface_a.if_speed_bps:
                return self.lag_interface_a.if_speed_bps
            return sum((c.effective_capacity_bps or 0) for c in self.children)
        speeds = [i.if_speed_bps for i in (self.interface_a, self.interface_b) if i and i.if_speed_bps]
        return min(speeds) if speeds else None

    @property
    def current_in_bps(self):
        """Current usage — prefers the bundle's own LAG interface counters
        when polled (see effective_capacity_bps for why), else sums
        members' (a LAG's aggregate throughput is its members' combined).
        A leaf's is read off interface_a, the same endpoint treated as
        canonical elsewhere on this model (site_a, etc.)."""
        if self.is_bundle:
            if self.lag_interface_a and self.lag_interface_a.last_in_bps is not None:
                return self.lag_interface_a.last_in_bps
            return sum((c.current_in_bps or 0) for c in self.children)
        return self.interface_a.last_in_bps if self.interface_a else None

    @property
    def _endpoint_a_label(self):
        """What to call interface_a's end when labeling a direction of
        flow — the site name normally, but a leaf circuit's two interfaces
        can share a site (intra-site, device-to-device within one
        building), where "Site X" on both ends of an arrow says nothing.
        Falls back to the device hostname there instead."""
        if not self.is_bundle and self.is_intra_site and self.interface_a:
            return self.interface_a.device.hostname
        return self.site_a.name if self.site_a else "?"

    @property
    def _endpoint_b_label(self):
        if not self.is_bundle and self.is_intra_site and self.interface_b:
            return self.interface_b.device.hostname
        return self.site_b.name if self.site_b else "?"

    @property
    def flow_out_label(self):
        """Human label for current_out_bps's direction: traffic leaving
        interface_a's end toward interface_b's end. Circuit creation picks
        which interface is "a" arbitrarily (whichever the form's Interface
        A field got), so without a label, "Out" alone doesn't say which of
        the two sites it's leaving — a circuit list mixing several circuits
        each with a different arbitrary "a" end would show numbers with no
        consistent site-to-site meaning."""
        return f"{self._endpoint_a_label} → {self._endpoint_b_label}"

    @property
    def flow_in_label(self):
        """Human label for current_in_bps's direction — the reverse of
        flow_out_label."""
        return f"{self._endpoint_b_label} → {self._endpoint_a_label}"

    @property
    def current_out_bps(self):
        if self.is_bundle:
            if self.lag_interface_a and self.lag_interface_a.last_out_bps is not None:
                return self.lag_interface_a.last_out_bps
            return sum((c.current_out_bps or 0) for c in self.children)
        return self.interface_a.last_out_bps if self.interface_a else None

    @property
    def effective_waypoints(self):
        """Only the root circuit's own line is ever drawn on the map (see
        api.py), so a bundle's waypoints have to live somewhere reachable
        from the bundle itself. If the bundle has none set directly, fall
        back to the first member that has some — bundle members are
        parallel links between the same two sites, so they typically share
        one physical route anyway."""
        if self.waypoints:
            return self.waypoints
        if self.is_bundle:
            for child in self.children:
                if child.waypoints:
                    return child.waypoints
        return []


class CircuitWaypoint(db.Model):
    """Purely cosmetic — an ordered point the map draws the circuit's line
    through between its two real endpoints (site_a/site_b, still derived
    from interface_a/interface_b same as always). Doesn't feed status,
    capacity, or anything else; a waypoint site doesn't need to be a
    passthrough site, just usually is one, since that's the case this
    exists for (a fiber route physically passing through a hut/manhole
    with no monitored equipment)."""
    id = db.Column(db.Integer, primary_key=True)
    circuit_id = db.Column(db.Integer, db.ForeignKey("circuit.id"), nullable=False)
    site_id = db.Column(db.Integer, db.ForeignKey("site.id"), nullable=False)
    position = db.Column(db.Integer, nullable=False)

    site = db.relationship("Site")

    __table_args__ = (db.UniqueConstraint("circuit_id", "position"),)


class CircuitStatusHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    circuit_id = db.Column(db.Integer, db.ForeignKey("circuit.id"), nullable=False)
    state = db.Column(db.String(20), nullable=False)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    cleared_at = db.Column(db.DateTime, nullable=True)

    # incident_number: assigned automatically the moment this row is
    # created (see poller.py's _transition) — SiteWatch's own identifier
    # for the down event, independent of whatever the NOC's external
    # system assigns. external_ticket: set later by a human once the NOC
    # opens (or already has) a ticket for it — never auto-populated.
    incident_number = db.Column(db.String(20), nullable=True)
    external_ticket = db.Column(db.String(80), nullable=True)


class UtilizationRollup(db.Model):
    """Hourly only — a "daily" row is never written; the 7-day history view
    aggregates hourly rows on the fly at display time instead of maintaining
    a second accumulation path. period_type stays on the model as forward
    room, not because anything produces "daily" rows today.

    Updated incrementally, once per poll cycle, by
    poller.py's _update_utilization_rollup — not a separate scheduled job.
    There's no raw per-poll sample history anywhere (Interface only ever
    holds its LATEST last_in_bps/last_out_bps), so an hour's avg/peak has
    to be built up sample-by-sample as polls land, not computed after the
    fact from stored samples. sample_count is what makes avg_*_bps a real
    running mean (avg = avg + (sample - avg) / count) instead of a rough
    approximation."""
    id = db.Column(db.Integer, primary_key=True)
    interface_id = db.Column(db.Integer, db.ForeignKey("interface.id"), nullable=False)
    period_type = db.Column(db.String(10), nullable=False)  # hourly | daily
    period_start = db.Column(db.DateTime, nullable=False)
    avg_in_bps = db.Column(db.Float)
    avg_out_bps = db.Column(db.Float)
    peak_in_bps = db.Column(db.Float)
    peak_out_bps = db.Column(db.Float)
    sample_count = db.Column(db.Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (db.UniqueConstraint("interface_id", "period_type", "period_start"),)


class AlertMute(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    circuit_id = db.Column(db.Integer, db.ForeignKey("circuit.id"), nullable=False, unique=True)
    muted_until = db.Column(db.DateTime, nullable=False)

    @staticmethod
    def is_muted(circuit_id):
        row = AlertMute.query.filter_by(circuit_id=circuit_id).first()
        if row is None:
            return False
        if row.muted_until < datetime.utcnow():
            db.session.delete(row)
            db.session.commit()
            return False
        return True


class MapRegion(db.Model):
    """A saved dashboard map view — center point + zoom level, named. Purely
    a UI convenience (the "All" option always re-fits to whatever sites
    currently exist instead of reading a saved region), created from the
    Manage Regions page by positioning the map how you want it and saving
    that view."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    center_lat = db.Column(db.Float, nullable=False)
    center_lon = db.Column(db.Float, nullable=False)
    zoom = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AuditLog(db.Model):
    """Durable, queryable record of human-initiated create/update/delete
    (and mute/unmute/import) actions on user-managed objects. Written
    explicitly by sitewatch/audit_log.py's record(), called from routes
    right alongside their own db.session.commit() — deliberately not an
    automatic ORM/session hook, so every entry can carry a hand-crafted,
    readable label/diff instead of a generic column dump, and so poller-
    driven writes (which have no current_user at all) are never at risk
    of getting swept in by accident. See routes/*.py for call sites.

    Never touched by backup.py — joins User/CircuitStatusHistory/AlertMute/
    UtilizationRollup in being permanently excluded from every export/
    import scope (see that module's docstring); an audit trail that could
    be silently overwritten by importing an old backup would defeat the
    point of having one. Pruned by a daily job per Setting
    "audit_log_retention_days" — see audit_log.prune_old_entries()."""
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Nullable so deleting a User (settings.py's delete_user()) never blocks
    # or cascades onto their own past audit rows. username is a point-in-
    # time snapshot so those rows stay legible after that user is gone —
    # the acting user's own history must survive their own deletion.
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    username = db.Column(db.String(80), nullable=False)
    user = db.relationship("User")

    action = db.Column(db.String(20), nullable=False, index=True)  # create | update | delete | import | mute | unmute
    # Matches the model class name (or "Backup" for a scope-level import) —
    # not a DB-enforced FK, since object_id may point at a row from any
    # one of several tables depending on this column.
    object_type = db.Column(db.String(40), nullable=False, index=True)
    object_id = db.Column(db.Integer, nullable=True)  # null for a bulk import or a Setting batch save — no single row
    label = db.Column(db.String(200), nullable=False)  # name/hostname/username snapshot, so a delete/rename still reads

    details = db.Column(db.Text, nullable=True)  # JSON — diff or snapshot; never credential values, see audit_log.py

    __table_args__ = (
        db.Index("ix_audit_log_object", "object_type", "object_id"),
    )
