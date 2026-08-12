from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required

from sitewatch.extensions import db
from sitewatch.models import Site, Circuit, Region, SITE_TYPES
from sitewatch.status import compute_site_status, site_degree_breakdown

sites_bp = Blueprint("sites", __name__, url_prefix="/sites")


@sites_bp.route("/")
@login_required
def list_sites():
    region_id = request.args.get("region_id", type=int)
    query = Site.query
    if region_id:
        query = query.filter_by(region_id=region_id)
    sites = query.all()
    statuses = {s.id: compute_site_status(s) for s in sites}
    return render_template("sites.html", sites=sites, statuses=statuses,
                            regions=Region.query.order_by(Region.name).all(), selected_region_id=region_id)


@sites_bp.route("/add", methods=["GET", "POST"])
@login_required
def add_site():
    if request.method == "POST":
        site_type = request.form.get("site_type", "site")
        if site_type not in SITE_TYPES:
            site_type = "site"
        site = Site(
            name=request.form["name"],
            lat=float(request.form["lat"]),
            lon=float(request.form["lon"]),
            site_type=site_type,
            region_id=request.form.get("region_id", type=int) or None,
            source="manual",
        )
        db.session.add(site)
        db.session.commit()
        return redirect(url_for("sites.list_sites"))
    return render_template("site_form.html", site=None, regions=Region.query.order_by(Region.name).all())


@sites_bp.route("/<int:site_id>/edit", methods=["GET", "POST"])
@login_required
def edit_site(site_id):
    site = Site.query.get_or_404(site_id)
    if request.method == "POST":
        site_type = request.form.get("site_type", "site")
        if site_type not in SITE_TYPES:
            site_type = "site"
        if site_type == "passthrough" and site.devices:
            flash("Has devices assigned — remove or reassign them first.")
            return redirect(url_for("sites.edit_site", site_id=site_id))
        site.name = request.form["name"]
        site.lat = float(request.form["lat"])
        site.lon = float(request.form["lon"])
        site.site_type = site_type
        site.region_id = request.form.get("region_id", type=int) or None
        db.session.commit()
        return redirect(url_for("sites.site_detail", site_id=site.id))
    return render_template("site_form.html", site=site, regions=Region.query.order_by(Region.name).all())


@sites_bp.route("/<int:site_id>/delete", methods=["POST"])
@login_required
def delete_site(site_id):
    site = Site.query.get_or_404(site_id)
    if site.devices:
        flash("Has devices assigned — remove or reassign them first.")
        return redirect(url_for("sites.site_detail", site_id=site_id))
    db.session.delete(site)
    db.session.commit()
    return redirect(url_for("sites.list_sites"))


@sites_bp.route("/<int:site_id>")
@login_required
def site_detail(site_id):
    site = Site.query.get_or_404(site_id)
    status = compute_site_status(site)

    root_circuits = [
        c for c in Circuit.query.filter_by(parent_circuit_id=None).all()
        if c.site_a_id_safe() == site_id or c.site_b_id_safe() == site_id
    ]
    intra_site_circuits = [c for c in root_circuits if c.is_intra_site]
    return render_template(
        "site_detail.html", site=site, status=status,
        devices=site.devices, intra_site_circuits=intra_site_circuits,
        degree_breakdown=site_degree_breakdown(site),
    )
