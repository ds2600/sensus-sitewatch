// Polls /api/poller-status and updates: the header icon (green play triangle
// while running, red square when stopped/disabled), its tooltip, and — on
// pages that have them — the "next poll cycle" text on the Settings page.
// A separate 1s tick recomputes the countdown between server fetches so it
// counts down live instead of just refreshing every 15s, and flips to
// "overdue by ..." if the poller is still mid-cycle (or stuck) past when it
// was scheduled to fire again — the point being to make drift visible
// instead of just saying "waiting" with no way to tell if that's normal.

function formatClockTime(iso) {
  return new Date(iso).toLocaleTimeString();
}

function formatCountdown(iso) {
  const diffSec = Math.round((new Date(iso).getTime() - Date.now()) / 1000);
  const sign = diffSec >= 0 ? "in" : "overdue by";
  const abs = Math.abs(diffSec);
  const m = Math.floor(abs / 60);
  const s = abs % 60;
  return `${sign} ${m > 0 ? m + "m " : ""}${s}s`;
}

function updateNextRunDisplays() {
  const settingsEl = document.getElementById("settings-poller-next-run");
  if (settingsEl) {
    const iso = settingsEl.dataset.nextRun;
    settingsEl.textContent = iso ? ` Next cycle ${formatCountdown(iso)}.` : "";
  }
  const pollerEl = document.getElementById("poller-next-run");
  if (pollerEl) {
    const iso = pollerEl.dataset.nextRun;
    pollerEl.textContent = iso ? `Next poll cycle: ${formatClockTime(iso)} (${formatCountdown(iso)})` : "";
  }
}

async function pollPollerStatus() {
  const res = await fetch("/api/poller-status");
  const data = await res.json();

  const symbol = document.getElementById("poller-status-symbol");
  if (symbol) {
    if (data.active) {
      symbol.innerHTML = "&#9654;"; // ▶
      symbol.classList.remove("text-danger");
      symbol.classList.add("text-success");
    } else {
      symbol.innerHTML = "&#9724;"; // ■
      symbol.classList.remove("text-success");
      symbol.classList.add("text-danger");
    }
  }
  const link = document.getElementById("poller-status-icon");
  if (link) {
    link.title = data.next_run_at
      ? `Poller: ${data.label} — next at ${formatClockTime(data.next_run_at)}`
      : `Poller: ${data.label}`;
  }

  const settingsEl = document.getElementById("settings-poller-next-run");
  if (settingsEl) settingsEl.dataset.nextRun = data.next_run_at || "";
  const pollerEl = document.getElementById("poller-next-run");
  if (pollerEl) pollerEl.dataset.nextRun = data.next_run_at || "";
  updateNextRunDisplays();
}

pollPollerStatus();
setInterval(pollPollerStatus, 15000);
setInterval(updateNextRunDisplays, 1000);
