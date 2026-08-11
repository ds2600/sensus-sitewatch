# CLAUDE.md

Guidance for Claude Code when working in this repo.

## What this is

Sensus SiteWatch — internal NMS. Polls Cisco IOS-XE/IOS-XR and Juniper
Junos devices via SNMP, tracks circuits (point-to-point links, with
bundle/LAG parents grouping multiple point-to-point circuits), and shows
site/circuit status on a Leaflet map. Full spec and setup steps: README.md.

## Architecture, read in this order

1. `sitewatch/models.py` — schema. Read the module docstring-equivalent
   comments on `Circuit` and `CircuitRole` before changing status logic.
2. `sitewatch/status.py` — the status rollup rules. This is the core
   business logic of the app. Anything touching site or circuit color
   coding goes through `rollup_degree_status()` and `compute_site_status()`.
3. `sitewatch/poller.py` — the polling loop and the down-threshold debounce.
   Runs on APScheduler, one pass per `polling_interval_minutes` setting.
4. `sitewatch/telemetry.py` — the only module poller.py and the walk route
   call. Routes to `snmp.py` or `simulator.py` based on `SITEWATCH_SIMULATE`.
   Don't call either backend directly from outside this file.
5. `sitewatch/snmp.py` — pysnmp wrappers. Pinned to pysnmp 4.4.x hlapi
   (sync style). Upgrading pysnmp means rewriting this file for the async
   v6.x API — nothing else depends on pysnmp's internals directly. Note:
   pysnmp 4.4's transport needs the stdlib `asyncore` module, removed in
   Python 3.12 — `pyasyncore` in requirements.txt backports it. Also note
   `napalm` imports `pkg_resources`, which setuptools 81+ dropped — the
   `setuptools<81` pin in requirements.txt exists solely for that.
6. `sitewatch/simulator.py` — fake telemetry backend for testing without
   device access. Reads scenario tags (`[sim:down]`, etc.) out of an
   interface's alias field. `sitewatch/seed_demo.py` builds a demo
   topology using it — see README.md section 14.
7. `sitewatch/routes/` — one blueprint per resource (sites, devices,
   circuits, settings) plus `api.py` for the JSON the map/alerts JS consume.
8. `sitewatch/integrations/` — NetBox pull-sync and Google Chat webhook
   delivery. Both are intentionally isolated here so they can be swapped
   or extended without touching poller/status logic.

## Status model — the thing most likely to need care

Circuit states: `up`, `degraded` (bundles only), `down`, `admin_down`,
`unreachable`. Site colors: `green`, `yellow`, `red` — computed, never
stored, from `compute_site_status()`.

- Degree rule: 1 relevant circuit down = down. 2+ relevant circuits, some
  down = degraded. All down = down.
- `admin_down`/`unreachable` don't count toward the degree rule directly —
  see `rollup_degree_status()`'s `relevant` filter.
- Site status splits internal (intra-site device-to-device) vs external
  (inter-site) circuits, and critical vs auxiliary tier (via `CircuitRole`).
  Only a fully-down **critical external** circuit set can push a site to
  red. Everything else caps at yellow. Full truth table is in
  `compute_site_status()` — don't reimplement this logic elsewhere.

## Conventions

- No ORM enum types — status/role/vendor values are plain strings,
  validated in Python. Valid sets are the tuples at the top of `models.py`.
- Credentials (SNMP community/keys, SSH password) are never stored or
  passed around in plaintext outside `crypto.py`. Access them via the
  property accessors on `Device` (e.g. `device.snmp_community`), never the
  `_enc` columns directly.
- Server-rendered Jinja + Bootstrap 5 (CDN, no build step) + vanilla JS for
  the map (Leaflet) and alert polling. No frontend build tooling on
  purpose — keep it that way unless there's a real reason to add one.
- Settings are runtime-editable (`Setting` key/value table), not
  environment variables — except NetBox URL/token and the crypto/secret
  keys, which stay in `.env` since they're deployment-level, not
  app-behavior-level.

## Known gaps / not yet built

- No automated test suite exists yet — verification so far has been manual
  runs against simulate mode (see README section 14), not pytest or similar.
- Utilization rollup (`UtilizationRollup` model exists) has no scheduled
  job writing to it yet — poller.py updates live `last_in_bps`/`last_out_bps`
  on `Interface` but doesn't roll those into hourly/daily aggregates.
- NAPALM-based bundle/LAG auto-suggestion (manual confirm/reject) is not
  implemented — `napalm` is in requirements.txt for this but unused so far.
- Status history retention (`status_history_retention_days` setting) is
  stored but nothing prunes old `CircuitStatusHistory` rows yet.
- No automated migration tool (Alembic etc.) — schema changes currently
  mean editing `models.py` and re-running `flask init-db` against a fresh
  database, or hand-writing SQLite `ALTER TABLE` statements.
- A site going fully unreachable (blue) doesn't fire a distinct alert —
  only individual circuit down-transitions trigger `send_down_alert`.
  Worth deciding whether that's acceptable or needs its own alert path.
- Utilization display (device detail page) is a raw computed percentage
  with no smoothing — a single noisy poll can show a spike that a rollup
  average would have hidden. Cosmetic until rollups exist.

## Running locally

See README.md section 2–3. Short version:

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in SECRET_KEY, ENCRYPTION_KEY, ADMIN_PASSWORD
flask --app app init-db
SITEWATCH_RUN_POLLER=1 flask --app app run --host=0.0.0.0 --port=5000
```

`SITEWATCH_RUN_POLLER=1` starts the background poller. Omit it when just
poking at routes/templates without wanting live SNMP polling running.
