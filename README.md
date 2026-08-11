# Sensus SiteWatch

Rudimentary NMS. Tracks sites, devices, and circuits. Displays status on a map. Polls devices via SNMP (v1/v2c/v3). Multi-vendor: Cisco IOS-XE, IOS-XR, Juniper Junos.

Rename note: app name is a single config value (`APP_NAME` in `.env`). Change it there, not in code.

---

## 1. Requirements

- WSL2, Ubuntu 22.04 or newer (WSL setup itself is assumed done)
- Python 3.11+
- pip
- git (optional, if pulling from a repo)
- Network reachability from WSL to target devices (SNMP UDP/161, SSH TCP/22 if using NAPALM discovery)

Check Python version:

```bash
python3 --version
```

---

## 2. Installation

```bash
# 1. Place project files in a working directory
cd ~
mkdir sensus-sitewatch && cd sensus-sitewatch
# (copy project files here)

# 2. Create virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy environment template and edit it
cp .env.example .env
nano .env
```

### Required `.env` values

| Variable | Purpose | Default |
|---|---|---|
| `APP_NAME` | Display name | Sensus SiteWatch |
| `SECRET_KEY` | Flask session signing | generate random, required |
| `ENCRYPTION_KEY` | Encrypts stored SNMP/SSH credentials at rest | generate random, required |
| `ADMIN_USERNAME` | First login user | admin |
| `ADMIN_PASSWORD` | First login password | set your own, required |
| `DATABASE_PATH` | SQLite file location | `instance/sitewatch.db` |
| `NETBOX_URL` | NetBox base URL, optional | blank = sync disabled |
| `NETBOX_TOKEN` | NetBox API token, optional | blank = sync disabled |

Generate `SECRET_KEY` and `ENCRYPTION_KEY`:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Run once for each key, paste results into `.env`.

---

## 3. First run

```bash
source venv/bin/activate
flask --app app init-db      # creates SQLite schema, seeds admin user
SITEWATCH_RUN_POLLER=1 flask --app app run --host=0.0.0.0 --port=5000
```

`SITEWATCH_RUN_POLLER=1` starts the background poller. Without it the app
runs but never polls anything — devices/circuits just sit at their default
state forever. Omit it only if you deliberately want the UI up without
live polling (e.g. browsing existing data).

Access from Windows host browser: `http://localhost:5000`
(WSL2 forwards localhost automatically — no extra config needed.)

Log in with `ADMIN_USERNAME` / `ADMIN_PASSWORD` from `.env`.

To run persistently instead of foreground: use `nohup`, `screen`, `tmux`, or a systemd user service inside WSL. Pick one — not covered here, ask if needed.

---

## 4. Settings (Settings page, first thing to configure)

| Setting | Default | Notes |
|---|---|---|
| Polling interval | 2 minutes | applies to all devices unless overridden per-device |
| Down threshold | 3 consecutive failed polls | prevents single dropped poll from flagping a circuit down |
| Default SNMP version | v2c | set per-device, this is just the form default |
| Google Chat webhook URL | blank | required for alert delivery |
| Alert mute max duration | 60 minutes | hard cap, not editable per-mute beyond this |

Circuit roles (tier mapping) also live in Settings — see Section 7.

---

## 5. Adding sites

Two paths:

**Manual**: Sites page → Add Site → name, latitude, longitude.

**NetBox sync**: Settings → NetBox → Sync Now. Pulls sites as NetBox Sites. Requires `NETBOX_URL` and `NETBOX_TOKEN` set in `.env`. Re-run Sync Now any time to pick up new/changed sites. Sync is pull-only — nothing pushes back to NetBox.

If a site is deleted in NetBox, it is not auto-deleted here. It is flagged "out of sync" on the Sites page. Review and remove manually if confirmed gone.

---

## 6. Adding devices

Devices page → Add Device (or pulled via NetBox sync, same out-of-sync flagging rule applies).

Required fields:
- Hostname / management IP
- Site (assign to one)
- Vendor: IOS-XE / IOS-XR / Junos
- SNMP version: v1 / v2c / v3
  - v1/v2c: community string
  - v3: username, auth protocol, auth key, priv protocol, priv key
- SSH credentials (optional, used for NAPALM-based discovery features)

Credentials are encrypted at rest using `ENCRYPTION_KEY`. Losing that key means re-entering all credentials — back it up separately from the database.

---

## 7. Discovering interfaces (walk)

Device page → Walk Now. Runs an SNMP walk of IF-MIB, populates the interface list (ifDescr, ifAlias, ifSpeed, oper/admin status).

Re-run Walk Now any time new interfaces are added on the device (new circuits, new hardware). Existing interface-to-circuit mappings are preserved by ifIndex; only new interfaces appear as unmapped.

If NAPALM-based LAG/bundle detection is enabled (Port-channel, Bundle-Ether, ae interfaces), a suggested bundle mapping appears after the walk. This is a suggestion only — confirm or reject it manually. Nothing is auto-created.

---

## 8. Building circuits

Circuits page → Add Circuit.

- Point-to-point only: pick interface on Device A, interface on Device B.
- Optional parent: assign to an existing bundle circuit, or create a new bundle from two or more circuits.
- Role: assign a circuit role (see Section 9). Default role tier: critical.
- Capacity: defaults to ifSpeed from the walk. Override manually if the real ceiling differs (rate-limited/policed circuits).

