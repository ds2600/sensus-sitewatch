// Wires up every searchable picker on circuit_form.html: role, parent
// bundle, device -> interface endpoints (add mode only), and the ordered
// waypoint list. Each follows the same pattern — a text input with a
// <datalist> for the dropdown-with-search-built-in look, backed by a
// hidden input (or, for waypoints, a hidden comma-separated list) that's
// what actually gets submitted.
document.addEventListener("DOMContentLoaded", () => {
  function jsonData(id) {
    const el = document.getElementById(id);
    return el ? JSON.parse(el.textContent) : [];
  }

  function wireSearchPicker(searchId, hiddenId, options, { clearsToEmpty } = {}) {
    const search = document.getElementById(searchId);
    const hidden = document.getElementById(hiddenId);
    if (!search || !hidden) return;
    const idByLabel = Object.fromEntries(options.map((o) => [o.label, o.id]));
    search.addEventListener("input", () => {
      if (clearsToEmpty && search.value === clearsToEmpty) {
        hidden.value = "";
        return;
      }
      hidden.value = idByLabel[search.value] || "";
    });
  }

  wireSearchPicker("role_search", "role_id", jsonData("roles-data"));
  wireSearchPicker("parent_search", "parent_circuit_id", jsonData("bundles-data"), { clearsToEmpty: "None" });

  // --- endpoint pickers (device -> interface), add mode only ---
  const devicesDataEl = document.getElementById("devices-data");
  const interfacesDataEl = document.getElementById("interfaces-by-device-data");
  if (devicesDataEl && interfacesDataEl) {
    const devices = JSON.parse(devicesDataEl.textContent);
    const interfacesByDevice = JSON.parse(interfacesDataEl.textContent);
    const deviceIdByLabel = Object.fromEntries(devices.map((d) => [d.label, d.id]));

    function wireEndpoint(letter) {
      const deviceSearch = document.getElementById(`device_${letter}_search`);
      const ifaceSearch = document.getElementById(`interface_${letter}_search`);
      const ifaceDatalist = document.getElementById(`interfaces_${letter}_datalist`);
      const ifaceHidden = document.getElementById(`interface_${letter}_id`);
      let ifaceIdByLabel = {};

      deviceSearch.addEventListener("input", () => {
        const deviceId = deviceIdByLabel[deviceSearch.value];
        ifaceSearch.value = "";
        ifaceHidden.value = "";
        ifaceDatalist.innerHTML = "";
        ifaceIdByLabel = {};

        if (!deviceId) {
          ifaceSearch.disabled = true;
          ifaceSearch.placeholder = "Select a device first…";
          return;
        }
        const opts = interfacesByDevice[deviceId] || [];
        ifaceDatalist.innerHTML = opts
          .map((o) => `<option value="${o.label.replace(/"/g, "&quot;")}">`)
          .join("");
        ifaceIdByLabel = Object.fromEntries(opts.map((o) => [o.label, o.id]));
        ifaceSearch.disabled = false;
        ifaceSearch.placeholder = opts.length ? "Search interface…" : "No available interfaces on this device";
      });

      ifaceSearch.addEventListener("input", () => {
        ifaceHidden.value = ifaceIdByLabel[ifaceSearch.value] || "";
      });
    }

    wireEndpoint("a");
    wireEndpoint("b");
  }

  // --- waypoints: ordered add/remove/drag-to-reorder list ---
  const waypointSearch = document.getElementById("waypoint_search");
  const waypointList = document.getElementById("waypoint_list");
  const waypointHidden = document.getElementById("waypoint_site_ids");
  if (waypointSearch && waypointList && waypointHidden) {
    const sites = jsonData("sites-data");
    const siteById = Object.fromEntries(sites.map((s) => [s.id, s.label]));
    let waypoints = jsonData("existing-waypoints-data"); // [{id, label}, ...] in order
    let dragIdx = null;

    function render() {
      waypointList.innerHTML = "";
      waypoints.forEach((wp, idx) => {
        const li = document.createElement("li");
        li.className = "list-group-item d-flex align-items-center py-1";
        li.draggable = true;
        li.dataset.idx = idx;

        const handle = document.createElement("span");
        handle.textContent = "⠇"; // drag handle (⠇)
        handle.className = "text-body-secondary me-2";
        handle.style.cursor = "grab";
        li.appendChild(handle);

        const label = document.createElement("span");
        label.className = "flex-grow-1";
        label.textContent = wp.label;
        li.appendChild(label);

        const removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.className = "btn btn-sm btn-outline-secondary";
        removeBtn.textContent = "×";
        removeBtn.title = "Remove";
        removeBtn.addEventListener("click", () => {
          waypoints.splice(idx, 1);
          render();
        });
        li.appendChild(removeBtn);

        li.addEventListener("dragstart", () => {
          dragIdx = idx;
          li.classList.add("opacity-50");
        });
        li.addEventListener("dragend", () => li.classList.remove("opacity-50"));
        li.addEventListener("dragover", (e) => e.preventDefault());
        li.addEventListener("drop", (e) => {
          e.preventDefault();
          if (dragIdx === null || dragIdx === idx) return;
          const [moved] = waypoints.splice(dragIdx, 1);
          waypoints.splice(idx, 0, moved);
          dragIdx = null;
          render();
        });

        waypointList.appendChild(li);
      });
      waypointHidden.value = waypoints.map((wp) => wp.id).join(",");
    }

    document.getElementById("add_waypoint_btn").addEventListener("click", () => {
      const label = waypointSearch.value;
      const id = Object.entries(siteById).find(([, l]) => l === label)?.[0];
      if (!id) return;
      waypoints.push({ id: Number(id), label });
      waypointSearch.value = "";
      render();
    });

    render();
  }

  // --- submit validation ---
  const isBundle = document.getElementById("is_bundle");
  document.querySelector("form").addEventListener("submit", (e) => {
    if (!document.getElementById("role_id").value) {
      e.preventDefault();
      alert("Pick a role from the list.");
      return;
    }
    if (isBundle && isBundle.checked) return;
    if (document.getElementById("interface_a_id")) {
      const aId = document.getElementById("interface_a_id").value;
      const bId = document.getElementById("interface_b_id").value;
      if (!aId || !bId) {
        e.preventDefault();
        alert("Pick both interfaces — search for the device first, then choose one of its interfaces from the list.");
      }
    }
  });
});
