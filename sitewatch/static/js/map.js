// Renders the site/circuit map on the dashboard. Colors follow status.py's
// green/yellow/red for sites, plus per-circuit state for lines.
const STATUS_COLOR = { green: "#198754", yellow: "#ffc107", red: "#dc3545", blue: "#0d6efd", passthrough: "#8a8fa3" };
const CIRCUIT_COLOR = { up: "#198754", degraded: "#ffc107", down: "#dc3545",
                         admin_down: "#6c757d", unreachable: "#0d6efd" };

// Quadratic bezier from p0 to p1, bulging toward a control point offset
// perpendicular to the p0->p1 line by `offsetFraction * segment length` at
// the midpoint. p0/p1 are untouched — only the middle of the curve moves —
// so multiple parallel circuits between the same two sites can fan out for
// visibility without their endpoints drifting off the site markers they
// connect to.
//
// The offset is a FRACTION of this segment's own length, not a fixed
// lat/lon amount — a fixed offset (the original approach here) is a fixed
// real-world distance regardless of how far apart the two points are, so
// it reads as a big, obvious bulge on a short local segment but is utterly
// imperceptible on a long haul segment at whatever zoom shows the whole
// route (both distances live in the same lat/lon space Leaflet renders
// directly, so a fixed offset really is a fixed number of pixels-at-a-given-
// zoom relative to the segment's own screen length). Scaling by the
// segment's own length keeps the visual separation proportionally the same
// everywhere, at every zoom — this is what fixed the "some overlapping
// circuits fan out, others on the same map don't" report.
function curvedSegment(p0, p1, offsetFraction, steps = 16) {
  const dLat = p1[0] - p0[0];
  const dLon = p1[1] - p0[1];
  const len = Math.sqrt(dLat * dLat + dLon * dLon) || 1;
  const magnitude = offsetFraction * len;
  const control = [
    (p0[0] + p1[0]) / 2 + (-dLon / len) * magnitude,
    (p0[1] + p1[1]) / 2 + (dLat / len) * magnitude,
  ];
  const points = [];
  for (let i = 0; i <= steps; i++) {
    const t = i / steps;
    const mt = 1 - t;
    points.push([
      mt * mt * p0[0] + 2 * mt * t * control[0] + t * t * p1[0],
      mt * mt * p0[1] + 2 * mt * t * control[1] + t * t * p1[1],
    ]);
  }
  return points;
}

// A circuit's full path (site_a -> waypoints -> site_b) as one polyline,
// but each leg gets its OWN offset fraction via offsetFractionForLeg(i)
// instead of one offset for the whole line — see the per-segment grouping
// below this exists for: two circuits with different real endpoints can
// still share a run of waypoints in the middle, and only THAT shared
// stretch should fan apart, not the rest of either circuit's route.
function curvedPathBySegment(points, offsetFractionForLeg) {
  const latLngs = points.map((p) => [p.lat, p.lon]);
  let path = [];
  for (let i = 0; i < latLngs.length - 1; i++) {
    const offsetFraction = offsetFractionForLeg(i);
    const seg = offsetFraction
      ? curvedSegment(latLngs[i], latLngs[i + 1], offsetFraction)
      : [latLngs[i], latLngs[i + 1]];
    path = path.concat(i === 0 ? seg : seg.slice(1)); // drop duplicate joint point
  }
  return path;
}

function pointKey(p) {
  return p.lat + "," + p.lon;
}

// Sorted-pair key identifying a segment regardless of which end is p0/p1 —
// same site pair, same key, whichever circuit's path traverses it in
// whichever direction.
function segmentKey(p0, p1) {
  return [pointKey(p0), pointKey(p1)].sort().join("|");
}

