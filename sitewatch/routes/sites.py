from flask import Blueprint, render_template, request, redirect, url_for, flash, Response
from flask_login import login_required

from sitewatch.auth import admin_required
from sitewatch.extensions import db
from sitewatch.models import Site, Circuit, Region, SITE_TYPES
from sitewatch.status import compute_site_status, site_degree_breakdown
from sitewatch.csv_import import parse_csv, CsvImportError
from sitewatch import audit_log

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


def _resolve_minor_parent(site_type):
    """A minor site's parent_site_id must be present and point at a plain
    Major Site (site_type == 'site') — never another minor site or a
    passthrough. This single check is what keeps minor-site nesting to one
    level, which status.py's compute_site_status() recursion relies on to
    never go more than one hop deep. Returns (parent_id_or_None, error).
    Non-minor site types always resolve to (None, None), dropping any
    stale parent_site_id if a site is edited away from "minor"."""
    if site_type != "minor":
        return None, None
    parent_id = request.form.get("parent_site_id", type=int)
    parent = Site.query.get(parent_id) if parent_id else None
    if parent is None:
        return None, "Minor sites need a parent Major Site."
    if parent.site_type != "site":
        return None, "A minor site's parent must be a Major Site, not a minor site or passthrough."
    return parent_id, None


@sites_bp.route("/add", methods=["GET", "POST"])
@admin_required
def add_site():
    if request.method == "POST":
        site_type = request.form.get("site_type", "site")
        if site_type not in SITE_TYPES:
            site_type = "site"
        parent_site_id, error = _resolve_minor_parent(site_type)
        if error:
            flash(error)
            return redirect(url_for("sites.add_site"))
        site = Site(
            name=request.form["name"],
            lat=float(request.form["lat"]),
            lon=float(request.form["lon"]),
            site_type=site_type,
            parent_site_id=parent_site_id,
            region_id=request.form.get("region_id", type=int) or None,
            source="manual",
        )
        db.session.add(site)
        db.session.flush()
        audit_log.record("create", "Site", site.id, site.name, {
            "lat": site.lat, "lon": site.lon, "site_type": site.site_type,
            "parent_site_id": site.parent_site_id, "region_id": site.region_id,
        })
        db.session.commit()
        return redirect(url_for("sites.list_sites"))
    return render_template("site_form.html", site=None, regions=Region.query.order_by(Region.name).all(),
                            major_sites=Site.query.filter_by(site_type="site").order_by(Site.name).all())


@sites_bp.route("/import")
@admin_required
def import_sites():
    # "minor" excluded — see import_sites_preview's comment.
    return render_template("site_import.html", site_types=("site", "passthrough"))


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

        # "minor" excluded here — a minor site needs a parent Major Site,
        # which a flat CSV row has no way to express yet (see backlog).
        site_type = (row["type"] or "site").strip().lower()
        if site_type not in ("site", "passthrough"):
            errors.append("Type must be one of: site, passthrough (blank defaults to site).")

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
    created = []
    for name, lat, lon, site_type in zip(names, lats, lons, types):
        resolved_type = site_type if site_type in ("site", "passthrough") else "site"
        db.session.add(Site(
            name=name, lat=float(lat), lon=float(lon),
            site_type=resolved_type,
            source="manual",
        ))
        created.append({"name": name, "site_type": resolved_type})
    audit_log.record("import", "Site", None, f"CSV import: {len(names)} site(s)",
                      {"count": len(names), "created": created})
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
        parent_site_id, error = _resolve_minor_parent(site_type)
        if error:
            flash(error)
            return redirect(url_for("sites.edit_site", site_id=site_id))
        before = {"name": site.name, "lat": site.lat, "lon": site.lon, "site_type": site.site_type,
                  "parent_site_id": site.parent_site_id, "region_id": site.region_id}
        site.name = request.form["name"]
        site.lat = float(request.form["lat"])
        site.lon = float(request.form["lon"])
        site.site_type = site_type
        site.parent_site_id = parent_site_id
        site.region_id = request.form.get("region_id", type=int) or None
        after = {"name": site.name, "lat": site.lat, "lon": site.lon, "site_type": site.site_type,
                 "parent_site_id": site.parent_site_id, "region_id": site.region_id}
        diff = audit_log.diff_fields(before, after)
        if diff:
            audit_log.record("update", "Site", site.id, site.name, diff)
        db.session.commit()
        return redirect(url_for("sites.site_detail", site_id=site.id))
    return render_template("site_form.html", site=site, regions=Region.query.order_by(Region.name).all(),
                            major_sites=Site.query.filter(Site.site_type == "site", Site.id != site.id)
                            .order_by(Site.name).all())


