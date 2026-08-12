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
9. `sitewatch/backup.py` — JSON export/import (Settings → Backup & restore).
   `export_data(scope=...)`/`import_data(data, scope=...)` — `scope="all"`
   is the full backup (same file shape as always); `"sites"`, `"devices"`,
   `"circuits"`, `"regions"`, `"settings"` export/import one object type at
   a time, for sharing a subset or surviving a schema change that breaks
   only part of a full-backup import. A scoped import only wipes/reloads
   that scope's own table(s) — it never cascades to dependents (importing
   "sites" alone doesn't touch devices even though they reference
   site_id). Export never includes device credentials — see the module
   docstring. See README.md section 13.

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

- UI copy (labels, placeholders, form-text hints, flash messages, button
  text, tooltips) must be short — Simplified Technical English style:
  plain words, one clause where possible, state the fact/action and stop.
  No explaining the "why" in-line, no parenthetical asides, no restating
  what a nearby label already says. This does not apply to job_log.py
  output or Python logging — those are meant to be detailed/technical,
  since that's the point of the walk/repoll log modal and server logs.
- No ORM enum types — status/role/vendor values are plain strings,
  validated in Python. Valid sets are the tuples at the top of `models.py`.
- Credentials (SNMP community/keys, SSH password) are never stored or
  passed around in plaintext outside `crypto.py`. Access them via the
  property accessors on `Device` (e.g. `device.snmp_community`), never the
  `_enc` columns directly. This is also why `backup.py` excludes
  credentials entirely rather than exporting ciphertext or plaintext —
  keep it that way if the export format changes.
- Server-rendered Jinja + Bootstrap 5 (CDN, no build step) + vanilla JS for
  the map (Leaflet) and alert polling. No frontend build tooling on
  purpose — keep it that way unless there's a real reason to add one.
- Settings are runtime-editable (`Setting` key/value table), not
  environment variables — except NetBox URL/token and the crypto/secret
  keys, which stay in `.env` since they're deployment-level, not
  app-behavior-level.
- Delete routes (sites, devices, circuits, roles) all guard against
  orphaning data rather than relying on DB-level cascade: a site blocks
  deletion if it still has devices, a device blocks if its interfaces are
  wired into a circuit, a bundle circuit blocks if it still has members,
  a role blocks if any circuit still references it. Follow this pattern
  for any new delete route — SQLite FK enforcement isn't turned on here,
  so an unguarded delete leaves dangling foreign keys silently. This is
  for rows that *block* deletion of something else; for rows that are
  *owned by* the thing being deleted (e.g. `CircuitStatusHistory`,
  `AlertMute` belonging to a `Circuit`), use an ORM-level
  `cascade="all, delete-orphan"` relationship instead (see `Circuit`'s
  `status_history`/`alert_mute` relationships, or `Device.interfaces`) so
  deleting the parent actually cleans up its children rather than leaving
  them dangling.

## End every change with a pull note

The user runs this app on a separate machine from wherever Claude Code is
editing — they `git pull` to get changes, and re-running `flask --app app
init-db` unnecessarily means rebuilding/second-guessing their real
database for no reason. So: the last line of any response that changes
code must say exactly what to do after `git pull`, e.g.:

- `flask --app app init-db` — only if models.py added/changed a table or
  column (it's additive/safe, but only run it when actually needed)
- Nothing — say so explicitly ("No migration or restart needed") rather
  than omitting the line
- Anything else non-obvious: a new .env var, a new pip dependency, a
  process restart because a background thread's state won't pick up the
  change otherwise, etc.

Don't make the user infer this from the diff — state it plainly every time.

## Bump the version on every code change

`sitewatch/__init__.py`'s `__version__` (SemVer, shown in the page footer)
must be bumped as part of any response that changes code — same
don't-make-the-user-infer-it spirit as the pull note above. Pick the
level based on what actually shipped:

- **Patch** (`0.1.1` → `0.1.2`): bug fixes, visual/copy tweaks, doc fixes —
  nothing a user would call a "feature."
- **Minor** (`0.1.1` → `0.2.0`): a new feature or user-visible capability
  (a new page, a new import type, a new setting), even a small one. Also
  use minor for a breaking change while still pre-1.0 (`0.x`) — the app
  isn't API/schema-stable yet, so `1.0.0` is reserved for the first
  release considered "done," not for the first breaking change.
- **Major**: not until `1.0.0` exists. After that, only for a genuinely
  breaking change (e.g. a `backup.py` `VERSION` bump, a removed feature).

One bump per response covers everything shipped in it, at whichever
level the highest-impact change in that response requires — don't bump
separately per commit or per file. A response with no code change (a
question, a plan, pure research) doesn't need one.

## Schema changes must not cost the user their data

Two layers exist for this, in order of preference:

1. **`schema_sync.py`** — the first line of defense. Runs automatically on
   every app start (and explicitly via `flask init-db`): diffs each
   table's real columns against what `models.py` defines and
   `ALTER TABLE ADD COLUMN`s anything missing. Purely additive — new
   nullable columns, new tables — needs zero action from the user beyond
   `git pull` + restart. Prefer this path for any schema change that can
   be additive (which is almost all of them: a new nullable column, a new
   table). It cannot rename, drop, or retype an existing column — don't
   design a change that needs one of those without discussing it first.

2. **`backup.py`'s per-scope export/import** — the fallback for a change
   that genuinely can't be additive, and the mechanism for sharing a
   subset of config between environments. `export_data(scope=...)` /
   `import_data(data, scope=...)`: `scope="all"` is the full backup,
   `"sites"`/`"devices"`/`"circuits"`/`"regions"`/`"settings"` are one
   object type at a time. If a breaking change is ever unavoidable, the
   user should still be able to recover the *unaffected* scopes from an
   old full-backup export even if the affected scope's import fails.

Either way, the deal holds: any backup exported before your change must
still import cleanly after it (for `scope="all"`, that means the *old*
required sections — `sites`, `devices`, `interfaces`, `circuit_roles`,
`circuits`, `settings` — since a section added later, like `regions`, must
stay optional even in a full backup; see `_SCOPE_KEYS["all"]`). In
practice:

- New `Circuit`/`Device`/etc. columns must be nullable (or have a safe
  default) and read via `data[...].get("new_field")` in the relevant
  `_load_*()` function in `backup.py`, never `data[...]["new_field"]` — an
  old export simply won't have the key, and that must not be an error.
- Don't add a new section to `_SCOPE_KEYS["all"]` or bump `backup.VERSION`
  for an additive change — that's what breaks old exports on purpose.
  Only bump `VERSION` for a genuinely incompatible change, and even then
  prefer handling both shapes in the loader over breaking old files
  outright.
- When a change does add a schema field, say so in the end-of-response
  pull note (see below) — usually "nothing needed, schema self-heals on
  restart" now that `schema_sync.py` exists; only mention a manual
  backup/reimport step if the change genuinely isn't additive.

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
- The poller (`start_poller()`) runs in-process with the web app, started
  once per process on `SITEWATCH_RUN_POLLER=1`. There's no cross-process
  coordination, so production must run a single worker process (see
  README.md section 15.2) — scaling web workers means scaling poll
  cycles and duplicate alerts right along with them. Splitting the poller
  into its own process is the real fix if this app ever needs more than
  one worker; not implemented.

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

For running this as a persistent service instead (gunicorn + systemd +
reverse proxy), see README.md section 15 and the example configs in
`deploy/`.
