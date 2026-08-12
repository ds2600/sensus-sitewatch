// Renders the site/circuit map on the dashboard. Colors follow status.py's
// green/yellow/red for sites, plus per-circuit state for lines.
const STATUS_COLOR = { green: "#198754", yellow: "#ffc107", red: "#dc3545", blue: "#0d6efd", passthrough: "#8a8fa3" };
const CIRCUIT_COLOR = { up: "#198754", degraded: "#ffc107", down: "#dc3545",
                         admin_down: "#6c757d", unreachable: "#0d6efd" };

// Quadratic bezier from p0 to p1, bulging toward a control point offset
// perpendicular to the p0->p1 line by `magnitude` at the midpoint. p0/p1
// are untouched — only the middle of the curve moves — so multiple
// parallel circuits between the same two sites can fan out for visibility
// without their endpoints drifting off the site markers they connect to.
function curvedSegment(p0, p1, magnitude, steps = 16) {
  const dLat = p1[0] - p0[0];
  const dLon = p1[1] - p0[1];
  const len = Math.sqrt(dLat * dLat + dLon * dLon) || 1;
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

// A circuit's full path (site_a -> waypoints -> site_b) as one curved
// polyline — each leg curves independently so waypoints stay fixed
// pass-through points, not just the two real endpoints.
function curvedPath(points, magnitude) {
  const latLngs = points.map((p) => [p.lat, p.lon]);
  if (!magnitude) return latLngs;
  let path = [];
  for (let i = 0; i < latLngs.length - 1; i++) {
    const seg = curvedSegment(latLngs[i], latLngs[i + 1], magnitude);
    path = path.concat(i === 0 ? seg : seg.slice(1)); // drop duplicate joint point
  }
  return path;
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
  const byPair = {};
  data.lines.forEach((l) => {
    const key = [l.site_a.lat + "," + l.site_a.lon, l.site_b.lat + "," + l.site_b.lon].sort().join("|");
    byPair[key] = byPair[key] || [];
    byPair[key].push(l);
  });

  Object.values(byPair).forEach((group) => {
    group.forEach((l, idx) => {
      const magnitude = (idx - (group.length - 1) / 2) * 0.08;
      const points = curvedPath([l.site_a, ...(l.waypoints || []), l.site_b], magnitude);
      L.polyline(points, { color: CIRCUIT_COLOR[l.state] || "#000", weight: 4 })
        .bindPopup(`<a href="/circuits/${l.id}">${l.name}</a> (${l.role})`).addTo(map);
    });
  });

  // Passthrough markers first and deliberately muted (small, translucent,
  // no bringToFront) — they're map waypoints, not monitored sites, and at
  // a zoomed-out view with real sites sparse they used to visually dominate
  // the screen at full-strength purple. Real sites draw after and get
  // bringToFront, so they always sit on top even where a passthrough point
  // shares close to the same coordinates.
  data.sites.filter((s) => s.site_type === "passthrough").forEach((s) => {
    L.circleMarker([s.lat, s.lon], {
      radius: 5, color: STATUS_COLOR.passthrough, fillColor: STATUS_COLOR.passthrough,
      weight: 1, opacity: 0.6, fillOpacity: 0.35,
    }).bindPopup(`<a href="/sites/${s.id}">${s.name}</a> (passthrough)`).addTo(map);
  });

  data.sites.filter((s) => s.site_type !== "passthrough").forEach((s) => {
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
