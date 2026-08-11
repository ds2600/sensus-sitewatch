// Renders the site/circuit map on the dashboard. Colors follow status.py's
// green/yellow/red for sites, plus per-circuit state for lines.
const STATUS_COLOR = { green: "#198754", yellow: "#ffc107", red: "#dc3545", blue: "#0d6efd" };
const CIRCUIT_COLOR = { up: "#198754", degraded: "#ffc107", down: "#dc3545",
                         admin_down: "#6c757d", unreachable: "#0d6efd" };

async function loadMap() {
  const map = L.map("map").setView([39.8, -98.6], 4); // CONUS fallback until sites load (or if there are none)
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);

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
      const offset = (idx - (group.length - 1) / 2) * 0.05;
      L.polyline(
        [[l.site_a.lat + offset, l.site_a.lon + offset], [l.site_b.lat + offset, l.site_b.lon + offset]],
        { color: CIRCUIT_COLOR[l.state] || "#000", weight: 4 }
      ).bindPopup(`<a href="/circuits/${l.id}">${l.name}</a> (${l.role})`).addTo(map);
    });
  });

  data.sites.forEach((s) => {
    L.circleMarker([s.lat, s.lon], {
      radius: 10, color: STATUS_COLOR[s.status], fillColor: STATUS_COLOR[s.status], fillOpacity: 0.9,
    }).bindPopup(`<a href="/sites/${s.id}">${s.name}</a>`).addTo(map).bringToFront();
  });
}

loadMap();