// +1 if this leg's own p0->p1 traversal already matches the segment's
// canonical (sorted) point order, -1 if it's reversed. Which end a circuit
// calls "site_a" vs "site_b" (or which way its waypoints are ordered) is
// arbitrary per circuit — two circuits sharing the exact same physical
// segment can easily store it in opposite directions. curvedSegment's
// bulge direction is a 90-degree rotation of p0->p1, so without this a
// "forward" circuit and a "reversed" circuit sharing a segment get
// assigned opposite offsetFractions that, combined with their opposite
// direction vectors, land on the SAME side instead of opposite ones —
// their curves end up exactly on top of each other rather than fanned
// apart. Multiplying the offset by this sign re-anchors every circuit's
// bulge to the same canonical side regardless of its own storage
// direction, so circuits sharing a segment always fan out consistently.
function segmentDirectionSign(p0, p1) {
  return pointKey(p0) <= pointKey(p1) ? 1 : -1;
}

// Deliberately well short of the full viewport — a map that fills nearly
// the whole screen leaves almost no room to land the mouse off it, and
// Leaflet zooms on scroll wheel by default, so it was eating page-scroll
// input instead of letting the page scroll (the "Currently down" table
// etc. below it) like a normal page. ~45% of the window leaves plenty of
// room either side of the map to scroll normally.
function sizeMapContainer() {
  const el = document.getElementById("map");
  el.style.height = Math.max(320, Math.round(window.innerHeight * 0.45)) + "px";
}

// Map view picker (hand-saved MapRegion views, see Manage Map Views): a
// plain cookie, not a server round-trip, so a senior manager who wants the
// whole picture and someone who only cares about one view each just get
// whatever they last picked back on their next visit, page to page.
const REGION_COOKIE = "sitewatch_region";

function getCookie(name) {
  const match = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
  return match ? decodeURIComponent(match[1]) : null;
}

function setCookie(name, value) {
  document.cookie = `${name}=${encodeURIComponent(value)}; path=/; max-age=${60 * 60 * 24 * 365}`;
}

