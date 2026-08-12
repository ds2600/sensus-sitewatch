// Any <tr data-href="..."> navigates on click — one delegated listener
// covers every current and future table, no per-page wiring. Clicks on an
// actual link/button/form/input inside the row still do their own thing
// (edit link, mute/repoll buttons, etc.) instead of also navigating.
document.addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-href]");
  if (!row) return;
  if (e.target.closest("a, button, input, select, form")) return;
  window.location = row.dataset.href;
});
