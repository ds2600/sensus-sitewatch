// Polls /api/alerts, updates the header bell badge, and dings once per new
// alert (not once per poll) so it doesn't nag on every refresh.
let lastSeenCount = 0;

async function pollAlerts() {
  const res = await fetch("/api/alerts");
  const data = await res.json();
  const badge = document.getElementById("alert-count");

  if (data.count > 0) {
    badge.textContent = data.count;
    badge.classList.remove("d-none");
  } else {
    badge.classList.add("d-none");
  }

  if (data.count > lastSeenCount) {
    const sound = document.getElementById("alert-sound");
    if (sound.src) sound.play().catch(() => {});
  }
  lastSeenCount = data.count;
}

pollAlerts();
setInterval(pollAlerts, 30000);
