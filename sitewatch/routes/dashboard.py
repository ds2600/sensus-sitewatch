from flask import Blueprint, render_template, request
from flask_login import login_required
from sitewatch.models import Circuit, CircuitStatusHistory, MapRegion, Layer

dashboard_bp = Blueprint("dashboard", __name__)

CLEARED_COLLAPSED_COOKIE = "sitewatch_cleared_collapsed"


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
    map_views = MapRegion.query.order_by(MapRegion.name).all()
    layers = Layer.query.order_by(Layer.name).all()
    # Read server-side so the very first render already matches — a
    # JS-only toggle would flash the list open for a moment on every load
    # before catching up to what the cookie says. dashboard.html's inline
    # script (bottom of the file) is what writes this cookie back.
    cleared_collapsed = request.cookies.get(CLEARED_COLLAPSED_COOKIE) == "1"
    return render_template("dashboard.html", down_now=down_now, recently_cleared=recently_cleared,
                            map_views=map_views, layers=layers, cleared_collapsed=cleared_collapsed,
                            selected_layer_id=request.args.get("layer_id", type=int))
