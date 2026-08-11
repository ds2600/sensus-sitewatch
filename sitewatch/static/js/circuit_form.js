// Wires up the searchable device -> interface pickers on the "Add circuit"
// form (circuit_form.html). Only present when adding a leaf circuit —
// bundles skip this, and edit mode doesn't allow changing endpoints at all.
document.addEventListener("DOMContentLoaded", () => {
  const devicesDataEl = document.getElementById("devices-data");
  const interfacesDataEl = document.getElementById("interfaces-by-device-data");
  if (!devicesDataEl || !interfacesDataEl) return;

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
      const options = interfacesByDevice[deviceId] || [];
      ifaceDatalist.innerHTML = options
        .map((o) => `<option value="${o.label.replace(/"/g, "&quot;")}">`)
        .join("");
      ifaceIdByLabel = Object.fromEntries(options.map((o) => [o.label, o.id]));
      ifaceSearch.disabled = false;
      ifaceSearch.placeholder = options.length ? "Search interface…" : "No available interfaces on this device";
    });

    ifaceSearch.addEventListener("input", () => {
      ifaceHidden.value = ifaceIdByLabel[ifaceSearch.value] || "";
    });
  }

  wireEndpoint("a");
  wireEndpoint("b");

  const isBundle = document.getElementById("is_bundle");
  document.querySelector("form").addEventListener("submit", (e) => {
    if (isBundle && isBundle.checked) return;
    const aId = document.getElementById("interface_a_id").value;
    const bId = document.getElementById("interface_b_id").value;
    if (!aId || !bId) {
      e.preventDefault();
      alert("Pick both interfaces — search for the device first, then choose one of its interfaces from the list.");
    }
  });
});
