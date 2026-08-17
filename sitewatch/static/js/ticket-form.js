// Ajax-saves any form.ticket-form (the dashboard's inline "Ticket #"
// fields) in place, no page reload — entering several of these in a row
// during an incident shouldn't cost a full round trip each time. Enter
// key or the Save button both submit normally; this just intercepts that
// submit. Falls back to a plain POST + redirect if JS never runs (the
// route itself still handles that — see set_incident_ticket).
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("form.ticket-form").forEach((form) => {
    const button = form.querySelector("button");
    const status = document.createElement("span");
    status.className = "small ms-1 align-self-center";
    form.appendChild(status);
    let fadeTimer = null;

    function showStatus(text, cls, fade) {
      clearTimeout(fadeTimer);
      status.textContent = text;
      status.className = "small ms-1 align-self-center " + cls;
      if (fade) fadeTimer = setTimeout(() => { status.textContent = ""; }, 1500);
    }

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      button.disabled = true;
      fetch(form.action, {
        method: "POST",
        headers: { "X-Requested-With": "XMLHttpRequest" },
        body: new FormData(form),
      })
        .then((res) => {
          if (!res.ok) throw new Error();
          showStatus("Saved", "text-success", true);
        })
        .catch(() => showStatus("Failed to save", "text-danger", false))
        .finally(() => { button.disabled = false; });
    });
  });
});
