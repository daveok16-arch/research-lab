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

# Operations
flask --app oppintel.app.wsgi grant-admin EMAIL
flask --app oppintel.app.wsgi set-plan EMAIL PRO [--status ACTIVE|TRIALING|PAST_DUE|CANCELED]

# Tests
python -m pytest tests/ -q
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
* Schema lives in two strings in `db.py`: `SCHEMA` (intelligence) and `APP_SCHEMA`
  (application). `alert_event` is created by `_ensure_alert_event` so a legacy table can be
  rebuilt first. Column additions go through `_migrate_app_tables`.

## Testing conventions

* No mocks. Intelligence tests use real captured payloads; app tests use `tests/conftest_app.py`
  fixtures over a real database.
* `app_db` / `client` / `session_client` / `admin_client` run with protections off (debug).
* `secured_client` runs with CSRF and rate limiting **on** — use it for security tests.
* `csrf_from(client, path)` extracts a token as a browser form post would carry it.

## Gotcha list

* A dict key named `items` collides with `dict.items` in Jinja. Use another name
  (`pipeline.html` uses `entries`).
* `record_new_match` must commit; it is a public entry point outside the generation pass.
* `is_paid` excludes the operator plan deliberately.
* Session cookies only appear in `Set-Cookie` when the session changes.
* The application `data/oppintel.db` is git-ignored. Ingest before expecting data.
