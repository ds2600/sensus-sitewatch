"""Google Chat alert delivery. Edit build_payload() to change the message
format — this is the one function that controls what Google Chat displays.
"""
import requests
from sitewatch.models import Setting


def build_payload(circuit):
    site_names = ", ".join(filter(None, [
        circuit.site_a.name if circuit.site_a else None,
        circuit.site_b.name if circuit.site_b else None,
    ]))
    text = f"*Circuit down:* {circuit.name}\n*Role:* {circuit.role.name}\n*Sites:* {site_names}"
    return {"text": text}


def send_down_alert(circuit):
    url = Setting.get("google_chat_webhook_url")
    if not url:
        return
    try:
        requests.post(url, json=build_payload(circuit), timeout=5)
    except requests.RequestException:
        pass  # alert delivery failure shouldn't break the poll cycle
