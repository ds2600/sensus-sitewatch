// A real searchable dropdown: text input + a Bootstrap .dropdown-menu panel
// that opens on focus, filters live as you type, and closes on pick/blur/Esc.
// Uses Bootstrap's own dropdown-menu/dropdown-item CSS (already loaded) for
// the panel look — this file only drives show/hide/filtering/keyboard nav,
// not Bootstrap's JS toggle plugin, since that doesn't support live filtering.
//
// searchableSelect({ input, hidden, menu, options, onSelect })
//   input:  the visible text field
//   hidden: the actual value that gets submitted (option.id)
//   menu:   the <ul class="dropdown-menu"> to populate
//   options: array of {id, label}, OR a function returning that array
//            (for a list that depends on another field, e.g. interface
//            choices depend on which device is picked)
// Returns { refresh() } — call refresh() after changing what options()
// returns (e.g. a different device was picked) to reset the input/menu.
function searchableSelect({ input, hidden, menu, options, onSelect }) {
  let highlighted = -1;
  let shown = [];

  function currentOptions() {
    return typeof options === "function" ? options() : options;
  }

  function renderMenu(filterText) {
    const all = currentOptions();
    const needle = (filterText || "").trim().toLowerCase();
    shown = needle ? all.filter((o) => o.label.toLowerCase().includes(needle)) : all;
    highlighted = -1;
    menu.innerHTML = "";
    if (shown.length === 0) {
      const li = document.createElement("li");
      li.innerHTML = '<span class="dropdown-item disabled">No matches</span>';
      menu.appendChild(li);
      return;
    }
    shown.forEach((opt, idx) => {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = "#";
      a.className = "dropdown-item";
      a.textContent = opt.label;
      a.addEventListener("mousedown", (e) => {
        e.preventDefault(); // fires before blur, so the field doesn't close the menu first
        pick(opt);
      });
      a.addEventListener("mouseenter", () => setHighlight(idx));
      li.appendChild(a);
      menu.appendChild(li);
    });
  }

  function setHighlight(idx) {
    const items = menu.querySelectorAll(".dropdown-item:not(.disabled)");
    items.forEach((el, i) => el.classList.toggle("active", i === idx));
    highlighted = idx;
  }

  function pick(opt) {
    input.value = opt.label;
    hidden.value = opt.id;
    close();
    if (onSelect) onSelect(opt);
  }

  function open() {
    renderMenu(input.value);
    menu.classList.add("show");
  }

  function close() {
    menu.classList.remove("show");
  }

  input.addEventListener("focus", open);
  input.addEventListener("click", open);
  input.addEventListener("input", () => {
    hidden.value = ""; // typing invalidates the prior pick until something is chosen again
    open();
  });
  input.addEventListener("blur", () => setTimeout(close, 150));
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!menu.classList.contains("show")) return open();
      setHighlight(Math.min(highlighted + 1, shown.length - 1));
      menu.querySelectorAll(".dropdown-item")[highlighted]?.scrollIntoView({ block: "nearest" });
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlight(Math.max(highlighted - 1, 0));
      menu.querySelectorAll(".dropdown-item")[highlighted]?.scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter") {
      // No arrow-key nav yet? Enter still picks the top filtered match —
      // otherwise it falls through and submits the surrounding form, which
      // is never what "type and hit enter" means for a picker like this.
      const target = shown[highlighted >= 0 ? highlighted : 0];
      if (menu.classList.contains("show") && target) {
        e.preventDefault();
        pick(target);
      }
    } else if (e.key === "Escape") {
      close();
    }
  });

  return { refresh: () => renderMenu(input.value) };
}
