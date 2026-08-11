from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required

from sitewatch.extensions import db
from sitewatch.models import Site, Circuit
from sitewatch.status import compute_site_status

sites_bp = Blueprint("sites", __name__, url_prefix="/sites")


@sites_bp.route("/")
@login_required
def list_sites():
    sites = Site.query.all()
    statuses = {s.id: compute_site_status(s) for s in sites}
    return render_template("sites.html", sites=sites, statuses=statuses)


@sites_bp.route("/add", methods=["GET", "POST"])
@login_required
def add_site():
    if request.method == "POST":
        site = Site(
            name=request.form["name"],
            lat=float(request.form["lat"]),
            lon=float(request.form["lon"]),
            source="manual",
        )
        db.session.add(site)
        db.session.commit()
        return redirect(url_for("sites.list_sites"))
    return render_template("site_form.html", site=None)


@sites_bp.route("/<int:site_id>/edit", methods=["GET", "POST"])
@login_required
def edit_site(site_id):
    site = Site.query.get_or_404(site_id)
    if request.method == "POST":
        site.name = request.form["name"]
        site.lat = float(request.form["lat"])
        site.lon = float(request.form["lon"])
        db.session.commit()
        return redirect(url_for("sites.site_detail", site_id=site.id))
    return render_template("site_form.html", site=site)


@sites_bp.route("/<int:site_id>/delete", methods=["POST"])
@login_required
def delete_site(site_id):
    site = Site.query.get_or_404(site_id)
    if site.devices:
        flash("Can't delete a site that still has devices assigned. Remove or reassign them first.")
        return redirect(url_for("sites.site_detail", site_id=site_id))
    db.session.delete(site)
    db.session.commit()
    return redirect(url_for("sites.list_sites"))


@sites_bp.route("/<int:site_id>")
@login_required
def site_detail(site_id):
    site = Site.query.get_or_404(site_id)
    status = compute_site_status(site)

    intra_site_circuits = [
        c for c in Circuit.query.filter_by(parent_circuit_id=None).all()
        if not c.is_bundle and c.is_intra_site and c.site_a_id_safe() == site_id
    ]
    external_circuits = [
        c for c in Circuit.query.filter_by(parent_circuit_id=None).all()
        if not c.is_intra_site and (c.site_a_id_safe() == site_id or c.site_b_id_safe() == site_id)
        and _touches(c, site_id)
    ]
    return render_template(
        "site_detail.html", site=site, status=status,
        devices=site.devices, intra_site_circuits=intra_site_circuits,
        external_circuits=external_circuits,
    )


def _touches(circuit, site_id):
    if circuit.is_bundle:
        return any(_touches(c, site_id) for c in circuit.children)
    return circuit.site_a_id_safe() == site_id or circuit.site_b_id_safe() == site_id
