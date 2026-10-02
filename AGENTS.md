# AGENTS.md — repository guide

Persistent notes for working in this repository. Read this before making changes.

## What this is

DFW commercial construction opportunity intelligence for HVAC/mechanical contractors. Two
layers, deliberately separated:

* **Intelligence layer** (`src/oppintel/*.py`): connectors → normalize → assemble → evidence →
  classify → eligibility → procurement → changes → quality.
* **Application layer** (`src/oppintel/app/`): a Flask site that *reads* the intelligence data
  through `OpportunityService`. It re-implements nothing.

## Commands

```bash
export PYTHONPATH=src
export SECRET_KEY=dev-only-not-for-production

# Database and schema
python -m oppintel.cli initdb                 # intelligence schema + sources
flask --app oppintel.app.wsgi init-app        # application schema + migrations + plans

# Ingestion
python -m oppintel.cli ingest [--max-pages N] [--source ID]
python -m oppintel.cli assemble
flask --app oppintel.app.wsgi build-search-index
flask --app oppintel.app.wsgi monitor         # raise alerts from detected changes

# Inspect
python -m oppintel.cli stats
python -m oppintel.cli projects [--classification HIGH]
flask --app oppintel.app.wsgi report-quality
flask --app oppintel.app.wsgi seo-report            # SEO audit; --json for machine output

# Operations
flask --app oppintel.app.wsgi grant-admin EMAIL
flask --app oppintel.app.wsgi set-plan EMAIL PRO [--status ACTIVE|TRIALING|PAST_DUE|CANCELED]

# Tests
python -m pytest tests/ -q

# Run the app and automate the online-search pipeline (no cron/systemd here)
ops/start.sh                 # gunicorn on 0.0.0.0:12000 + scheduled refresh
ops/stop.sh
PYTHONPATH=src python ops/automate.py --once --max-pages 2   # one bounded refresh
```

## Non-negotiable rules

These are enforced by tests, not by convention. Breaking one fails the suite.

1. **Never invent a value.** A missing field stays `None` in the database and renders as
   "Not verified" (HTML) or `null` (JSON). Never the string "Not verified" in JSON.
2. **The application layer holds no intelligence logic.** No classification, scoring, evidence
   field names, or `FROM permit` queries in `app/`. `tests/test_app_architecture.py` asserts it.
3. **No hardcoded market or trade.** Resolve from `config/markets.yaml` and `config/trades.yaml`.
   The evidence field name comes from `TradeConfig.discovery`.
4. **Relevance is not procurement.** A project relevant to HVAC is not an open bid. Only a
   source stating bid language yields `Confirmed open`, which today never happens.
5. **User workflow is not source status.** Pipeline stages are the account's labels, kept
   disjoint from `procurement.py` states. A test asserts the vocabularies do not overlap.
6. **Every alert has an underlying event.** An alert points at a `project_change` row or a
   first-match event. `AlertService.alerts_without_event()` must stay empty.
7. **Change detection records real differences only.** A no-op assembly pass emits nothing.
8. **No web route grants privilege.** ADMIN only via CLI; plans only via `set-plan`.
9. **Ingestion stays off the web surface.** `/pipeline` is a reserved path in the admin tests;
   the account workflow route is `/my-pipeline`.
10. **Preserve the intelligence engine.** Do not simplify classification, provenance or
    eligibility to make the web layer easier.

## Layout notes

* `service.py` — the only module in the app layer that queries intelligence tables. Add a new
  read here, not in a route.
* `changes.py` / `quality.py` — intelligence layer. `changes.py` is diffed inside
  `Pipeline.assemble_and_classify`, which is why monitoring cannot be skipped.
* `app/workflow.py` — watching, pipeline, notes, tags, activity, org peers. Per-user rows only.
* `app/alerts.py` — event-driven, in-app. Email is modelled (`email_sent_at`) but not sent.
* `app/entitlements.py` — plan / subscription / entitlement. No payment code.
* `app/security.py` — CSRF, rate limiting, headers. Installed in `create_app`.
* `app/seo_gate.py` — programmatic-page quality gate. Thresholds live in `config/keywords.yaml`,
  not in code. The sitemap re-evaluates the same gate so the two signals agree.
