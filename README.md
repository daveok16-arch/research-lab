# DFW Construction Opportunity Intelligence — MVP

Discovers newly issued and active commercial construction projects in Dallas–Fort Worth
that may create HVAC/mechanical contracting opportunities, for commercial HVAC contractors.

This repository contains **Phase 1**: a working research and data pipeline that collects
public construction and permit information, resolves it into project records, and attaches
source evidence to every fact it reports.

Design for all phases is in [`docs/DESIGN.md`](docs/DESIGN.md).

## The one rule that shapes everything

> Never invent, infer, or hallucinate missing project information. If something cannot be
> verified, write **"Not verified."**

This is enforced structurally, not by convention:

- A project field can only be set through `provenance.assert_field`, which simultaneously
  records the `Evidence` row that licenses the value. An unsourced fact cannot be created
  by accident.
- Blank values and source sentinels (`NULL`, `N/A`, `-`) are never stored.
- In the database an unverified field is `NULL`; the string `"Not verified."` is applied at
  render time, so the sentinel can never be mistaken for real data.

## Quick start

```bash
pip install -r requirements.txt      # or: pip install requests PyYAML

export PYTHONPATH=src

python -m oppintel.cli initdb
python -m oppintel.cli sources
python -m oppintel.cli ingest --max-pages 2      # live fetch, a few thousand rows
python -m oppintel.cli stats
python -m oppintel.cli projects --limit 20
python -m oppintel.cli projects --classification HIGH
```

Tests:

```bash
PYTHONPATH=src python -m pytest tests/ -q
```

## What it currently finds

Measured against live sources (Dallas via Accela, Fort Worth, Collin CAD):

| Metric | Result |
|---|---|
| Permits ingested | 21,937 |
| Commercial projects assembled | 3,940 |
| HIGH | 13 |
| MEDIUM | 691 |
| NEEDS_VERIFICATION | 3,236 |
| Projects with mechanical evidence | 367 |
| Dallas records | 18,035 |
| Dallas projects | 2,734 |
| Dallas records with mechanical evidence | 2,446 |
| Dallas coverage | 2026-01-01 → 2026-09-21 |

A small HIGH count is the intended outcome, not a shortcoming. Four gates see to that: a
project is only called HIGH when a public record documents mechanical scope, the project is
commercial-scale, its address is precise enough to act on, and something establishes that it
is significant. Under-claiming keeps the list worth reading.

## Reports

```bash
python -m oppintel.cli brief --limit 5 --prepared-for "Acme Mechanical"  # customer + internal
python -m oppintel.cli report                                            # coverage, quality, validation
```

Both are deterministic given the same database state, so a report can be regenerated and
will be identical.

| Output | Purpose |
|---|---|
| `reports/out/customer_brief.md` | The product. 5 opportunities, business language, one citation per fact |
| `reports/out/internal_report.md` | Full audit trail: field verdicts, classification reasoning, every permit, provenance |
| `reports/out/coverage_report.md` | Per-source coverage, dates, caps, lower bounds |
| `reports/out/data_quality_report.md` | Record and project counts, opportunity states, grouping and procurement distribution |
| `reports/out/validation_report.md` | Per-project field validation with CONFIRMED / PARTIALLY VERIFIED / NOT VERIFIED |

### Procurement status

No source in this market publishes bid status, so no opportunity is ever presented as an
open bid. Four values:

| Value | Meaning |
|---|---|
| `Confirmed open` | a source explicitly advertises the work for bid (`Confirmed open` is currently unreachable) |
| `Evidence found, status unclear` | active work on record; procurement not stated |
| `Not verified` | nothing supports a procurement claim |
| `Closed` | finished, expired or withdrawn — excluded from customer output |

Every customer brief states verbatim: *"Public permit evidence does not by itself confirm
that the HVAC/mechanical package is currently available for bid."*

### Selection and eligibility

An opportunity reaches a customer brief only if it is not completed, has tier-1 or tier-2
mechanical evidence, is classified HIGH or MEDIUM, is not service/repair/maintenance work,
and states at least one significance fact (building class, declared value or footprint).
Confirmed open bidding is deliberately **not** required, because no source can establish it.

Selection is deterministic and explainable: ordered by classification score, then label, then
amount of usable context, then recency, then id. The reason each opportunity was included is
stored and printed.

Where several records share a base street address they are treated as **possible** suites of
one building. They are never merged — the relationship is flagged as uncertain and selection
prefers one project per building so a brief cannot show five suites of one tower as five
independent opportunities.

## Sources

| Source | Live? | Notes |
|---|---|---|
| **DallasNow / Accela** (`aca-prod.accela.com/DALLASTX`) | **Current** | The real current Dallas system. Publishes `Commercial Mechanical Permit` as a first-class type. No declared value or floor area. Mechanism documented in `docs/dallas_source.md` |
| Fort Worth Development Permits (ArcGIS) | Current | Publishes a `Mechanical` permit type, giving Tier-1 HVAC evidence |
| Collin CAD Building Permits (Socrata) | Current to 2026 | Plano, Frisco, McKinney, Allen, Prosper, Celina and others. No trade permits, so no Tier-1 evidence |
| Dallas Permits FY2023-24 (ArcGIS) | Historical, ends 2023-12 | Disabled |
| Dallas Building Permits (Socrata) | Stale, ends 2019-12 | Disabled |

