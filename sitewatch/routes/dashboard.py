from flask import Blueprint, render_template
from flask_login import login_required
from sitewatch.models import Circuit, CircuitStatusHistory, MapRegion

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
@login_required
def index():
    # Inner join drops any history row whose circuit no longer exists —
    # belt-and-suspenders alongside the cascade delete on Circuit, in case
    # rows were orphaned before that cascade was added.
    down_now = (CircuitStatusHistory.query.join(Circuit)
                .filter(CircuitStatusHistory.cleared_at.is_(None))
                .order_by(CircuitStatusHistory.started_at.desc()).all())
    recently_cleared = (CircuitStatusHistory.query.join(Circuit)
                         .filter(CircuitStatusHistory.cleared_at.isnot(None))
                         .order_by(CircuitStatusHistory.cleared_at.desc()).limit(20).all())
    regions = MapRegion.query.order_by(MapRegion.name).all()
    return render_template("dashboard.html", down_now=down_now, recently_cleared=recently_cleared, regions=regions)
