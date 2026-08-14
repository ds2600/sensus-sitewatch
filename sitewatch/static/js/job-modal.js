// Drives the Tail Modal (#job-log-modal, see base.html) for any button
// carrying data-job-url: POSTs there to kick off a background job (device
// walk/repoll, circuit repoll, the repoll-all-unreachable sweep), then
// polls /api/jobs/<id>/log for new lines every 400ms and appends them,
// auto-scrolling — plus a completed/total counter (see updateProgress)
// for jobs that report one. data-confirm on a button asks first and
// aborts silently on Cancel, no modal shown — used for the larger
// repoll-all-unreachable action. The modal can't be dismissed (no
// backdrop/ESC, Close stays disabled) until the job reports done=true, so
// the user always sees the real outcome instead of a click making the
// window disappear mid-operation. Closing afterward reloads the
// originating page so the device's fresh state (interfaces, reachability,
// last-polled) is visible without a second manual refresh.
//
// The Stop button (#job-log-stop-btn) POSTs to /api/jobs/<id>/cancel —
// see job_log.request_cancel for what that can and can't actually
// interrupt. It's a request, not an instant kill: the modal keeps
// tailing normally until the job itself notices and reports done, same
// as any other finish.
let jobStartedAt = null;
let jobElapsedTimer = null;
let jobPollTimer = null;
let jobRedirectUrl = null;
let currentJobId = null;

function jobModalEl() {
  return document.getElementById("job-log-modal");
}

function updateElapsed() {
  if (jobStartedAt === null) return;
  const secs = ((Date.now() - jobStartedAt) / 1000).toFixed(1);
  document.getElementById("job-log-elapsed").textContent = secs + "s elapsed";
}

function appendLines(lines) {
  if (!lines || lines.length === 0) return;
  const pre = document.getElementById("job-log-lines");
  pre.textContent += lines.join("\n") + "\n";
  pre.scrollTop = pre.scrollHeight;
}

// Only jobs that report a total (job_log.start_job(..., total=N), e.g. the
// repoll-all-unreachable sweep) show anything here — an ordinary walk/repoll
// job's total is always null, so this stays blank for those, no layout change.
function updateProgress(completed, total) {
  const el = document.getElementById("job-log-progress");
  if (!el) return;
  el.textContent = total != null ? `${completed}/${total}` : "";
}

function setStopEnabled(enabled) {
  const stopBtn = document.getElementById("job-log-stop-btn");
  if (!stopBtn) return;
  stopBtn.disabled = !enabled;
  stopBtn.textContent = "Stop";
}

function finishJob(success, error, cancelled) {
  clearInterval(jobElapsedTimer);
  const statusEl = document.getElementById("job-log-status");
  if (cancelled) {
    statusEl.textContent = "Stopped.";
    statusEl.className = "me-auto text-warning";
  } else if (success) {
    statusEl.textContent = "Completed successfully.";
    statusEl.className = "me-auto text-success";
  } else {
    statusEl.textContent = "Failed" + (error ? ": " + error : ".");
    statusEl.className = "me-auto text-danger";
  }
  document.getElementById("job-log-close-btn").disabled = false;
  document.getElementById("job-log-close-x").style.display = "";
  setStopEnabled(false);
}

function pollJobLog(jobId, since) {
  fetch(`/api/jobs/${jobId}/log?since=${since}`)
    .then((r) => r.json())
    .then((data) => {
      if (data.done === undefined) {
        // 404 shape — {"error": "Job not found or expired."}, no other keys —
        // distinct from a legitimately finished job, which always has `done`
        // and may ALSO carry a non-null error (a real failure, or "Stopped by
        // user." for a cancelled one) that the done-branch below handles.
        appendLines([data.error]);
        finishJob(false, data.error, false);
        return;
      }
      appendLines(data.lines);
      updateProgress(data.completed, data.total);
      if (data.done) {
        finishJob(data.success, data.error, data.cancelled);
      } else {
        jobPollTimer = setTimeout(() => pollJobLog(jobId, data.next_index), 400);
      }
    })
    .catch(() => {
      // Transient fetch hiccup (not a job failure) — keep tailing.
      jobPollTimer = setTimeout(() => pollJobLog(jobId, since), 1000);
    });
}