### Closing the Dallas gap

Dallas was previously an explicit gap: the open-data extract stops on 2019-12-31 and the
GIS layer on 2023-12-29. The current system is **DallasNow**, an Accela Citizen Access
portal. It is public, login-free, and returns records dated the same day.

Reaching it required three things, all documented and reproducible in
[`docs/dallas_source.md`](docs/dallas_source.md):

1. a session-scoped `__VIEWSTATE` taken from a `GET` in the same session,
2. the session cookies, and
3. `Origin` and `Referer` headers — the non-obvious requirement. Without them an otherwise
   valid request returns an error page.

The portal's "Download results" control was tested and returns re-rendered HTML, not a file,
so the connector paginates the result grid instead.

### Honest coverage limits

- **Dallas publishes no value or floor area.** Those fields read `Not verified.` for Dallas
  projects, which limits how many can reach HIGH.
- **Tier-1 mechanical evidence exists only where a city publishes trade permits**, which is
  Dallas and Fort Worth. Collin CAD cannot supply it.
- **`architect` is almost always `Not verified.`** No free DFW source publishes the architect
  of record.
- **TDLR TABS and the Dallas GIS open data portal were excluded** because both require an
  account, and paid sources were excluded by design.

## Architecture

```
connectors  ->  raw landing  ->  normalize  ->  assemble  ->  classify  ->  SQLite
 (per source)    (verbatim)      (permit)      (project)    (opportunity)
```

- **Raw landing** writes every response verbatim to `data/raw/` with a content hash before
  parsing, so a bug fix can be replayed without re-hitting a government server.
- **Assembly** clusters permits by normalized address within a rolling window. A project
  must contain at least one permit describing construction work, which stops standalone
  trade service calls from becoming "opportunities".
- **Classification** is additive and explained: every point awarded appends a readable
  reason to `classification_reasons`, so no label is a black box.

### Modularity

Adding a city means adding a `config/sources.yaml` entry and a connector class, then
registering it in `connectors/__init__.py`. Adding a trade means adding a profile to
`config/trades.yaml`. The core domain, pipeline, and dashboard are unchanged.

## Layout

```
config/sources.yaml      source registry with verified coverage metadata
config/trades.yaml       trade profile: keywords, weights, thresholds, gates
src/oppintel/
  models.py              Permit, Project, Evidence, ProjectParty + parsers
  provenance.py          the not-verified rule; assert_field is the only way to set a fact
  normalize.py           commercial filtering, boilerplate stripping, mechanical detection
  assemble.py            permit clustering into projects
  classify.py            HIGH / MEDIUM / NEEDS_VERIFICATION with four gates
  discrepancy.py         contradictory values across sources: both kept, never merged
  reporting.py           coverage and field-level validation reports
  db.py                  SQLite schema and queries
  pipeline.py            orchestration of the stages above
  cli.py                 ingest / assemble / stats / projects
  connectors/            per-source fetch and normalize
tests/                   124 tests, real code paths, no mocks
docs/DESIGN.md           full design for phases 1-4
docs/dallas_source.md    verified Dallas request mechanism
reports/out/             generated coverage and validation reports
```

## Status

- Phase 1 — data pipeline: **implemented and verified against live sources**
- Phase 2 — opportunity intelligence: **classification logic implemented**; the label, score,
  and reasons are persisted per project
- Phase 3 — dashboard: designed, not yet built
- Phase 4 — client report: designed, not yet built

## Known limitations

- **Partial coverage.** No single source covers DFW completely. Dallas, Fort Worth and
  Collin County are covered; other DFW cities are not.
- **Lower-bound counts.** Three Dallas record types stop at a 300-page pagination safety cap,
  so their totals are lower bounds. The coverage report marks these explicitly.
- **Dallas publishes no value or floor area**, which limits how many Dallas projects can
  establish significance.
- **No bid status.** No configured source publishes it, so confirmation that a package is
  open must come from the owner or GC directly.
- **Architect and developer are unpublished** by every free source in this market and remain
  Not verified on effectively every record.
- **Suite-level records.** Many Dallas suites share one building. They are grouped and
  flagged, never merged, so counts of projects exceed counts of buildings.

## Testing

```bash
PYTHONPATH=src python -m pytest tests/ -q      # 303 tests, no mocks
```

Notable suites:

| File | Covers |
|---|---|
| `test_dallas_accela.py` | Request construction, same-session viewstate, retries, URL capture |
| `test_dallas_pagination.py` | Windowed pager, Next-anchor behaviour, safety cap |
| `test_dallas_probe_matrix.py` | The required classification matrix (mechanical vs plumbing etc.) |
| `test_grouping.py` | Suite-level grouping, no merging, uncertain relationships |
| `test_eligibility.py` | Customer-brief eligibility criteria |
| `test_brief_sanity.py` | The report sanity checks as automated assertions |
| `test_procurement.py` | Procurement vocabulary, closed vs unknown |
| `test_provenance.py` | The never-invent rule and per-field evidence |

## Compliance

All data is collected from public, no-authentication government sources. Records may be
incomplete or superseded. Confirm any figure with the issuing jurisdiction before commercial
reliance.