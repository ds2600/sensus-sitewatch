// Blocks the screen while a form tagged class="js-blocking-form" is
// submitting — used for device walk/repoll, which run synchronous SNMP
// round-trips server-side and can take several seconds. Prevents a stray
// click or double-submit from piling up a second request on top.
document.addEventListener("submit", (e) => {
  const form = e.target;
  if (!(form instanceof HTMLFormElement) || !form.classList.contains("js-blocking-form")) return;

  const overlay = document.getElementById("busy-overlay");
  const text = document.getElementById("busy-overlay-text");
  if (!overlay) return;

  text.textContent = form.dataset.busyText || "Working…";
  overlay.classList.remove("d-none");

  const btn = form.querySelector("button[type=submit], button:not([type])");
  if (btn) btn.disabled = true;
});
