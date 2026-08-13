from flask import Blueprint, render_template, request, redirect, url_for, flash, Response
from flask_login import login_required

from sitewatch.auth import admin_required
from sitewatch.extensions import db
from sitewatch.models import Site, Circuit, Region, SITE_TYPES
from sitewatch.status import compute_site_status, site_degree_breakdown
from sitewatch.csv_import import parse_csv, CsvImportError

sites_bp = Blueprint("sites", __name__, url_prefix="/sites")

_SITE_CSV_HEADER_ALIASES = {
    "name": ["site", "name", "site name"],
    "lat": ["lat", "latitude"],
    "lon": ["long", "lon", "longitude"],
    "type": ["type", "site type"],
}


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
@admin_required
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


@sites_bp.route("/import")
@admin_required
def import_sites():
    return render_template("site_import.html", site_types=SITE_TYPES)


@sites_bp.route("/import/template")
@admin_required
def import_sites_template():
    csv_text = (
        "Site,Lat,Long,Type\n"
        "Example HQ,39.7392,-104.9903,site\n"
        "Example Hub,41.8781,-87.6298,passthrough\n"
    )
    return Response(csv_text, mimetype="text/csv",
                     headers={"Content-Disposition": "attachment; filename=sitewatch-sites-template.csv"})


@sites_bp.route("/import/preview", methods=["POST"])
@admin_required
def import_sites_preview():
    file = request.files.get("csv_file")
    if not file or file.filename == "":
        flash("Choose a CSV file.")
        return redirect(url_for("sites.import_sites"))
    try:
        rows = parse_csv(file, _SITE_CSV_HEADER_ALIASES)
    except CsvImportError as e:
        flash(str(e))
        return redirect(url_for("sites.import_sites"))

    existing_names = {s.name.strip().lower() for s in Site.query.all()}
    seen_names = set()
    preview_rows = []
    for i, row in enumerate(rows, start=1):
        errors = []

        name = row["name"]
        if not name:
            errors.append("Name is required.")

        lat_raw = row["lat"]
        try:
            lat = float(lat_raw)
            if not (-90 <= lat <= 90):
                errors.append("Lat must be between -90 and 90.")
        except ValueError:
            errors.append("Lat must be a number.")

        lon_raw = row["lon"]
        try:
            lon = float(lon_raw)
            if not (-180 <= lon <= 180):
                errors.append("Long must be between -180 and 180.")
        except ValueError:
            errors.append("Long must be a number.")

        site_type = (row["type"] or "site").strip().lower()
        if site_type not in SITE_TYPES:
            errors.append(f"Type must be one of: {', '.join(SITE_TYPES)} (blank defaults to site).")

        warning = None
        key = name.strip().lower()
        if not errors:
            if key in existing_names:
                warning = f"A site named '{name}' already exists — this adds a duplicate."
            elif key in seen_names:
                warning = f"'{name}' is duplicated earlier in this file — this adds a duplicate."
        if key:
            seen_names.add(key)

        preview_rows.append({
            "row_num": i, "name": name, "lat": lat_raw, "lon": lon_raw, "type": site_type,
            "errors": errors, "warning": warning,
        })

    valid_count = sum(1 for r in preview_rows if not r["errors"])
    return render_template("site_import_preview.html", rows=preview_rows,
                            valid_count=valid_count, total_count=len(preview_rows))


@sites_bp.route("/import/confirm", methods=["POST"])
@admin_required
def import_sites_confirm():
    names = request.form.getlist("row_name")
    lats = request.form.getlist("row_lat")
    lons = request.form.getlist("row_lon")
    types = request.form.getlist("row_type")
    if not names:
        flash("Nothing to import.")
        return redirect(url_for("sites.import_sites"))
    for name, lat, lon, site_type in zip(names, lats, lons, types):
        db.session.add(Site(
            name=name, lat=float(lat), lon=float(lon),
            site_type=site_type if site_type in SITE_TYPES else "site",
            source="manual",
        ))
    db.session.commit()
    flash(f"Imported {len(names)} site(s).")
    return redirect(url_for("sites.list_sites"))


@sites_bp.route("/<int:site_id>/edit", methods=["GET", "POST"])
@admin_required
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
@admin_required
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
