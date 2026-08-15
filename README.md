# Sensus SiteWatch

Rudimentary NMS. Tracks sites, devices, and circuits. Displays status on a map. Polls devices via SNMP (v1/v2c/v3). Multi-vendor: Cisco IOS-XE, IOS-XR, Juniper Junos.

Rename note: app name is a single config value (`APP_NAME` in `.env`). Change it there, not in code.

Versioning: SemVer, tracked as `__version__` in `sitewatch/__init__.py` and
shown in the page footer of every screen. Releases are tagged in git
(`vX.Y.Z`) and published via GitHub Releases — bump `__version__`, commit,
`git tag vX.Y.Z`, `git push --tags`, then `gh release create vX.Y.Z`.
Currently pre-1.0 (`0.x`): breaking changes can still happen between
minor versions until the first `1.0.0`.

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
| Down threshold | 3 consecutive failed polls | prevents single dropped poll from flagging a circuit down |
| Max concurrent devices polled | 8 | how many devices get SNMP-polled at once each cycle — a different knob from gunicorn's `--workers`, see Section 15.2 |
| SNMP version | v1 (Add Device form's first option — not actually configured here) | set per-device on the Add/Edit Device form |
| Google Chat webhook URL | blank | required for alert delivery |
| Alert mute max duration | 60 minutes | hard cap, not editable per-mute beyond this |
| Status history retention | 30 days | stored, but nothing prunes old records yet — no effect until that's implemented |

Circuit roles (tier mapping) also live in Settings — see Section 9.

---

## 5. Adding sites

Three paths:

**Manual**: Sites page → Add Site → name, latitude, longitude.

**CSV import**: Sites page → Import CSV. Columns: Site, Lat, Long, Type (Type optional, defaults to "site"; the other allowed value is "passthrough"). Upload, review a per-row validation preview (nothing is written until you confirm), then import.

**NetBox sync**: Settings → NetBox → Sync Now. Pulls sites as NetBox Sites. Requires `NETBOX_URL` and `NETBOX_TOKEN` set in `.env`. Re-run Sync Now any time to pick up new/changed sites. Sync is pull-only — nothing pushes back to NetBox.

If a site is deleted in NetBox, it is not auto-deleted here. It is flagged "out of sync" on the Sites page. Review and remove manually if confirmed gone.

Sites can optionally be grouped into **Regions** (Settings → Regions) for sorting/filtering the Sites list and quick search — unrelated to the map's own "Map views" (saved camera positions, Settings → Map views).

---

## 6. Adding devices

Two paths:

**Manual**: Devices page → Add Device (or pulled via NetBox sync, same out-of-sync flagging rule applies).

**CSV import**: Devices page → Import CSV. Columns: Site, Hostname, Mgmt IP, Vendor. Site must match an existing site by name; Hostname/Mgmt IP are rejected if already in use. SNMP credentials aren't part of the CSV — add them per device afterward (same as below).

Required fields either way:
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

**JSON export/import** (Settings → Backup & restore) is a lighter-weight alternative to copying the `.db` file — useful for moving config between environments, or a quick config-only snapshot. "Export backup all" downloads sites (incl. regions), devices, interfaces, circuits, circuit roles, map views, and settings in one file. It does **not** include device credentials (SNMP community/keys, SSH password) — those never leave `crypto.py` in plaintext, so every device needs its credentials re-entered after an import. It also does not include user accounts or historical data (status history, utilization rollups) — those regenerate or stay as-is.

The same page also has per-object export/import (Sites, Devices, Circuits, Map views, Settings) — useful for sharing just one slice of config, or recovering part of the data if a future schema change breaks a full-backup import.

Import is a full replace, but only *of what you imported*: "Import backup all" wipes and reloads everything listed above; a per-object import only wipes/reloads that one object type; e.g. importing just Sites never touches Devices, even though devices reference a site. This can't be undone — export a fresh backup first if you want a way back. User accounts are left untouched either way, so you don't lock yourself out.

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

`seed-demo` builds 6 sites (including a passthrough waypoint and a minor
site), 7 devices, and a handful of circuits scripted to hit every status
color:

| What you'll see | Why |
|---|---|
| Chicago DC / Denver DC: yellow | Core bundle between them is fully up (critical), but an auxiliary office link is down — demonstrates auxiliary failures cap at yellow, never red |
| Austin DC: red | Single critical circuit to Chicago, down, no redundancy |
| Phoenix DC: blue, with a blue line to Denver | Device tagged unreachable — demonstrates both the whole-site-unreachable case and an unreachable circuit rendering on the map, not just an isolated site |
| Chicago Annex: green, parent (Chicago DC) is yellow | Minor site — its own circuit to Austin is up, so it reads green even though its parent is yellow, demonstrating that a minor site's status is its own, and that the parent cascade only forces red for a red/blue parent, never a merely-yellow one |
| CHI-DEN-Office-Backup circuit: gray | Tagged admin-down — excluded from status math entirely |
| Rocky Mountain Relay (passthrough site): "Circuits passing through" shows CHI-DEN-Core | The bundle is waypointed through it — demonstrates the map line bending through a passthrough site and that site's own transit list |
| Chicago Annex: "Circuits passing through" shows CHI-AUS-Core | CHI-AUS-Core is waypointed through Chicago Annex too — demonstrates a major/minor site's own transit list, not just a passthrough site's |
| Dashboard "Layer" dropdown shows "West Ops" | Austin DC's whole site/device/circuit trio is tagged to it — selecting it shows Austin plus everything untagged, hiding nothing else since there's only one layer defined; demonstrates the map-visibility filter (Settings -> Layers) without touching status/polling/alerting |
| Chicago DC / chi-core-01 / DEN-PHX-Core detail pages show a "Custom fields" section | One field per object type (Site Owner/Asset Tag/Client), one value each — demonstrates Settings -> Custom fields |
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

## 15. Production deployment

Sections 1–3 cover running the app in the foreground with Flask's built-in
dev server — fine for evaluating or developing, not for leaving up. This
section covers running it as a persistent service with a real WSGI server
behind TLS. Example config files referenced below live in `deploy/`.

### 15.1 Install gunicorn

Already pinned in `requirements.txt` — `pip install -r requirements.txt`
picks it up. No separate step needed if you've already run section 2.

### 15.2 The poller and worker count — read this before setting `--workers`

`SITEWATCH_RUN_POLLER=1` starts the APScheduler poll loop inside the app
process, once, at startup (`sitewatch/poller.py`). Gunicorn's `--workers N`
forks N independent copies of the app — each one runs its own poller loop.
With `--workers` > 1 every device gets polled N times per interval and
every down-alert fires N times to Google Chat.

There's no cross-process lock or leader-election to prevent this, so the
supported production setup is **`--workers 1`**, using `--threads` instead
for request concurrency. For an internal NMS at typical staff-facing
traffic levels, one worker with a handful of threads has no trouble
serving the UI and API concurrently — the poller loop itself doesn't block
request handling since it runs in Flask's app context, not inside a
request. If you outgrow this, the poller needs to be split into its own
process (a separate entrypoint that only calls `poll_all_devices()`,
scheduled independently of the web workers) — not implemented today.

Don't confuse this with **Max concurrent devices polled** (Settings page,
default 8) — that's a different, unrelated knob: within one poll cycle, on
one worker process, it controls how many devices get their SNMP data
fetched at the same time (a thread pool inside `poll_all_devices()`, not a
process). Raising it speeds up a poll cycle against many devices; it has
no relationship to `--workers`, which must still stay at 1.

