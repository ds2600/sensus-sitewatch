// Renders the site/circuit map on the dashboard. Colors follow status.py's
// green/yellow/red for sites, plus per-circuit state for lines.
const STATUS_COLOR = { green: "#198754", yellow: "#ffc107", red: "#dc3545", blue: "#0d6efd", passthrough: "#6f42c1" };
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

// Sizes #map against the actual rendered header height rather than a flat
// vh guess, which can't know how tall the navbar really is (wrapped nav
// links on a narrow window, browser chrome, etc). Leaves a little room
// below for breathing room rather than running edge-to-edge.
function sizeMapContainer() {
  const el = document.getElementById("map");
  const nav = document.querySelector("nav.navbar");
  const navHeight = nav ? nav.getBoundingClientRect().height : 0;
  const chrome = navHeight + 64; // container-fluid's top padding + a bit of breathing room below
  el.style.height = Math.max(300, window.innerHeight - chrome) + "px";
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
  if (data.sites.length > 0) {
    const bounds = L.latLngBounds(data.sites.map((s) => [s.lat, s.lon]));
    map.fitBounds(bounds, { padding: [60, 60], maxZoom: 11 });
  }

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

  data.sites.forEach((s) => {
    const isPassthrough = s.site_type === "passthrough";
    L.circleMarker([s.lat, s.lon], {
      radius: isPassthrough ? 7 : 10, color: STATUS_COLOR[s.status], fillColor: STATUS_COLOR[s.status],
      fillOpacity: 0.9,
    }).bindPopup(`<a href="/sites/${s.id}">${s.name}</a>${isPassthrough ? " (passthrough)" : ""}`)
      .addTo(map).bringToFront();
  });
}

loadMap();
