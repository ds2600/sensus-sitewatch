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


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Setting(db.Model):
    """Single-row key/value settings table. Access via Setting.get/set."""
    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.String(255))

    DEFAULTS = {
        "polling_interval_minutes": "2",
        "down_threshold_count": "3",
        "mute_max_minutes": "60",
        "google_chat_webhook_url": "",
        "status_history_retention_days": "30",
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


class Site(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    lat = db.Column(db.Float, nullable=False)
    lon = db.Column(db.Float, nullable=False)
    netbox_id = db.Column(db.Integer, nullable=True)
    source = db.Column(db.String(20), default="manual")
    out_of_sync = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    devices = db.relationship("Device", backref="site", lazy=True)


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

    capacity_bps_override = db.Column(db.BigInteger, nullable=True)

    current_state = db.Column(db.String(20), default="up")
    consecutive_fail_count = db.Column(db.Integer, default=0)
    consecutive_success_count = db.Column(db.Integer, default=0)
    state_changed_at = db.Column(db.DateTime, default=datetime.utcnow)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    role = db.relationship("CircuitRole")
    interface_a = db.relationship("Interface", foreign_keys=[interface_a_id])
    interface_b = db.relationship("Interface", foreign_keys=[interface_b_id])
    children = db.relationship("Circuit", backref=db.backref("parent", remote_side=[id]))
    status_history = db.relationship("CircuitStatusHistory", backref="circuit",
                                      cascade="all, delete-orphan")
    alert_mute = db.relationship("AlertMute", backref="circuit",
                                  cascade="all, delete-orphan", uselist=False)

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
        return self.interface_a.device.site_id if self.interface_a else None

    def site_b_id_safe(self):
        return self.interface_b.device.site_id if self.interface_b else None

    @property
    def effective_capacity_bps(self):
        if self.capacity_bps_override:
            return self.capacity_bps_override
        if self.is_bundle:
            return sum((c.effective_capacity_bps or 0) for c in self.children)
        speeds = [i.if_speed_bps for i in (self.interface_a, self.interface_b) if i and i.if_speed_bps]
        return min(speeds) if speeds else None


class CircuitStatusHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    circuit_id = db.Column(db.Integer, db.ForeignKey("circuit.id"), nullable=False)
    state = db.Column(db.String(20), nullable=False)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    cleared_at = db.Column(db.DateTime, nullable=True)


class UtilizationRollup(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    interface_id = db.Column(db.Integer, db.ForeignKey("interface.id"), nullable=False)
    period_type = db.Column(db.String(10), nullable=False)  # hourly | daily
    period_start = db.Column(db.DateTime, nullable=False)
    avg_in_bps = db.Column(db.Float)
    avg_out_bps = db.Column(db.Float)
    peak_in_bps = db.Column(db.Float)
    peak_out_bps = db.Column(db.Float)

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
