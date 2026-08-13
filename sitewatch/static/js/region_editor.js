// Manage Regions page: a plain reference map (site markers only, no
// circuit lines — this page is about framing a view, not reading status)
// you pan/zoom, then "Save current view as region" captures
// map.getCenter()/getZoom() into the save-region modal's hidden fields.
const REGION_STATUS_COLOR = { green: "#198754", yellow: "#ffc107", red: "#dc3545", blue: "#0d6efd", passthrough: "#6f42c1" };

document.addEventListener("DOMContentLoaded", async () => {
  const mapEl = document.getElementById("region-edit-map");
  if (!mapEl) return;

  const map = L.map("region-edit-map").setView([39.8, -98.6], 4);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);

  const res = await fetch("/api/map");
  const data = await res.json();
  data.sites.forEach((s) => {
    const color = REGION_STATUS_COLOR[s.status] || "#666";
    L.circleMarker([s.lat, s.lon], { radius: 6, color, fillColor: color, fillOpacity: 0.9 })
      .bindPopup(s.name)
      .addTo(map);
  });
  if (data.sites.length > 0) {
    map.fitBounds(L.latLngBounds(data.sites.map((s) => [s.lat, s.lon])), { padding: [60, 60], maxZoom: 11 });
  }

  // Read-only users don't get the Save button or its modal (see regions.html) —
  // null-check instead of assuming it's there.
  const saveBtn = document.getElementById("save_region_btn");
  if (saveBtn) {
    saveBtn.addEventListener("click", () => {
      const center = map.getCenter();
      document.getElementById("save_center_lat").value = center.lat;
      document.getElementById("save_center_lon").value = center.lng;
      document.getElementById("save_zoom").value = map.getZoom();
      bootstrap.Modal.getOrCreateInstance(document.getElementById("save-region-modal")).show();
    });
  }

  document.querySelectorAll("[data-goto-lat]").forEach((btn) => {
    btn.addEventListener("click", () => {
      map.setView([Number(btn.dataset.gotoLat), Number(btn.dataset.gotoLon)], Number(btn.dataset.gotoZoom));
    });
  });
});
