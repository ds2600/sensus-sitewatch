import os
from flask import Flask
from dotenv import load_dotenv

from sitewatch.extensions import db, login_manager, scheduler

load_dotenv()


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

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(sites_bp)
    app.register_blueprint(devices_bp)
    app.register_blueprint(circuits_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(api_bp)

    @app.context_processor
    def inject_app_name():
        return {"app_name": app.config["APP_NAME"]}

    # Poller starts only under the real server, not under `flask init-db` etc.
    if os.environ.get("SITEWATCH_RUN_POLLER") == "1":
        from sitewatch.poller import start_poller
        start_poller(app)

    return app