* `app/seo_report.py` — the SEO audit. Computed from the map and the database; reports no ranking.
* `app/analytics_funnel.py` — landing events. Records a page *kind*, never a URL or identity.
* `config/keywords.yaml` — keyword-to-page map. One primary keyword per page (asserted by test).
  Curated `landing_pages` cities are indexable; city x trade combinations are gated.
* Schema lives in two strings in `db.py`: `SCHEMA` (intelligence) and `APP_SCHEMA`
  (application). `alert_event` is created by `_ensure_alert_event` so a legacy table can be
  rebuilt first. Column additions go through `_migrate_app_tables`.

## Testing conventions

* No mocks. Intelligence tests use real captured payloads; app tests use `tests/conftest_app.py`
  fixtures over a real database.
* `app_db` / `client` / `session_client` / `admin_client` run with protections off (debug).
* `secured_client` runs with CSRF and rate limiting **on** — use it for security tests.
* `csrf_from(client, path)` extracts a token as a browser form post would carry it.

## Automation

* `ops/automate.py` is the only automation entry point. It supervises gunicorn **and** runs
  the refresh pipeline on a separate thread, so a long `ingest` never blocks server restart.
* This environment has no cron or systemd (PID 1 is `openhands-agent`). Do not add a crontab
  or unit file; schedule in-process instead.
* Connectors order newest-first and default to 200 pages/source. An unbounded `ingest`
  (Fort Worth ArcGIS alone) is 200k+ records and takes >13 min, so a recurring refresh is
  bounded (`MAX_PAGES`, default 3); use `--full` only for an initial backfill.
* `ops/start.sh` redirects the daemon's stdout to `data/automation.out`, **not**
  `data/automation.log`: the daemon owns that log file itself and a second writer interleaves
  and truncates lines.
* Runtime state is git-ignored: `data/*.log`, `data/*.out`, `data/*.pid`,
  `data/automation_state.json`.

## No static publishing

* The site is served by the running app, not published as a static mirror. There is no GitHub
  Pages workflow and no static export step: `render.yaml` is the deployment.
* The site must not be frozen into a separate build. The public pages read assembled projects
  through `OpportunityService`, so a snapshot would be a second copy of the data to keep current
  — and a stale one. Serving from the app keeps one source of truth.
* `robots.txt` and `sitemap.xml` are generated by `app/seo.py` from the app's own route map, so
  they describe the live site with no extra tooling.

## Render deployment

* `render.yaml` is the blueprint for the persistent, always-on deployment. It runs the app and
  its refresh loop in one service — no separate cron, worker or scheduler.
* **A disk is required.** SQLite is a file and the assembled dataset is the app's value, and
  Render's container filesystem is ephemeral. The disk mounts at `/var/data` and
  `OPPINTEL_DATA_DIR` points at it. Without the disk, every deploy starts from an empty database.
* **One instance only.** A Render disk attaches to a single instance, and a second instance
  would run a second refresh loop against the same file. Scaling out means moving the database to
  a networked store first, not adding instances.
* `ops/start.sh` has two modes. Locally it backgrounds the daemon and writes a PID file; on a
  container host (`$RENDER`, or `FOREGROUND=1`) it `exec`s the supervisor in the foreground and
  binds the platform's `$PORT`. Render requires the foreground mode — a backgrounded process
  looks like a crashed service.
* `/healthz` is the platform's health check. It returns 503 **only** when the database is
  unreachable. An empty database is `200` with `"status": "empty"` on purpose: a first deploy
  starts empty and the refresh loop fills it, so a 503 there would only cause a restart loop.
* `OPPINTEL_DB` is read by the CLI (`oppintel.cli.DEFAULT_DB`), so the pipeline honours a mounted
  disk without every command repeating `--db`. The web layer reads `OPPINTEL_DB` through
  `AppConfig.database_path`. Point both at the same file.
* `/healthz` is operational, not content: it is in the robots disallow list (`app/seo.py`) and
  marked `noindex`. Add any new operational route there, not to a sitemap.

## Gotcha list

* A dict key named `items` collides with `dict.items` in Jinja. Use another name
  (`pipeline.html` uses `entries`).
* `record_new_match` must commit; it is a public entry point outside the generation pass.
* `is_paid` excludes the operator plan deliberately.
* Session cookies only appear in `Set-Cookie` when the session changes.
* The application `data/oppintel.db` is git-ignored. Ingest before expecting data.
