from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required

from sitewatch.auth import admin_required
from sitewatch.extensions import db
from sitewatch.models import MapRegion
from sitewatch import audit_log

regions_bp = Blueprint("regions", __name__, url_prefix="/regions")


@regions_bp.route("/")
@login_required
def manage_regions():
    return render_template("regions.html", regions=MapRegion.query.order_by(MapRegion.name).all())


@regions_bp.route("/add", methods=["POST"])
@admin_required
def add_region():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Name the view before saving it.")
        return redirect(url_for("regions.manage_regions"))
    if MapRegion.query.filter_by(name=name).first():
        flash(f"A view named '{name}' already exists.")
        return redirect(url_for("regions.manage_regions"))
    region = MapRegion(
        name=name,
        center_lat=float(request.form["center_lat"]),
        center_lon=float(request.form["center_lon"]),
        zoom=int(request.form["zoom"]),
    )
    db.session.add(region)
    db.session.flush()
    audit_log.record("create", "MapRegion", region.id, region.name,
                      {"center_lat": region.center_lat, "center_lon": region.center_lon, "zoom": region.zoom})
    db.session.commit()
    flash(f"View '{name}' saved.")
    return redirect(url_for("regions.manage_regions"))


@regions_bp.route("/<int:region_id>/delete", methods=["POST"])
@admin_required
def delete_region(region_id):
    region = MapRegion.query.get_or_404(region_id)
    name = region.name
    db.session.delete(region)
    audit_log.record("delete", "MapRegion", region_id, name)
    db.session.commit()
    return redirect(url_for("regions.manage_regions"))