SQLite is already configured for this (`sitewatch/extensions.py`: WAL
journal mode + a 30s busy timeout), so `--threads 4` serving requests
concurrently while the poller writes in the background isn't a concern —
a writer no longer blocks readers under WAL, and a second writer waits up
to 30s instead of failing immediately with "database is locked."

### 15.3 systemd service

```bash
sudo useradd --system --home /opt/sitewatch --shell /usr/sbin/nologin sitewatch
sudo mkdir -p /opt/sitewatch
# copy the project into /opt/sitewatch, or clone it there
cd /opt/sitewatch
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env      # fill in SECRET_KEY, ENCRYPTION_KEY, ADMIN_PASSWORD
flask --app app init-db
deactivate
sudo chown -R sitewatch:sitewatch /opt/sitewatch
sudo chmod 600 /opt/sitewatch/.env

sudo cp deploy/sitewatch.service.example /etc/systemd/system/sitewatch.service
sudo nano /etc/systemd/system/sitewatch.service   # confirm User/Group/WorkingDirectory match your install
sudo systemctl daemon-reload
sudo systemctl enable --now sitewatch
sudo systemctl status sitewatch
journalctl -u sitewatch -f                          # tail logs (access + error logs both go here)
```

`Restart=on-failure` in the unit means a crashed process comes back
automatically; it also means the service survives reboots via `enable`.

