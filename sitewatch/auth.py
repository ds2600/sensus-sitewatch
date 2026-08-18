import secrets
from functools import wraps

from flask import Blueprint, render_template, redirect, url_for, request, flash, abort, g
from flask_login import login_user, logout_user, login_required, current_user

from sitewatch.extensions import login_manager
from sitewatch.models import User, Probe

auth_bp = Blueprint("auth", __name__)


def admin_required(fn):
    """Drop-in replacement for @login_required on routes that mutate data —
    the read_only role can view virtually everything but can't create,
    edit, delete, walk/repoll, import, or reach Settings at all (the one
    write exception, assigning an incident's external ticket number,
    stays on plain @login_required — see circuits.py's set_incident_ticket).
    Redirects to login the same way @login_required would for a logged-out
    request; only aborts 403 once we know the user is authenticated but
    not an admin."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if not current_user.is_admin:
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


def probe_required(fn):
    """Machine auth for routes/probe_api.py — a standalone probe (sitewatch/
    probe.py) is a script on a remote box, not a browser session, so this
    is deliberately separate from login_required/admin_required: it checks
    an `Authorization: Bearer <key>` header against Probe.api_key instead
    of a Flask-Login session, and sets g.probe on success rather than
    relying on current_user. api_key_enc is Fernet-encrypted (non-
    deterministic ciphertext), so a presented key can't be looked up by an
    indexed equality query — decrypt-and-compare against every Probe row
    instead. Fine at this app's scale (a handful of probes, not
    thousands); revisit with a separate indexed key-hash column if that
    ever stops being true."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            abort(401)
        presented = auth_header[len("Bearer "):]
        for probe in Probe.query.all():
            if probe.api_key and secrets.compare_digest(probe.api_key, presented):
                g.probe = probe
                return fn(*args, **kwargs)
        abort(401)
    return wrapper


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = User.query.filter_by(username=request.form["username"]).first()
        if user and user.check_password(request.form["password"]):
            login_user(user)
            return redirect(url_for("dashboard.index"))
        flash("Invalid username or password.")
    return render_template("login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
