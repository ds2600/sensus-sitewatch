"""Google Chat alert delivery. Edit build_payload() to change the message
format — this is the one function that controls what Google Chat displays.

Alerts are grouped: every circuit that goes down together within one poll
cycle or repoll action sends exactly one webhook call, not one per circuit
— see poller.py's alert_batch threading through _transition/poll_all_devices/
poll_device_now. A pager full of separate one-liners for a single-device
outage that took out five circuits is worse than one message that says so.
"""
import requests
from sitewatch.models import Setting
from sitewatch.status import compute_site_status

_STATUS_EMOJI = {"red": "\U0001F534", "blue": "\U0001F535", "yellow": "\U0001F7E1"}
_STATUS_WORD = {"red": "down", "blue": "unreachable", "yellow": "degraded"}
# Rendering order, worst first — a reader scanning top-to-bottom sees the
# worst-hit sites before the merely-degraded ones.
_STATUS_RANK = {"red": 0, "blue": 1, "yellow": 2}


def build_payload(circuits):
    """circuits: non-empty list of Circuit objects that just transitioned
    to down together (see module docstring). Two sections: the circuits
    themselves, then the resulting site impact — computed fresh via
    compute_site_status() AFTER every circuit in the batch has already
    applied its state change, so a site pushed to red by two circuits
    going down together reads as one red line, not a stale yellow."""
    count = len(circuits)
    lines = [f"\U0001F534 *{count} circuit{'s' if count != 1 else ''} down*", ""]

    for c in circuits:
        site_names = " ↔ ".join(filter(None, [
            c.site_a.name if c.site_a else None,
            c.site_b.name if c.site_b else None,
        ])) or "unknown site"
        lines.append(f"• *{c.name}* ({c.role.name}) — {site_names}")

    affected_sites = {}
    for c in circuits:
        for site in (c.site_a, c.site_b):
            if site and site.id not in affected_sites:
                affected_sites[site.id] = site
    # Only sites actually knocked off green are worth a line — a merely
    # auxiliary circuit going down on an otherwise-healthy site is still
    # reported above (it's still a real down circuit) but doesn't need its
    # own "site impact" line if the site itself is still green.
    site_impact = sorted(
        ((site, status) for site in affected_sites.values()
         if (status := compute_site_status(site)) in _STATUS_RANK),
        key=lambda pair: _STATUS_RANK[pair[1]],
    )

    if site_impact:
        lines.append("")
        lines.append(f"*Site impact ({len(site_impact)}):*")
        for site, status in site_impact:
            lines.append(f"{_STATUS_EMOJI[status]} *{site.name}* — {_STATUS_WORD[status]}")

    _append_link(lines)
    return {"text": "\n".join(lines)}


def _append_link(lines):
    url = Setting.get("sitewatch_url")
    if url:
        lines.append("")
        lines.append(f"<{url}|Open SiteWatch>")


def _post_best_effort(payload):
    """Shared delivery for every alert path that must never break its
    caller over a bad webhook (poll cycles, poller backoff/recovery) — the
    one exception is send_test_alert(), which deliberately raises so a
    broken URL is caught from the Settings page, not discovered mid-outage."""
    url = Setting.get("google_chat_webhook_url")
    if not url:
        return
    try:
        requests.post(url, json=payload, timeout=5)
    except requests.RequestException:
        pass  # alert delivery failure shouldn't break the poll cycle


def send_down_alerts(circuits):
    """circuits: every Circuit that just transitioned to down in this poll
    cycle/repoll action — call once with the whole batch, never once per
    circuit. No-op if the batch is empty or no webhook URL is configured,
    so every caller can call this unconditionally after its own loop."""
    if not circuits:
        return
    _post_best_effort(build_payload(circuits))


def build_poller_backoff_payload(consecutive_failures):
    lines = [
        f"\U0001F7E0 *Poller backed off* — every device failed "
        f"{consecutive_failures} poll cycle{'s' if consecutive_failures != 1 else ''} in a row.",
        "",
        "Polling continues at a slower interval until a device responds again.",
    ]
    _append_link(lines)
    return {"text": "\n".join(lines)}


def build_poller_recovered_payload():
    lines = [
        "\U0001F7E2 *Poller recovered* — a device responded again. Polling resumed at the normal interval.",
    ]
    _append_link(lines)
    return {"text": "\n".join(lines)}


def send_poller_backoff_alert(consecutive_failures):
    """Fired once, on the transition into backoff — see poller.py's
    _apply_failure_backoff, which guards against calling this again on
    every subsequent failed cycle while already backed off."""
    _post_best_effort(build_poller_backoff_payload(consecutive_failures))


def send_poller_recovered_alert():
    """Fired once, on the transition back out of backoff."""
    _post_best_effort(build_poller_recovered_payload())


def build_probe_stale_payload(probe):
    lines = [
        f"\U0001F7E0 *Probe stale: {probe.name}* — hasn't checked in for over "
        f"{Setting.get_int('probe_stale_after_minutes')} minutes.",
        "",
        "Its devices are now shown unreachable until it checks in again.",
    ]
    _append_link(lines)
    return {"text": "\n".join(lines)}


def build_probe_recovered_payload(probe):
    lines = [
        f"\U0001F7E2 *Probe recovered: {probe.name}* — checked in again.",
    ]
    _append_link(lines)
    return {"text": "\n".join(lines)}


def send_probe_stale_alert(probe):
    """Fired once, on the transition into staleness — see poller.py's
    check_probe_staleness, which guards against calling this again on
    every subsequent watchdog pass while a probe stays stale."""
    _post_best_effort(build_probe_stale_payload(probe))


def send_probe_recovered_alert(probe):
    """Fired once, on the transition back out of staleness (the probe's
    next successful report — see poller.py's apply_probe_report)."""
    _post_best_effort(build_probe_recovered_payload(probe))


def build_test_payload():
    """A fixed example message — not built from real data, since a
    realistic build_payload() call needs persisted Circuit/Site rows
    (compute_site_status() queries the database). Clearly marked as a
    test so it can never be mistaken for a real outage in the channel,
    but same shape/formatting as the real thing so Settings' "Send test
    alert" button actually shows what an alert looks like."""
    lines = [
        "\U0001F9EA *Test alert from SiteWatch* — this is only a drill.",
        "",
        "\U0001F534 *2 circuits down*",
        "",
        "• *Example-Core* (core) — Chicago DC ↔ Denver DC",
        "• *Example-Office* (office) — Chicago DC ↔ Denver DC",
        "",
        "*Site impact (1):*",
        "\U0001F534 *Denver DC* — down",
    ]
    _append_link(lines)
    return {"text": "\n".join(lines)}


def send_test_alert():
    """Settings page's "Send test alert" button. Posts to whatever webhook
    URL is currently SAVED (Setting.get, not an unsaved form value — save
    first). Unlike send_down_alerts(), delivery failures are NOT swallowed
    here: this exists specifically to catch a bad URL/network problem
    before it's discovered during a real outage, so the route needs the
    real exception to report back to the admin. Raises requests.
    RequestException on failure; returns False with no request made at
    all if no URL is configured."""
    url = Setting.get("google_chat_webhook_url")
    if not url:
        return False
    response = requests.post(url, json=build_test_payload(), timeout=5)
    response.raise_for_status()
    return True