### 15.4 Reverse proxy + TLS

Gunicorn binds to `127.0.0.1:8000` only — nothing external talks to it
directly. Put nginx (or another reverse proxy) in front for TLS
termination and to be the thing actually exposed on the network. This
matters even for LAN-only access: without TLS, the login form posts the
admin password in plaintext.

```bash
sudo apt install nginx
sudo mkdir -p /etc/ssl/sitewatch
sudo openssl req -x509 -nodes -days 825 -newkey rsa:2048 \
  -keyout /etc/ssl/sitewatch/privkey.pem \
  -out /etc/ssl/sitewatch/fullchain.pem \
  -subj "/CN=sitewatch.internal"        # self-signed — fine for LAN-only; use a real CA cert if you have one

sudo cp deploy/nginx-sitewatch.conf.example /etc/nginx/sites-available/sitewatch
sudo nano /etc/nginx/sites-available/sitewatch   # set server_name to your host's LAN name/IP
sudo ln -s /etc/nginx/sites-available/sitewatch /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Browsers will warn on the self-signed cert (expected — there's no CA
vouching for it). Click through, or replace it with a cert from an
internal CA if your org has one.

### 15.5 Firewall

Only the reverse proxy needs to be reachable from the LAN — gunicorn's
`127.0.0.1:8000` already isn't. On the host:

```bash
sudo ufw allow 443/tcp
sudo ufw allow 80/tcp     # for the redirect to https
```

Don't open 8000 — that would let clients bypass TLS and talk to gunicorn directly.

### 15.6 Running this on WSL2 instead of a dedicated Linux host

If you're running the above inside WSL2 rather than a standalone VM
(e.g. as an interim setup before moving to real server), two WSL-specific
things apply on top of everything in 15.1–15.5:

- **systemd**: needs to be enabled explicitly — add `[boot] systemd=true`
  to `/etc/wsl.conf` inside the WSL distro, then from PowerShell run
  `wsl --shutdown` and reopen your WSL terminal. Requires a reasonably
  recent WSL version; check with `wsl --version` from PowerShell if
  `systemctl` isn't found after this.
- **LAN exposure**: WSL2's default networking NATs the distro behind the
  Windows host, so other LAN devices can't reach it directly even once
  nginx is listening. Two options, from PowerShell (admin):
  - **Mirrored networking** (simpler, needs Windows 11 22H2+ and a recent
    WSL): add `networkingMode=mirrored` under `[wsl2]` in `%UserProfile%\.wslconfig`,
    then `wsl --shutdown` and reopen. WSL then shares the host's network
    interface directly — no proxying needed, just a Windows Firewall rule
    for 443/80 if one doesn't already allow it.
  - **Port proxy** (works on older WSL): forward the port from the
    Windows host to WSL2's (changes-on-restart) IP:
    ```powershell
    wsl hostname -I                                            # get current WSL2 IP
    netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=443 connectaddress=<WSL2-IP> connectport=443
    netsh advfirewall firewall add rule name="SiteWatch HTTPS" dir=in action=allow protocol=TCP localport=443
    ```
    The WSL2 IP changes on every restart, so this `portproxy` command
    needs re-running each time unless scripted (e.g. a scheduled task
    that runs it at Windows startup) — mirrored mode avoids this entirely
    if your Windows version supports it.

### 15.7 Before calling it done

- [ ] `.env` has real `SECRET_KEY`/`ENCRYPTION_KEY`/`ADMIN_PASSWORD` values (not the ones from local dev), and is `chmod 600`
- [ ] Logged in once and confirmed the admin password works through the reverse proxy over HTTPS
- [ ] `journalctl -u sitewatch -f` shows poll cycles running (confirms `SITEWATCH_RUN_POLLER=1` took effect) — see section 16 if not
- [ ] `--workers 1` in the systemd unit (see 15.2 — don't "fix" this without splitting out the poller first)
- [ ] `instance/sitewatch.db` and `.env` are both included in whatever backup process you use (section 13); consider also keeping periodic JSON exports (Settings → Backup & restore) since those don't require stopping the service

---

## 16. Troubleshooting

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