function startJob(url, label) {
  const modalEl = jobModalEl();
  const bsModal = bootstrap.Modal.getOrCreateInstance(modalEl);

  document.getElementById("job-log-title").textContent = label || "Working…";
  document.getElementById("job-log-lines").textContent = "";
  document.getElementById("job-log-status").textContent = "Running…";
  document.getElementById("job-log-status").className = "me-auto";
  document.getElementById("job-log-close-btn").disabled = true;
  document.getElementById("job-log-close-x").style.display = "none";
  updateProgress(0, null);
  jobRedirectUrl = null;
  currentJobId = null;
  setStopEnabled(false);  // re-enabled once we actually have a job_id back
  jobStartedAt = Date.now();
  updateElapsed();
  clearInterval(jobElapsedTimer);
  jobElapsedTimer = setInterval(updateElapsed, 200);
  bsModal.show();

  fetch(url, { method: "POST" })
    .then((r) => r.json())
    .then((data) => {
      if (data.job_id) {
        currentJobId = data.job_id;
        jobRedirectUrl = data.redirect || null;
        if (data.label) document.getElementById("job-log-title").textContent = data.label;
        setStopEnabled(true);
        pollJobLog(data.job_id, 0);
      } else {
        appendLines([data.error || "Failed to start."]);
        finishJob(false, data.error, false);
      }
    })
    .catch((err) => {
      appendLines(["Failed to start: " + err]);
      finishJob(false, String(err), false);
    });
}

function viewJob(jobId, label) {
  // Same modal, but for an already-started job (from the Background
  // activity list) — skip the POST-to-start step and just tail/show its
  // log from the top. Works whether the job already finished (shows the
  // full log immediately, Close enabled right away) or is still running.
  const modalEl = jobModalEl();
  const bsModal = bootstrap.Modal.getOrCreateInstance(modalEl);

  document.getElementById("job-log-title").textContent = label || "Activity";
  document.getElementById("job-log-lines").textContent = "";
  document.getElementById("job-log-status").textContent = "Loading…";
  document.getElementById("job-log-status").className = "me-auto";
  document.getElementById("job-log-close-btn").disabled = true;
  document.getElementById("job-log-close-x").style.display = "none";
  updateProgress(0, null);
  jobRedirectUrl = null;
  currentJobId = jobId;
  // Optimistic — if the job turns out to already be done, the first
  // pollJobLog response below calls finishJob(), which disables this again.
  setStopEnabled(true);
  jobStartedAt = Date.now();
  updateElapsed();
  clearInterval(jobElapsedTimer);
  jobElapsedTimer = setInterval(updateElapsed, 200);
  bsModal.show();

  pollJobLog(jobId, 0);
}

document.addEventListener("DOMContentLoaded", () => {
  const closeAndReload = () => {
    clearTimeout(jobPollTimer);
    const bsModal = bootstrap.Modal.getInstance(jobModalEl());
    if (bsModal) bsModal.hide();
    if (jobRedirectUrl) window.location.href = jobRedirectUrl;
  };
  const closeBtn = document.getElementById("job-log-close-btn");
  const closeX = document.getElementById("job-log-close-x");
  if (closeBtn) closeBtn.addEventListener("click", closeAndReload);
  if (closeX) closeX.addEventListener("click", closeAndReload);

  const stopBtn = document.getElementById("job-log-stop-btn");
  if (stopBtn) {
    stopBtn.addEventListener("click", () => {
      if (!currentJobId || stopBtn.disabled) return;
      stopBtn.disabled = true;
      stopBtn.textContent = "Stopping…";
      fetch(`/api/jobs/${currentJobId}/cancel`, { method: "POST" }).catch(() => {});
      // No local finishJob() call here — the request only sets a flag
      // server-side (see job_log.request_cancel); the actual stop happens
      // whenever the job itself next checks it, and the normal poll loop
      // above picks up the resulting done:true/cancelled:true same as any
      // other finish. Re-enabling the button on failure would just let
      // someone spam it while it's already in flight, for no benefit.
    });
  }

  // Cosmetic only — the real 60s-per-target rate limit is enforced
  // server-side (sitewatch/cooldown.py), which is what actually stops a
  // click from starting a job; a 429 shows up in the modal same as any
  // other start failure. This just avoids the wasted round trip most of
  // the time and gives instant feedback. Clears on page reload — fine,
  // the server-side check doesn't depend on it.
  const CLIENT_COOLDOWN_MS = 60000;
  document.querySelectorAll("[data-job-url]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
      startJob(btn.dataset.jobUrl, btn.dataset.jobLabel);
      btn.disabled = true;
      setTimeout(() => { btn.disabled = false; }, CLIENT_COOLDOWN_MS);
    });
  });
});