Bundles show as a single line on the map. Individual members are visible by expanding the bundle (click "+" on the map line, or open the bundle's detail page).

---

## 9. Circuit roles and status tiers

Settings → Circuit Roles.

Each role has a name (free text, e.g. "core", "backbone", "office", "management") and a tier: **critical** or **auxiliary**.

Rule:
- A site's external status (up/degraded/down) is calculated from **critical**-tier circuits only, using the degree rule: one degree down with only one degree total = down; multiple degrees with at least one down = degraded.
- **Auxiliary**-tier circuits never push a site to red. If an auxiliary circuit or any internal (intra-site) circuit is down while all critical external circuits are up, the site shows degraded, not down.
- Unmapped/new roles default to critical tier (fail-safe).

Add/edit roles any time. Existing circuits keep their assigned role; tier changes take effect on next status calculation (immediate).

---

## 10. Status color legend

| Color | Meaning |
|---|---|
| Green | fully up |
| Yellow | degraded — see Section 9 for what triggers this |
| Red | down — all critical external paths to the site are down |
| Gray | interface is admin-down (intentionally disabled) — excluded from status math, flagged for review |
| Blue | device or entire site unreachable via SNMP — distinct from a confirmed down interface, since the device itself isn't answering |

---

## 11. Alerts

**Delivery**: Google Chat webhook. Set the webhook URL in Settings. Payload template is in `sensus/integrations/webhook_payload.py` — edit this file directly to change the message format sent to Google Chat.

**Trigger**: alert fires on initial transition to down only (green/yellow → red, or up → down at the circuit level). Does not re-alert while still down. Fires again only after a clear-then-re-down cycle.

**In-browser**: alert icon in the UI header, audible ding on new alert, badge count of unacknowledged alerts.

**Muting**: per-circuit, from the circuit's detail page. Choose mute duration up to 60 minutes. Mute auto-expires and reverts to normal alerting — no manual unmute required, though a manual unmute option is also available.

---

## 12. Status history

Circuit detail page and a dashboard-level history view show:
- Currently down: circuit name, down-since timestamp, duration
- Recently cleared: circuit name, cleared timestamp, how long it was down

Retention: configurable in Settings, no default assumed yet — set based on how far back you want to look.

---

## 13. Data location / backup

- Database: `instance/sitewatch.db` (SQLite, single file)
- Encryption key: `.env` — back this up separately from the database, or encrypted credentials become unrecoverable
- Back up by copying the `.db` file. Stop the app first, or accept a small risk of an in-flight write being mid-transaction (SQLite handles this reasonably well, but a clean stop is safer)

---

## 14. Testing without real devices

If you can't reach real devices yet (ACLs, firewall, not deployed), run in
simulate mode instead. This swaps SNMP calls for a fake backend that
generates plausible telemetry — no network access needed.

```bash
source venv/bin/activate
rm -f instance/sitewatch.db          # start clean if you already ran init-db
flask --app app init-db
SITEWATCH_SIMULATE=1 flask --app app seed-demo
SITEWATCH_SIMULATE=1 SITEWATCH_RUN_POLLER=1 flask --app app run --host=0.0.0.0 --port=5000
```

`seed-demo` drives several poll cycles itself as part of seeding, so the
map and dashboard show the correct states (down/degraded/unreachable/etc.)
the instant you load the page — you don't have to wait on the scheduler.
`SITEWATCH_RUN_POLLER=1` on the `run` command is still worth setting so
things keep updating live afterward (e.g. to watch a mute expire).

`seed-demo` builds 4 sites, 6 devices, and a handful of circuits scripted
to hit every status color:

| What you'll see | Why |
|---|---|
| Chicago DC / Denver DC: yellow | Core bundle between them is fully up (critical), but an auxiliary office link is down — demonstrates auxiliary failures cap at yellow, never red |
| Austin DC: red | Single critical circuit to Chicago, down, no redundancy |
| Phoenix DC: blue, with a blue line to Denver | Device tagged unreachable — demonstrates both the whole-site-unreachable case and an unreachable circuit rendering on the map, not just an isolated site |
| CHI-DEN-Office-Backup circuit: gray | Tagged admin-down — excluded from status math entirely |
| One core bundle member: high but not alarming utilization | Demonstrates the utilization numbers on the device detail page without needing real traffic |

`SITEWATCH_SIMULATE=1` must be set for both `seed-demo` and `flask run` —
it swaps every device's SNMP calls for synthetic data generated from tags
in each interface's alias field (see `sitewatch/simulator.py`). Real
devices added alongside demo ones will still fail to poll while simulate
mode is on, since it's a global switch, not per-device — unset the
variable and restart once you have real device access.

To script your own scenarios: after a walk, edit an interface's alias in
the database to include a tag like `[sim:down]`, `[sim:admin_down]`,
`[sim:near_capacity]`, or `[sim:flapping]`. `sitewatch/simulator.py` lists
the full set.

## 15. Troubleshooting

**Device shows blue immediately after adding it**: check SNMP reachability manually first:
```bash
snmpwalk -v2c -c <community> <device-ip> 1.3.6.1.2.1.1.1.0
```
If this fails from WSL directly, the app will fail identically — it's a network/credential issue, not an app issue.

**Walk returns no interfaces**: confirm SNMP version/credentials match the device config exactly. v3 is the most common mismatch point (auth/priv protocol must match device config exactly, not just the key).

**Circuit stuck yellow after fix confirmed**: check the down threshold setting — a circuit needs the configured number of consecutive successful polls to clear, same as it needs consecutive failures to trigger. This prevents flapping from generating alert noise, but means clearing isn't instant.

**Google Chat alerts not arriving**: test the webhook URL directly:
```bash
curl -X POST -H "Content-Type: application/json" -d '{"text":"test"}' <webhook-url>
```
If this fails, the URL or Google Chat space config is the issue, not the app.
