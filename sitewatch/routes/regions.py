from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required

from sitewatch.extensions import db
from sitewatch.models import MapRegion

regions_bp = Blueprint("regions", __name__, url_prefix="/regions")


@regions_bp.route("/")
@login_required
def manage_regions():
    return render_template("regions.html", regions=MapRegion.query.order_by(MapRegion.name).all())


@regions_bp.route("/add", methods=["POST"])
@login_required
def add_region():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Name a region before saving it.")
        return redirect(url_for("regions.manage_regions"))
    if MapRegion.query.filter_by(name=name).first():
        flash(f"A region named '{name}' already exists.")
        return redirect(url_for("regions.manage_regions"))
    db.session.add(MapRegion(
        name=name,
        center_lat=float(request.form["center_lat"]),
        center_lon=float(request.form["center_lon"]),
        zoom=int(request.form["zoom"]),
    ))
    db.session.commit()
    flash(f"Region '{name}' saved.")
    return redirect(url_for("regions.manage_regions"))


@regions_bp.route("/<int:region_id>/delete", methods=["POST"])
@login_required
def delete_region(region_id):
    region = MapRegion.query.get_or_404(region_id)
    db.session.delete(region)
    db.session.commit()
    return redirect(url_for("regions.manage_regions"))