async function loadMap() {
  sizeMapContainer();
  const map = L.map("map").setView([39.8, -98.6], 4); // CONUS fallback until sites load (or if there are none)
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);

  window.addEventListener("resize", () => {
    sizeMapContainer();
    map.invalidateSize();
  });

  const res = await fetch("/api/map");
  const data = await res.json();

  // Frame the view around however spread out the sites actually are — tight
  // clusters (a handful of sites in one state) zoom in, wide spreads (coast
  // to coast) zoom out. maxZoom caps how far a tight cluster (including a
  // single site) zooms in, so there's still room to see what's around it.
  // This is what "All" in the region picker means — always re-fit to
  // whatever sites currently exist, not a saved view.
  function fitToAllSites() {
    if (data.sites.length > 0) {
      const bounds = L.latLngBounds(data.sites.map((s) => [s.lat, s.lon]));
      map.fitBounds(bounds, { padding: [60, 60], maxZoom: 11 });
    }
  }
  fitToAllSites();

  // Lines first, markers last — Leaflet stacks later-added vector layers on
  // top, and site marker/line endpoints often share exact coordinates. If
  // markers went first, clicking a site would hit the line instead and show
  // the circuit's popup rather than the site's.
  //
  // Overlap grouping is per SEGMENT (each leg between two consecutive
  // points along a circuit's site_a -> waypoints -> site_b path), not per
  // whole circuit — two circuits with different real endpoints can still
  // share a run of waypoints in the middle (e.g. two circuits that both
  // route through the same regional hub) and would draw exactly on top of
  // each other along that shared stretch even though their overall
  // endpoint pairs never match. A same-endpoint-pair pair of circuits with
  // no waypoints is just the one-segment case of this.
  const bySegment = {};
  data.lines.forEach((l) => {
    const points = [l.site_a, ...(l.waypoints || []), l.site_b];
    for (let i = 0; i < points.length - 1; i++) {
      const key = segmentKey(points[i], points[i + 1]);
      (bySegment[key] = bySegment[key] || []).push(l.id);
    }
  });
  // Sorted, not insertion-order — a circuit lands on the same side across
  // every segment it shares with the same other circuits, instead of
  // potentially flipping sides leg to leg.
  Object.values(bySegment).forEach((ids) => ids.sort((a, b) => a - b));

  data.lines.forEach((l) => {
    const points = [l.site_a, ...(l.waypoints || []), l.site_b];
    const path = curvedPathBySegment(points, (i) => {
      const group = bySegment[segmentKey(points[i], points[i + 1])];
      const idx = group.indexOf(l.id);
      // Fraction of THIS leg's own length, not a fixed lat/lon amount —
      // see curvedSegment's comment for why that matters. Multiplied by
      // segmentDirectionSign so every circuit sharing this segment fans
      // out relative to the same canonical side, regardless of which
      // direction each one happens to traverse it in — see that
      // function's comment for why that's necessary.
      const sign = segmentDirectionSign(points[i], points[i + 1]);
      return (idx - (group.length - 1) / 2) * 0.15 * sign;
    });
    const utilLabel = l.util_pct != null ? ` — ${l.util_pct}% util` : "";
    L.polyline(path, { color: CIRCUIT_COLOR[l.state] || "#000", weight: 4 })
      .bindPopup(`<a href="/circuits/${l.id}">${l.name}</a> (${l.role})${utilLabel}`).addTo(map);
  });

  // Passthrough markers first and deliberately muted (small, translucent,
  // no bringToFront) — they're map waypoints, not monitored sites, and at
  // a zoomed-out view with real sites sparse they used to visually dominate
  // the screen at full-strength purple. Real sites draw after and get
  // bringToFront, so they always sit on top even where a passthrough point
  // shares close to the same coordinates.
  data.sites.filter((s) => s.site_type === "passthrough").forEach((s) => {
    L.circleMarker([s.lat, s.lon], {
      radius: 6, color: STATUS_COLOR.passthrough, fillColor: STATUS_COLOR.passthrough,
      weight: 1, opacity: 0.85, fillOpacity: 0.55,
    }).bindPopup(`<a href="/sites/${s.id}">${s.name}</a> (passthrough)`).addTo(map);
  });

  // Minor sites are fully monitored (not muted like passthrough) but drawn
  // smaller than a Major Site's marker to read as subordinate at a glance —
  // still full-opacity/status-colored and bringToFront like a major site.
  data.sites.filter((s) => s.site_type === "minor").forEach((s) => {
    L.circleMarker([s.lat, s.lon], {
      radius: 8, color: STATUS_COLOR[s.status], fillColor: STATUS_COLOR[s.status],
      fillOpacity: 0.9,
    }).bindPopup(`<a href="/sites/${s.id}">${s.name}</a> (minor)`).addTo(map).bringToFront();
  });

  data.sites.filter((s) => s.site_type === "site").forEach((s) => {
    L.circleMarker([s.lat, s.lon], {
      radius: 10, color: STATUS_COLOR[s.status], fillColor: STATUS_COLOR[s.status],
      fillOpacity: 0.9,
    }).bindPopup(`<a href="/sites/${s.id}">${s.name}</a>`).addTo(map).bringToFront();
  });

  const regionSelect = document.getElementById("region_select");
  if (regionSelect) {
    function applySelectedRegion() {
      setCookie(REGION_COOKIE, regionSelect.value);
      if (regionSelect.value === "all") {
        fitToAllSites();
        return;
      }
      const opt = regionSelect.selectedOptions[0];
      if (opt && opt.dataset.lat) {
        map.setView([Number(opt.dataset.lat), Number(opt.dataset.lon)], Number(opt.dataset.zoom));
      }
    }

    const saved = getCookie(REGION_COOKIE);
    if (saved && [...regionSelect.options].some((o) => o.value === saved)) {
      regionSelect.value = saved;
    }
    applySelectedRegion(); // may override the "All" fit already done above
    regionSelect.addEventListener("change", applySelectedRegion);
  }
}

loadMap();
