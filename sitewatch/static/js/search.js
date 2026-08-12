// Navbar quick search: debounced fetch to /api/search, results grouped by
// type in a Bootstrap dropdown-menu. "/" focuses the box from anywhere
// (unless already typing in a field) so keyboard users never touch the mouse.
(() => {
  const input = document.getElementById("search-input");
  const menu = document.getElementById("search-menu");
  if (!input || !menu) return;

  const dropdown = new bootstrap.Dropdown(input, { autoClose: true });
  let debounceTimer = null;
  let items = [];
  let activeIndex = -1;

  const urlFor = {
    sites: (id) => `/sites/${id}`,
    devices: (id) => `/devices/${id}`,
    circuits: (id) => `/circuits/${id}`,
  };
  const labelFor = { sites: "Sites", devices: "Devices", circuits: "Circuits" };

  function render(data) {
    menu.innerHTML = "";
    items = [];
    for (const type of ["sites", "devices", "circuits"]) {
      const group = data[type] || [];
      if (!group.length) continue;
      const header = document.createElement("li");
      header.className = "dropdown-header";
      header.textContent = labelFor[type];
      menu.appendChild(header);
      for (const row of group) {
        const li = document.createElement("li");
        const a = document.createElement("a");
        a.className = "dropdown-item";
        a.href = urlFor[type](row.id);
        a.textContent = row.label;
        li.appendChild(a);
        menu.appendChild(li);
        items.push(a);
      }
    }
    if (!items.length) {
      const li = document.createElement("li");
      li.innerHTML = '<span class="dropdown-item-text text-body-secondary">No matches</span>';
      menu.appendChild(li);
    }
    activeIndex = -1;
    if (!dropdown._element.matches(":focus")) dropdown.show();
  }

  function setActive(i) {
    items.forEach((el) => el.classList.remove("active"));
    if (i >= 0 && i < items.length) {
      items[i].classList.add("active");
      items[i].scrollIntoView({ block: "nearest" });
    }
    activeIndex = i;
  }

  input.addEventListener("input", () => {
    clearTimeout(debounceTimer);
    const q = input.value.trim();
    if (q.length < 2) {
      dropdown.hide();
      return;
    }
    debounceTimer = setTimeout(() => {
      fetch(`/api/search?q=${encodeURIComponent(q)}`)
        .then((r) => r.json())
        .then(render);
    }, 200);
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!items.length) return;
      setActive((activeIndex + 1) % items.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (!items.length) return;
      setActive((activeIndex - 1 + items.length) % items.length);
    } else if (e.key === "Enter") {
      if (activeIndex >= 0 && items[activeIndex]) {
        e.preventDefault();
        window.location = items[activeIndex].getAttribute("href");
      }
    } else if (e.key === "Escape") {
      dropdown.hide();
      input.blur();
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key !== "/") return;
    const tag = document.activeElement.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    e.preventDefault();
    input.focus();
  });
})();
