// Dashboard-only auto-refresh toggle, off by default (deliberately not
// hitting the API on a timer unless the user opts in). The dashboard has
// no partial-update path for the map or the down/cleared tables (see
// map.js: /api/map is fetched once, on load), so "refresh" here just
// means reloading the whole page — that re-renders everything correctly
// for free. The toggle's on/off state is remembered in a cookie so it
// survives the very reload it causes.
(() => {
  const COOKIE = "sitewatch_autorefresh";
  const INTERVAL_MS = 30000;
  const btn = document.getElementById("auto-refresh-toggle");
  if (!btn) return;

  function getCookie(name) {
    const match = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return match ? decodeURIComponent(match[1]) : null;
  }
  function setCookie(name, value) {
    document.cookie = `${name}=${encodeURIComponent(value)}; path=/; max-age=${60 * 60 * 24 * 365}`;
  }
  function setLabel(on) {
    btn.textContent = on ? "Auto-refresh: On (30s)" : "Auto-refresh: Off";
    btn.classList.toggle("btn-outline-secondary", !on);
    btn.classList.toggle("btn-secondary", on);
  }

  let enabled = getCookie(COOKIE) === "1";
  setLabel(enabled);
  if (enabled) setTimeout(() => window.location.reload(), INTERVAL_MS);

  btn.addEventListener("click", () => {
    enabled = !enabled;
    setCookie(COOKIE, enabled ? "1" : "0");
    setLabel(enabled);
    if (enabled) setTimeout(() => window.location.reload(), INTERVAL_MS);
  });
})();
