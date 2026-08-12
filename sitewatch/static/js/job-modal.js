// Drives the #job-log-modal (see base.html) for any button carrying
// data-job-url: POSTs there to kick off a background job (device walk/repoll
// today), then polls /api/jobs/<id>/log for new lines every 400ms and
// appends them, auto-scrolling. The modal can't be dismissed (no
// backdrop/ESC, Close stays disabled) until the job reports done=true, so
// the user always sees the real outcome instead of a click making the
// window disappear mid-operation. Closing afterward reloads the
// originating page so the device's fresh state (interfaces, reachability,
// last-polled) is visible without a second manual refresh.
let jobStartedAt = null;
let jobElapsedTimer = null;
let jobPollTimer = null;
let jobRedirectUrl = null;

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

function finishJob(success, error) {
  clearInterval(jobElapsedTimer);
  const statusEl = document.getElementById("job-log-status");
  if (success) {
    statusEl.textContent = "Completed successfully.";
    statusEl.className = "me-auto text-success";
  } else {
    statusEl.textContent = "Failed" + (error ? ": " + error : ".");
    statusEl.className = "me-auto text-danger";
  }
  document.getElementById("job-log-close-btn").disabled = false;
  document.getElementById("job-log-close-x").style.display = "";
}

function pollJobLog(jobId, since) {
  fetch(`/api/jobs/${jobId}/log?since=${since}`)
    .then((r) => r.json())
    .then((data) => {
      if (data.error) {
        appendLines([data.error]);
        finishJob(false, data.error);
        return;
      }
      appendLines(data.lines);
      if (data.done) {
        finishJob(data.success, data.error);
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
  jobRedirectUrl = null;
  jobStartedAt = Date.now();
  updateElapsed();
  clearInterval(jobElapsedTimer);
  jobElapsedTimer = setInterval(updateElapsed, 200);
  bsModal.show();

  fetch(url, { method: "POST" })
    .then((r) => r.json())
    .then((data) => {
      if (data.job_id) {
        jobRedirectUrl = data.redirect || null;
        if (data.label) document.getElementById("job-log-title").textContent = data.label;
        pollJobLog(data.job_id, 0);
      } else {
        appendLines([data.error || "Failed to start."]);
        finishJob(false, data.error);
      }
    })
    .catch((err) => {
      appendLines(["Failed to start: " + err]);
      finishJob(false, String(err));
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
  jobRedirectUrl = null;
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

  document.querySelectorAll("[data-job-url]").forEach((btn) => {
    btn.addEventListener("click", () => startJob(btn.dataset.jobUrl, btn.dataset.jobLabel));
  });
});
