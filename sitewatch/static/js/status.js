// Single poll target for the header: alert bell + poller icon both update
// from one /api/status fetch instead of two separate requests. A separate
// 1s tick recomputes the poller's countdown between fetches so it counts
// down live, flipping to "overdue by ..." if a cycle runs past when it was
// due — makes drift visible instead of just saying "waiting".

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
  const el = document.getElementById("poller-next-run");
  if (!el) return;
  if (el.dataset.state === "polling") {
    // A sweep in progress means next_run_at is already in the past (the
    // scheduled fire time that kicked it off) — running the countdown
    // against it would just say "overdue by Ns", which reads as a
    // problem when the poller is doing exactly what it should. Show real
    // status instead: device progress if this cycle reports one (see
    // poller.py's job_log.set_total/set_progress), else a plain "running".
    const total = el.dataset.progressTotal;
    const completed = el.dataset.progressCompleted;
    el.textContent = total ? `Polling now — ${completed}/${total} device(s)` : "Polling now…";
    return;
  }
  const iso = el.dataset.nextRun;
  el.textContent = iso ? `Next poll: ${formatClockTime(iso)} (${formatCountdown(iso)})` : "";
}

function updateAlerts(alerts) {
  const badge = document.getElementById("alert-count");
  if (!badge) return;
  if (alerts.count > 0) {
    badge.textContent = alerts.count;
    badge.classList.remove("d-none");
  } else {
    badge.classList.add("d-none");
  }
  if (alerts.count > (updateAlerts.lastSeen || 0)) {
    const sound = document.getElementById("alert-sound");
    if (sound && sound.src) sound.play().catch(() => {});
  }
  updateAlerts.lastSeen = alerts.count;
}

function updatePoller(poller) {
  const symbol = document.getElementById("poller-status-symbol");
  if (symbol) {
    if (poller.active) {
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
  if (link) link.title = "Poller: " + poller.label;

  const el = document.getElementById("poller-next-run");
  if (el) {
    el.dataset.nextRun = poller.next_run_at || "";
    el.dataset.state = poller.state || "";
    el.dataset.progressCompleted = poller.progress ? poller.progress.completed : "";
    el.dataset.progressTotal = poller.progress ? poller.progress.total : "";
  }
  updateNextRunDisplays();

  const badge = document.getElementById("poller-status-badge");
  if (badge) {
    badge.textContent = poller.label;
    badge.className = "badge " + (poller.active ? "bg-success" : "bg-danger");
  }
  document.querySelectorAll("[data-poller-toggle]").forEach((btn) => {
    const isStart = btn.dataset.pollerToggle === "start";
    btn.disabled = poller.state === "disabled" || (isStart ? poller.active : !poller.active);
  });

  const viewLogBtn = document.getElementById("poller-view-log-btn");
  if (viewLogBtn) {
    viewLogBtn.dataset.viewJobId = poller.job_id || "";
    viewLogBtn.disabled = !poller.job_id;
  }
}

function jobStatusBadge(job) {
  const span = document.createElement("span");
  if (!job.done) {
    span.className = "badge bg-primary";
    span.textContent = "running";
  } else if (job.cancelled) {
    span.className = "badge bg-warning text-dark";
    span.textContent = "stopped";
  } else {
    span.className = "badge " + (job.success ? "bg-success" : "bg-danger");
    span.textContent = job.success ? "ok" : "failed";
  }
  return span;
}

function activityRow(job) {
  const tr = document.createElement("tr");

  const labelTd = document.createElement("td");
  labelTd.textContent = job.label;

  const statusTd = document.createElement("td");
  statusTd.appendChild(jobStatusBadge(job));

  const startedTd = document.createElement("td");
  startedTd.textContent = new Date(job.started_at + "Z").toLocaleTimeString();

  const durationTd = document.createElement("td");
  durationTd.textContent = job.elapsed + "s";

  const actionTd = document.createElement("td");
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn btn-outline-secondary btn-sm";
  btn.textContent = "View log";
  btn.addEventListener("click", () => viewJob(job.id, job.label));
  actionTd.appendChild(btn);

  tr.append(labelTd, statusTd, startedTd, durationTd, actionTd);
  return tr;
}

async function updateActivity() {
  const tbody = document.getElementById("activity-rows");
  if (!tbody) return;
  const res = await fetch("/api/jobs?limit=100");
  const data = await res.json();
  tbody.innerHTML = "";
  if (data.jobs.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 5;
    td.className = "text-body-secondary";
    td.textContent = "Nothing yet.";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }
  data.jobs.forEach((job) => tbody.appendChild(activityRow(job)));
}

async function pollStatus() {
  const res = await fetch("/api/status");
  const data = await res.json();
  updateAlerts(data.alerts);
  updatePoller(data.poller);
  updateActivity();
}

pollStatus();
setInterval(pollStatus, 15000);
setInterval(updateNextRunDisplays, 1000);
