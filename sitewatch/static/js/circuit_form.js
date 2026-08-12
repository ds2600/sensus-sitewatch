// Wires up circuit_form.html: role, parent bundle, and (add mode only)
// device -> interface endpoint pickers all use searchable_select.js. The
// waypoint list adds its own picker plus an ordered add/remove/drag-to-reorder
// list backed by a hidden comma-separated field.
document.addEventListener("DOMContentLoaded", () => {
  function jsonData(id) {
    const el = document.getElementById(id);
    return el ? JSON.parse(el.textContent) : [];
  }

  searchableSelect({
    input: document.getElementById("role_search"),
    hidden: document.getElementById("role_id"),
    menu: document.getElementById("role_menu"),
    options: jsonData("roles-data"),
  });

  searchableSelect({
    input: document.getElementById("parent_search"),
    hidden: document.getElementById("parent_circuit_id"),
    menu: document.getElementById("parent_menu"),
    options: jsonData("bundles-data"),
  });

  // --- device -> interface pickers: leaf endpoints (a/b, add mode only)
  // and a bundle's optional LAG interfaces (lag_a/lag_b, add or edit) ---
  const devices = jsonData("devices-data");
  const interfacesByDevice = jsonData("interfaces-by-device-data");

  function wireEndpoint(prefix) {
    const deviceInput = document.getElementById(`device_${prefix}_search`);
    const ifaceInput = document.getElementById(`interface_${prefix}_search`);
    const ifaceHidden = document.getElementById(`interface_${prefix}_id`);
    if (!deviceInput || !ifaceInput || !ifaceHidden) return; // not on this form (leaf fields only exist in add mode)

    let ifaceOptions = [];

    searchableSelect({
      input: ifaceInput,
      hidden: ifaceHidden,
      menu: document.getElementById(`interface_${prefix}_menu`),
      options: () => ifaceOptions,
    });

    searchableSelect({
      input: deviceInput,
      hidden: { value: "" }, // the device pick itself isn't submitted, only the interface it resolves to
      menu: document.getElementById(`device_${prefix}_menu`),
      options: devices,
      onSelect: (device) => {
        ifaceOptions = interfacesByDevice[device.id] || [];
        ifaceInput.value = "";
        ifaceHidden.value = "";
        ifaceInput.disabled = ifaceOptions.length === 0;
        ifaceInput.placeholder = ifaceOptions.length ? "Search interface…" : "No interfaces available";
      },
    });

    // Pre-filled on load (editing a circuit that already has this set) —
    // resolve the interface list for the already-shown device so the
    // picker works immediately without forcing a re-pick first.
    if (deviceInput.value) {
      const preset = devices.find((d) => d.label === deviceInput.value);
      if (preset) ifaceOptions = interfacesByDevice[preset.id] || [];
    }
  }

  wireEndpoint("a");
  wireEndpoint("b");
  wireEndpoint("lag_a");
  wireEndpoint("lag_b");

  // --- waypoints: ordered add/remove/drag-to-reorder list ---
  const waypointList = document.getElementById("waypoint_list");
  const waypointHidden = document.getElementById("waypoint_site_ids");
  if (waypointList && waypointHidden) {
    const waypointSearch = document.getElementById("waypoint_search");
    const waypointPick = { value: "" };
    searchableSelect({
      input: waypointSearch,
      hidden: waypointPick,
      menu: document.getElementById("waypoint_menu"),
      options: jsonData("sites-data"),
    });

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
        handle.textContent = "⠇";
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
      if (!waypointPick.value) return;
      waypoints.push({ id: Number(waypointPick.value), label: waypointSearch.value });
      waypointSearch.value = "";
      waypointPick.value = "";
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
        alert("Pick both interfaces — search for the device first, then choose one of its interfaces.");
      }
    }
  });
});