@sites_bp.route("/<int:site_id>/delete", methods=["POST"])
@admin_required
def delete_site(site_id):
    site = Site.query.get_or_404(site_id)
    if site.devices:
        flash("Has devices assigned — remove or reassign them first.")
        return redirect(url_for("sites.site_detail", site_id=site_id))
    if site.minor_sites:
        flash("Has minor sites assigned — remove or reassign them first.")
        return redirect(url_for("sites.site_detail", site_id=site_id))
    name = site.name
    db.session.delete(site)
    audit_log.record("delete", "Site", site_id, name)
    db.session.commit()
    return redirect(url_for("sites.list_sites"))


def _passthrough_transit(site):
    """Every root circuit whose drawn path — site_a -> effective_waypoints
    -> site_b, the same path map.js renders — passes through this site as
    one of its cosmetic waypoints (never as the circuit's own A/Z end, even
    when that end happens to equal this site — see the range() below),
    with the immediate neighbor point on each side (which may be the
    circuit's own A/Z end, if this site is the first or last waypoint)
    plus the circuit's real A/Z ends regardless. Usually called for a
    passthrough site (a waypoint is usually one, and never anyone's real
    end since a passthrough site has no devices/interfaces to be one), but
    works for any site: a major/minor site can still be picked as some
    OTHER circuit's waypoint — see circuit_form.html's waypoint picker,
    which lists every site. A circuit can in principle route through the
    same site twice; each occurrence is its own row rather than only
    reporting the first."""
    results = []
    for c in Circuit.query.filter_by(parent_circuit_id=None).all():
        path = [c.site_a] + [w.site for w in c.effective_waypoints] + [c.site_b]
        # 1..len-2 only — path[0]/path[-1] are the circuit's own real A/Z
        # ends, not waypoints, even on a major/minor site that happens to
        # BE that circuit's own end (already covered by Intra-site/
        # External circuits on that site's own page; listing it again here
        # too would be a bogus "passes through itself" duplicate row).
        for i in range(1, len(path) - 1):
            point = path[i]
            if point and point.id == site.id:
                results.append({
                    "circuit": c,
                    "prev_site": path[i - 1],
                    "next_site": path[i + 1],
                    "site_a": c.site_a,
                    "site_b": c.site_b,
                })
    return results


@sites_bp.route("/<int:site_id>")
@login_required
def site_detail(site_id):
    site = Site.query.get_or_404(site_id)
    status = compute_site_status(site)
    # Only fetched to let the template explain *why* a minor site is red
    # when its own circuits wouldn't otherwise say so (see status.py's
    # parent-cascade rule) — not used for anything else here.
    parent_status = compute_site_status(site.parent_site) if site.parent_site else None
    minor_site_statuses = {m.id: compute_site_status(m) for m in site.minor_sites}

    root_circuits = [
        c for c in Circuit.query.filter_by(parent_circuit_id=None).all()
        if c.site_a_id_safe() == site_id or c.site_b_id_safe() == site_id
    ]
    intra_site_circuits = [c for c in root_circuits if c.is_intra_site]
    passthrough_transit = _passthrough_transit(site)
    return render_template(
        "site_detail.html", site=site, status=status, parent_status=parent_status,
        minor_site_statuses=minor_site_statuses,
        devices=site.devices, intra_site_circuits=intra_site_circuits,
        degree_breakdown=site_degree_breakdown(site),
        passthrough_transit=passthrough_transit,
    )
