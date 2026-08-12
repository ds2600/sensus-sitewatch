import os
from flask import Flask
from dotenv import load_dotenv

from sitewatch.extensions import db, login_manager, scheduler

load_dotenv()

# SemVer. Bump on every user-facing release; GitHub Releases tags match
# this (vX.Y.Z). Surfaced in the UI footer (see inject_app_name below) so
# anyone looking at a running instance can tell what's actually deployed.
__version__ = "0.1.1"


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ["SECRET_KEY"]
    db_path = os.environ.get("DATABASE_PATH", "instance/sitewatch.db")
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.abspath(db_path)}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["APP_NAME"] = os.environ.get("APP_NAME", "Sensus SiteWatch")

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    from sitewatch import job_log
    job_log.install()

    from sitewatch.models import User

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from sitewatch.auth import auth_bp
    from sitewatch.routes.dashboard import dashboard_bp
    from sitewatch.routes.sites import sites_bp
    from sitewatch.routes.devices import devices_bp
    from sitewatch.routes.circuits import circuits_bp
    from sitewatch.routes.settings import settings_bp
    from sitewatch.routes.api import api_bp
    from sitewatch.routes.regions import regions_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(sites_bp)
    app.register_blueprint(devices_bp)
    app.register_blueprint(circuits_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(regions_bp)

    @app.context_processor
    def inject_app_name():
        return {"app_name": app.config["APP_NAME"], "app_version": __version__}

    @app.template_filter("bps")
    def format_bps(value):
        """Used on the circuits pages for capacity/current-usage display —
        a 100G circuit reading '100000000000 bps' isn't useful to anyone."""
        if value is None:
            return "-"
        value = float(value)
        for unit, divisor in (("Tbps", 1e12), ("Gbps", 1e9), ("Mbps", 1e6), ("Kbps", 1e3)):
            if value >= divisor:
                return f"{value / divisor:.1f} {unit}"
        return f"{value:.0f} bps"

    # Self-healing schema: db.create_all() (what `flask init-db` runs) only
    # creates missing TABLES, it silently does nothing for a table that
    # already exists even if the model gained new columns since — so a
    # routine additive schema change needs this to actually land without
    # the user having to hand-run ALTER TABLE or wipe/reimport the DB. Must
    # run after every model class above has actually been imported (blueprint
    # imports above pull in models.py) — db.metadata is empty, and this a
    # silent no-op, otherwise. Runs on every app start; cheap and a no-op
    # once the schema's already caught up.
    from sitewatch.schema_sync import sync_schema
    with app.app_context():
        sync_schema(db)

    # Poller starts only under the real server, not under `flask init-db` etc.
    if os.environ.get("SITEWATCH_RUN_POLLER") == "1":
        from sitewatch.poller import start_poller
        start_poller(app)

    return app
