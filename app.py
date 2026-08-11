"""Entry point. Run with: flask --app app run --host=0.0.0.0 --port=5000"""
import os
import click
from flask.cli import with_appcontext
from sitewatch import create_app
from sitewatch.extensions import db
from sitewatch.models import User, Setting

app = create_app()


@app.cli.command("init-db")
@with_appcontext
def init_db():
    """Create schema and seed the admin user + default settings."""
    db.create_all()

    admin_user = os.environ.get("ADMIN_USERNAME", "admin")
    admin_pass = os.environ.get("ADMIN_PASSWORD")
    if not admin_pass:
        click.echo("ADMIN_PASSWORD not set in .env — aborting.")
        return

    if not User.query.filter_by(username=admin_user).first():
        u = User(username=admin_user)
        u.set_password(admin_pass)
        db.session.add(u)

    Setting.seed_defaults()
    db.session.commit()
    click.echo(f"Database initialized. Admin user: {admin_user}")


@app.cli.command("seed-demo")
@with_appcontext
def seed_demo_command():
    """Populate the DB with a small simulated topology (no real devices
    needed). Run the server with SITEWATCH_SIMULATE=1 afterward so the
    poller reads back simulated telemetry instead of real SNMP."""
    from sitewatch.seed_demo import run
    run()
    click.echo("Demo data seeded. Start the server with SITEWATCH_SIMULATE=1 to see it poll.")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
