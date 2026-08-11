from flask import Blueprint, render_template
from flask_login import login_required
from sitewatch.models import CircuitStatusHistory

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
@login_required
def index():
    down_now = (CircuitStatusHistory.query.filter_by(cleared_at=None)
                .order_by(CircuitStatusHistory.started_at.desc()).all())
    recently_cleared = (CircuitStatusHistory.query.filter(CircuitStatusHistory.cleared_at.isnot(None))
                         .order_by(CircuitStatusHistory.cleared_at.desc()).limit(20).all())
    return render_template("dashboard.html", down_now=down_now, recently_cleared=recently_cleared)
