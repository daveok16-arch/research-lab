# DFW Construction Opportunity Intelligence

Commercial construction intelligence for HVAC and mechanical contractors, built from public
construction and permit records.

**What it does:** discovers commercial construction projects across Dallas–Fort Worth that show
documented HVAC/mechanical activity, so a contractor's estimating team can find relevant work
without manually searching thousands of public permit records.

**What it does not claim:** that any project is an open bid, that every project has an available
HVAC package, or that a permit guarantees work. No source in this market publishes bid status,
so the product states the procurement status it can actually evidence and no more.

---

## Contents

- [Product overview](#product-overview)
- [Architecture](#architecture)
- [Data sources and coverage](#data-sources-and-coverage)
- [The evidence standard](#the-evidence-standard)
- [Local development](#local-development)
- [Running the application](#running-the-application)
- [Ingestion and report generation](#ingestion-and-report-generation)
- [The JSON API](#the-json-api)
- [Environment variables](#environment-variables)
- [Plans and entitlement](#plans-and-entitlement)
- [Security](#security)
- [Production deployment](#production-deployment)
- [Testing](#testing)
- [Adding a market or trade](#adding-a-market-or-trade)
- [Known limitations](#known-limitations)

---

## Product overview

Two layers, deliberately separated:

**Intelligence layer.** Collects public permit records, normalises them, assembles individual
permits into the underlying project, attaches per-field evidence, and classifies each project
against documented evidence. It never invents a value: a field a source does not publish stays
empty in the database and is rendered as `Not verified`.

**Application layer.** A web application that reads the intelligence data. It does not
re-implement any classification, eligibility or scoring rule — it calls the same modules the
report generator uses, so a page and a report can never disagree about a project.

### The web application

| Page | Purpose |
|---|---|
| `/` | Homepage: what the product does, with real counts from the database |
| `/dashboard` | The account's working centre: new matches, recently updated, watching, saved, pipeline, review |
| `/opportunities` | The discovery directory, with filters, search and pagination |
| `/opportunities/<slug>` | One opportunity: evidence, provenance, procurement status, sources, timeline, notes |
| `/markets`, `/markets/dfw`, `/markets/dfw/dallas` | Market and city landing pages |
| `/trades`, `/trades/commercial-hvac` | Trade landing pages |
| `/markets/dfw/commercial-hvac` | Market crossed with trade — the primary SEO page |
| `/project-types`, `/project-types/<type>` | Project-type landing pages, one per type the data actually holds |
| `/guides`, `/guides/<slug>` | Educational content on reading permit evidence |
| `/how-it-works` | The pipeline and what the product refuses to do |
| `/reports` | Published customer reports |
| `/watching` | Opportunities the account is monitoring for change |
| `/my-pipeline` | The account's own workflow stages over opportunities |
| `/alerts` | Event-driven alerts, each traceable to a recorded change |
| `/saved`, `/preferences` | Saved opportunities and personalization |
| `/signin`, `/signup` | Free account |
| `/sitemap.xml`, `/robots.txt` | Crawler control |
| `/api/*` | Read-mostly JSON API |
| `/admin/data` | Internal operations view. **Operator accounts only** (see below) |

`/my-pipeline` is deliberately not `/pipeline`: the reserved-path test that keeps ingestion off
the web surface reserves `/pipeline`, and the account's workflow route must not shadow it.

### What the account workspace does

The dashboard and the account views answer the five questions the product is built around — what
is happening, why it matters, what the evidence is, what changed, and what the customer can do
next. The mechanisms behind that:

* **Save vs. watch.** A save bookmarks a project. A watch asks the system to re-check the record
  for meaningful change and drives alerts. They are separate actions, separate tables, and
  separate intents.
* **Pipeline.** A user workflow stage (New, Reviewing, Watching, Target, Contacted, Pursuing,
  Closed out) is the account's own label for where it is with a project. It is *not* the
  project's procurement status. The vocabulary is kept disjoint from the procurement states on
  purpose, and a test asserts they never overlap.
* **Alerts are event-driven.** Every alert points at a recorded change (with a previous value, a
  current value and a source) or at the moment a project first matched the account's criteria.
  There is no scheduler that invents notifications. `/admin/data` reports the count of alerts
  that reference neither, which must be zero.
* **Change detection.** Each assembly pass diffs a project against the snapshot from the
  previous pass and records only real differences. A re-run with identical data produces
  nothing, so the timeline and the alerts stay a record of movement rather than of pipeline runs.
* **Personalization.** Match reasons are checkable facts — trade evidence on record, market,
  selected city and project type, value band, classification, recency — produced by one shared
  module so a card, a detail page and the API cannot disagree.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│ Application layer                                                   │
│   Flask routes ── OpportunityService ── templates ── JSON API       │
│   accounts · SEO · sitemap · reports                                │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ reads only; no intelligence logic
┌───────────────────────────────▼─────────────────────────────────────┐
│ Intelligence layer                                                  │
│   connectors → raw landing → normalize → assemble → evidence        │
│   → classify → eligibility → procurement → reports                  │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                    config/markets.yaml · config/trades.yaml
                    config/sources.yaml
```

### Module map

| Module | Responsibility |
|---|---|
| `connectors/` | Per-source fetch and normalize. DallasNOW/Accela, Fort Worth ArcGIS, Collin CAD |
| `pipeline.py` | Orchestrates collect → land → normalize → assemble → classify |
| `normalize.py` | Commercial filtering, boilerplate stripping, mechanical evidence detection |
| `assemble.py` | Clusters permits at one address into a project |
| `provenance.py` | The never-invent rule; the only path that sets a project field |
| `classify.py` | HIGH / MEDIUM / NEEDS_VERIFICATION with recorded reasons and four gates |
| `eligibility.py` | The seven customer-brief criteria |
| `grouping.py` | Building identity, sibling detection, duplicate statistics |
| `procurement.py` | The four procurement states |
| `discrepancy.py` | Cross-source contradiction detection |
| `report_generator.py` | Customer brief and internal research report |
| `service.py` | **The boundary.** Every web read goes through here |
| `changes.py` | Diffs a project against its last snapshot; records only real changes |
| `quality.py` | Observes data-quality problems without repairing or imputing them |
| `app/` | Flask application: routes, templates, static assets, SEO, accounts, API |
| `app/workflow.py` | The account's own record: watching, pipeline, notes, tags, activity |
| `app/alerts.py` | Turns recorded changes into per-user alerts. An alert cannot exist without an event |
| `app/entitlements.py` | Plan, subscription and entitlement, kept as three separate things |
| `app/matching.py` | Why an opportunity matches an account, as checkable facts |
| `app/security.py` | CSRF, rate limiting and response headers |
| `app/content.py` | Owned educational prose for the guide pages |

### Why the boundary matters

`OpportunityService` is the only module in the application layer that queries the intelligence
tables. No route, template or API handler writes SQL against `project`. This is asserted by
tests in `tests/test_app_architecture.py`, which fail if classification logic, a hardcoded
market name, or a literal evidence column appears in the application layer.

---

## Data sources and coverage

| City | Source | Live? | Notes |
|---|---|---|---|
| Dallas | DallasNOW / Accela | Current | Publishes `Commercial Mechanical Permit` as a first-class record type. No declared value or floor area |
| Fort Worth | City Development Permits (ArcGIS) | Current | Publishes a `Mechanical` permit type |
| Collin County | Collin CAD (Socrata) | Current | Plano, Frisco, McKinney and others. Publishes no trade permits, so no Tier-1 evidence |

The Dallas request mechanism is documented in [`docs/dallas_source.md`](docs/dallas_source.md),
including the windowed pagination behaviour and the `Origin`/`Referer` requirement.

Coverage is **not complete for DFW**. Three cities are ingested; the rest of the metroplex is
not. Three Dallas record types stop at a 300-page pagination safety cap, so their counts are
lower bounds. The coverage report marks these explicitly.

---

## The evidence standard

The product's value rests on being checkable, so these rules are enforced in code rather than
by convention.

**Never invent.** A project field can only be set through `provenance.assert_field`, which
simultaneously records the evidence that licenses the value. An unsourced fact cannot be created
by accident. Source sentinels such as `NULL` are never stored.

**Missing stays missing.** An unverified field is `NULL` in the database and renders as
`Not verified` in HTML, or `null` in JSON. The API returns `null` rather than the string, because
`"architect": "Not verified"` would assert that an architect by that name exists.

**Every material claim is citable.** Each project carries the source name, the record
identifier, the source's own date, and the excerpt that supports the value.

**Contradictions are preserved.** When two different publishers state different values for the
same field, both are retained and the field is flagged. Several permits from one publisher are
not a contradiction — that is normal scope.

**Procurement is never implied.** Four states exist, and `Confirmed open` is unreachable because
no source publishes bid status. Every customer-facing page states verbatim that permit evidence
does not confirm an available HVAC package.

**Completed work is not an opportunity.** A closed project is excluded from discovery. It stays
reachable by direct link, where its status is shown plainly.

**Trade evidence is required for discovery.** An HVAC directory lists only projects with
mechanical evidence. The requirement comes from `config/trades.yaml`, so a future trade declares
its own evidence.

---

## Why the classified HIGH count differs from the customer-visible HIGH count

Two different numbers describe "HIGH", and they are supposed to differ. This is the pipeline
working, not a discrepancy to reconcile away.

| Number | Where it comes from | Meaning |
|---|---|---|
| **13 HIGH** | `project.classification` in the intelligence database | Projects the classification gates judged HIGH on their evidence |
| **10 HIGH** | `/opportunities`, `/api/opportunities?classification=HIGH`, `/api/statistics` | Of those, the ones that are *currently discoverable* |

### Where the 3 go

The difference is the procurement-status filter, and nothing else. Of the 13 classified HIGH,
three have a source status recording the work as finished:

| Project | Recorded status |
|---|---|
| 7850 AVIATION PL | Final CO Issued |
| 2911 TURTLE CREEK BLVD, 600 | Closed - Complete |
| 3001 OLYMPUS BLVD, 130 | Closed - Complete |

A building whose certificate of occupancy has issued has already had its mechanical work
installed. Presenting it as a lead would be a false claim, so those three are excluded from
discovery — and the count is verified rather than assumed: the eligibility filter removes
**zero** of the 13, so the entire difference is the procurement filter.

They are not deleted. Each remains reachable at its own URL, where the page states the status
plainly and warns that the mechanical scope has already been let or completed. A customer who
follows an old link or a search result sees the truth rather than a 404.

### The full funnel

Every step is a filter with a stated purpose. No step changes a classification.

```
22,351 permit records              collected from three public sources
   ↓  assembled by address
3,996 projects                     permits grouped into the underlying project
   ↓  classification gates
   708 HIGH or MEDIUM               scored on documented evidence
   ↓  procurement filter            excludes Closed and Not verified
   509 with active procurement      work recorded as proceeding
   ↓  trade evidence filter         config/trades.yaml
   276 discoverable                 HVAC/mechanical evidence present
```

Where the 13 vs 10 difference sits:

```
    13 HIGH classified
   ↓  procurement filter
    10 HIGH discoverable            exposed by the customer application
```

### Why the filters live in the application layer

Classification answers *"how strong is the evidence?"* — a property of the record, computed
once and stored. Discovery answers *"is this worth showing a customer today?"* — a property of
the moment, which also depends on whether the work is still live and whether it matches the
trade being served.

Keeping them separate means the audit view (`reports/out/validation_report.md`, which lists all
13) and the customer view (which lists 10) can both be correct at the same time. Collapsing them
— by re-classifying closed work as something else, or by showing finished buildings so the
numbers match — would destroy the distinction the product depends on.

---

## Local development

```bash
git clone <repo> && cd research-lab
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

export PYTHONPATH=src
export SECRET_KEY=dev-only-not-for-production

# Create the intelligence schema and the application tables.
python -m oppintel.cli initdb
flask --app oppintel.app.wsgi init-app
python -m oppintel.app.wsgi   # or: flask --app oppintel.app.wsgi run
```

Then open <http://127.0.0.1:5000>.

A fresh database has no data, so the homepage shows a real empty state rather than placeholder
content. Run an ingestion (below) to populate it.

---

## Running the application

```bash
# Development server
PYTHONPATH=src python -m oppintel.app.wsgi

# Or with the Flask CLI
PYTHONPATH=src flask --app oppintel.app.wsgi run --debug

# Production WSGI server
pip install gunicorn
PYTHONPATH=src gunicorn --workers 2 --bind 0.0.0.0:8000 oppintel.app.wsgi:application
```

After any ingestion, rebuild the search index and slugs:

```bash
PYTHONPATH=src flask --app oppintel.app.wsgi build-search-index
```

---

## Ingestion and report generation

```bash
export PYTHONPATH=src

# List configured sources and their verified coverage
python -m oppintel.cli sources

# Ingest (omit --source for every enabled source)
python -m oppintel.cli ingest --source dallas_accela_permits --since 2026-01-01 --max-pages 300

# Assemble permits into projects and classify
python -m oppintel.cli assemble

# Coverage, data quality and validation reports
python -m oppintel.cli report

# Customer brief and internal research report
python -m oppintel.cli brief --limit 5 --prepared-for "Acme Mechanical"

# Rebuild the web search index and generate missing slugs
python -m oppintel.app.wsgi --help 2>/dev/null || flask --app oppintel.app.wsgi build-search-index
```

Reports are deterministic given the same database state: regenerating produces byte-identical
output.

---

## The JSON API

The API is the machine-readable form of the same `OpportunityService` the pages use, so a page
and an API response can never disagree about a project. A missing value is `null`, never the
string "Not verified" — the machine-readable document must not assert that a party named
"Not verified" exists.

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/opportunities` | GET | List/search. Same filters as the directory, plus `min_value`, `max_value`, `mechanical_only`, `freshness_days` |
| `/api/opportunities/<slug>` | GET | One opportunity, with sources, per-field verdicts, recorded changes and match reasons |
| `/api/markets`, `/api/trades` | GET | Configured markets and trades |
| `/api/statistics` | GET | Market, city and project-type counts. Every figure is a count of stored rows |
| `/api/saved` | GET | The account's saved opportunities. `401` when unauthenticated, never an empty list |
| `/api/saved/<id>` | POST/DELETE | Save or unsave |
| `/api/watching`, `/api/watching/<id>` | GET, POST/DELETE | The account's watched opportunities |
| `/api/pipeline`, `/api/pipeline/<id>` | GET, POST/DELETE | The account's workflow entries |
| `/api/notes/<id>` | GET/POST | The account's private notes on an opportunity |
| `/api/alerts`, `/api/alerts/<id>/read` | GET, POST | Event-driven alerts, with the change that caused each one |
| `/api/me` | GET | Identity and the resolved entitlement. Reports only what the stored subscription grants |

Account-scoped endpoints return `401` when there is no session, and every write is filtered by
the authenticated user id, so an id from another account resolves to nothing.

---

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SECRET_KEY` | **Yes in production** | random per process | Session signing. Without it, sessions do not survive a restart |
| `OPPINTEL_DB` | No | `data/oppintel.db` | Database path |
| `BASE_URL` | Yes for SEO | empty | Public origin for canonical URLs, Open Graph and the sitemap |
| `SESSION_COOKIE_SECURE` | No | on unless debug | Secure cookie flag |
| `FLASK_DEBUG` | No | `false` | Debug mode. Never enable in production |
| `CSRF_ENABLED` | No | on unless debug | CSRF, rate limiting and response headers |
| `FREE_VIEW_LIMIT` | No | `0` (disabled) | Distinct opportunities a free account may view |
| `LOG_LEVEL` | No | `INFO` | Log verbosity |
| `HOST`, `PORT` | No | `127.0.0.1`, `5000` | Development server bind address |

`CSRF_ENABLED` follows the same convention as the secure-cookie flag: it defaults on, and off
only when `FLASK_DEBUG` is set, so a real deployment is protected unless someone deliberately
disables it. The security tests construct a configuration with it on explicitly.

See `.env.example`.

---

## Plans and entitlement

Plans exist so commercial tiers do not require a redesign later. Three things are kept apart:

* A **plan** is a catalogue entry in `plan` (`FREE`, `PRO`, `TEAM`), seeded from
  `entitlements.py`. It holds no customer data.
* A **subscription** is an account's recorded relationship to a plan, with a status. Only an
  active or trialing status grants the plan's features, so a lapsed subscription loses access
  automatically rather than waiting for someone to revoke it.
* An **entitlement** is the resolved answer to "may this account use this feature", computed
  from the stored subscription plus the access level.

**Payment is not implemented, and nothing here pretends otherwise.** There is no checkout, no
invoice and no way to buy a plan. What is implemented is the property that keeps the product
honest: no web request can grant a plan. The only mechanism is the operator command, which is
also what an external billing integration would call once a subscription is real:

```bash
PYTHONPATH=src flask --app oppintel.app.wsgi set-plan estimator@example.com PRO
PYTHONPATH=src flask --app oppintel.app.wsgi set-plan estimator@example.com TEAM --status TRIALING
```

An account with no subscription is on `FREE` and holds no gated feature, and `/api/me` reports
exactly that. ADMIN is an internal operator level, not a paid tier: it grants every feature for
operations, and `is_paid` is deliberately false for it.

---

## Security

| Control | Implementation |
|---|---|
| CSRF | Session-bound token, constant-time compare, required on every unsafe method. A parameterised test asserts every POST form in the templates carries one |
| Rate limiting | Per-client sliding window; the tightest allowance is on `/signin` and `/signup` |
| Session | `HttpOnly`, `SameSite=Lax`, `Secure` outside debug; the cookie holds only the user id |
| Headers | `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`, and a same-origin CSP |
| Authorization | Anonymous ≠ FREE ≠ PRO/TEAM ≠ ADMIN. ADMIN is granted only by CLI |
| IDOR | Every account-scoped read and write is filtered by the authenticated user id; tests cover saves, notes and alerts across two accounts |
| Open redirect | `next` is accepted only when it is a same-site path |
| Password | PBKDF2-SHA256 with a per-password salt; never logged or echoed |
| Enumeration | Identical error for an unknown address and a wrong password |
| Error exposure | A 500 renders a generic page and records only the exception class, alongside the request path — no traceback, user id or body |

The rate limiter is per-process, so it is a defence in depth rather than the only one. The
documented production deployment puts a real limiter in front of the app.

---

## Production deployment

1. **Install and build.**
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt gunicorn
   ```

2. **Configure the environment.** Set at minimum `SECRET_KEY` and `BASE_URL`. Leave
   `FLASK_DEBUG` unset.

3. **Initialise the database.**
   ```bash
   PYTHONPATH=src python -m oppintel.cli initdb
   ```

4. **Ingest and build the index.**
   ```bash
   PYTHONPATH=src python -m oppintel.cli ingest
   PYTHONPATH=src python -m oppintel.cli assemble
   PYTHONPATH=src flask --app oppintel.app.wsgi init-app
   PYTHONPATH=src flask --app oppintel.app.wsgi build-search-index
   PYTHONPATH=src flask --app oppintel.app.wsgi monitor
   ```

   `init-app` creates the application tables, migrates any older application schema forward and
   seeds the plan catalogue. `monitor` raises alerts for changes detected during the assembly
   step, and is safe to re-run: a second call with no new data creates nothing. Change detection
   itself runs inside `assemble`, so monitoring is never a separate opportunity to miss. Also
   useful:

   ```bash
   PYTHONPATH=src flask --app oppintel.app.wsgi report-quality   # open data-quality findings
   ```

5. **Run under a WSGI server.**
   ```bash
   PYTHONPATH=src gunicorn --workers 2 --threads 4 --bind 0.0.0.0:8000 \
     --access-logfile - --error-logfile - oppintel.app.wsgi:application
   ```

   Terminate TLS and apply a shared rate limit at the proxy in front of this process. The
   in-process limiter is per worker, so behind several workers a hostile client gets several
   allowances; the proxy is where the single enforceable limit belongs.

6. **Create an operator account for `/admin/data`.** The page requires an account whose
   access level is `ADMIN`. Create the account through `/signup`, then promote it on the host
   that owns the database:

   ```bash
   PYTHONPATH=src flask --app oppintel.app.wsgi grant-admin ops@example.com
   ```

   An unauthenticated request is redirected to sign-in, and a signed-in non-operator receives
   `403`. The level is granted only by this command: there is no web route that can set it, so
   no request can escalate its own privileges. To remove access:

   ```bash
   PYTHONPATH=src flask --app oppintel.app.wsgi revoke-admin ops@example.com
   ```

   The page stays marked `noindex` and disallowed in `robots.txt` as a second layer.

**Schema migration.** The application tables are created with `IF NOT EXISTS` statements, so
`init-app` is additive and idempotent against a database that already holds ingested data. It
never alters or drops an intelligence table.

**Scheduled ingestion.** Run `ingest` then `assemble` then `build-search-index` on a daily cron.
A partial ingestion is safe: the pipeline commits every 250 permits, and re-running is
idempotent because raw records are keyed by content hash.

---

## Testing

```bash
PYTHONPATH=src python -m pytest tests/ -q      # 595 tests
```

| Suite | Covers |
|---|---|
| `test_app_directory.py` | Listing, filters, search, sorting, pagination, empty states |
| `test_app_detail.py` | Provenance, missing fields, procurement transparency, grouping, private-data separation |
| `test_app_accounts.py` | Signup, signin, password hashing, enumeration resistance, open redirect, saved opportunities |
| `test_app_seo.py` | Metadata, canonical URLs, noindex rules, sitemap, robots, structured data |
| `test_app_api.py` | API contract, null encoding, statistics, no internal leakage |
| `test_app_admin.py` | /admin/data access: anonymous, non-operator, operator, escalation, exposure |
| `test_app_architecture.py` | Market/trade are configuration; no intelligence logic duplicated |
| `test_changes.py` | Change detection: real differences recorded, no-op passes silent, before/after values |
| `test_workflow.py` | Watching, pipeline, notes, tags, assignment, per-account isolation, stage/procurement separation |
| `test_alerts.py` | Event-driven alerts, watch scoping, no-change-means-no-alert, alert integrity |
| `test_security.py` | CSRF, rate limiting, headers, privilege escalation, IDOR, open redirect, operator area |
| `test_entitlements.py` | Plan/subscription/entitlement separation, no self-service upgrade, lapsed access |
| `test_acceptance.py` | The full lifecycle from source to report against real records |
| `test_classify.py`, `test_normalize.py`, `test_provenance.py`, … | The intelligence engine |

No mocks are used. The intelligence tests run against real captured source payloads; the
application tests run against a real database and a real Flask app.

---

## Adding a market or trade

**A new market** is a `config/markets.yaml` entry plus whatever connectors its sources need.
Nothing in the application code references DFW.

```yaml
- id: houston
  slug: houston
  name: "Greater Houston"
  short_name: "Houston"
  state: TX
  active: true                 # flip to true to serve it
  trades: [commercial_hvac]
  cities:
    - {slug: houston, name: "Houston"}
  landing_pages:
    - {slug: houston, city: "Houston"}
  sources: [harris_county_permits]
```

Then add the connector, register it in `connectors/__init__.py`, and add its entry to
`config/sources.yaml`. Houston and Austin are already present as inactive placeholders, which is
why the architecture tests can prove a second market loads without code changes.

**A new trade** is a `config/trades.yaml` entry declaring its own evidence rule:

```yaml
trades:
  commercial_plumbing:
    label: "Commercial Plumbing"
    slug: commercial-plumbing
    active: true
    discovery:
      evidence_field: plumbing_evidence_tier   # the stored column that evidences this trade
      evidence_values: [1, 2]
      strong_evidence_value: 1
```

The application scopes discovery, statistics and reports from that declaration. The
classification gates stay in the intelligence layer and are unaffected.

---

## Known limitations

**Data**

- Coverage is not complete for DFW. Dallas, Fort Worth and Collin County are ingested; other
  cities are not.
- Three Dallas record types stop at the 300-page pagination safety cap, so their totals are
  lower bounds. The coverage report marks these.
- Dallas publishes no declared project value or floor area, which limits how many Dallas
  projects can establish significance.
- `architect` and `developer` are published by no free source in this market and remain
  `Not verified` on effectively every record.
- No source publishes bid status, so `Confirmed open` is unreachable.

**Product**

- Payment is not implemented, and no checkout exists. `FREE`, `PRO` and `TEAM` plans and the
  entitlement model are implemented so monetization does not require redesigning authorization,
  but a plan is granted only by the `set-plan` operator command. `/api/me` reports the truth:
  an account with no stored subscription holds no gated feature.
- Email alerts are not implemented. Alerts are in-app and event-driven. The schema carries
  `email_sent_at` and the preference exists, so a mailer would read the same event rows, but
  nothing sends mail today.
- Team features are architecturally present but partial. Organization membership, peer listing
  and assignment to a peer are implemented and isolated by organization. Organization
  administration (creating an org, inviting a member) is not exposed on the web, so an
  organization must be created directly in the database for now.
- `/admin/data` requires an `ADMIN` account and is unreachable without one. There is no
  self-service way to grant the level; it is set by CLI on the host.
- Suite-level records are grouped and flagged but never merged, so project counts exceed building
  counts.
- The rate limiter is per-process. Behind more than one worker each process counts separately,
  so the effective limit scales with the worker count. A shared limiter belongs in front of the
  app (see the deployment notes).
- Change detection compares the project's tracked fields and its permit set. A change to a
  source record that does not alter any of those — for example a corrected excerpt that leaves
  the value identical — is not surfaced as an event, because nothing a customer acts on changed.

---

## Compliance

All data is collected from public, no-authentication government sources. Records may be
incomplete or superseded. Permit evidence does not confirm that a project is currently accepting
bids for any trade. Confirm all figures with the issuing jurisdiction before commercial reliance.
