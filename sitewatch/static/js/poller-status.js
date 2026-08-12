// Polls /api/poller-status and updates the header icon: a green play
// triangle while the poller is running (waiting on its interval or mid-sweep),
// a red square when it's stopped or not running in this process at all.
async function pollPollerStatus() {
  const res = await fetch("/api/poller-status");
  const data = await res.json();
  const link = document.getElementById("poller-status-icon");
  const symbol = document.getElementById("poller-status-symbol");
  if (!link || !symbol) return;

  if (data.active) {
    symbol.innerHTML = "&#9654;"; // ▶
    symbol.classList.remove("text-danger");
    symbol.classList.add("text-success");
  } else {
    symbol.innerHTML = "&#9724;"; // ■
    symbol.classList.remove("text-success");
    symbol.classList.add("text-danger");
  }
  link.title = "Poller: " + data.label;
}

pollPollerStatus();
setInterval(pollPollerStatus, 15000);
