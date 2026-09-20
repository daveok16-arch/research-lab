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

Measured against live sources (2 pages each, Fort Worth plus Collin CAD):

| Metric | Result |
|---|---|
| Rows ingested | 4,000 permits |
| Commercial projects assembled | 1,201 |
| HIGH | 3 |
| MEDIUM | 124 |
| NEEDS_VERIFICATION | 1,074 |
| Projects with mechanical evidence | 7 (1 Tier-1, 6 Tier-2) |

A small HIGH count is the intended outcome, not a shortcoming. The classification gates are
designed so that a project is only called HIGH when a public record actually documents
mechanical scope *and* the project is commercial-scale and locatable. Under-claiming keeps
the list worth reading.

## Sources

| Source | Live? | Notes |
|---|---|---|
| Fort Worth Development Permits (ArcGIS) | Current | The only source that publishes a `Mechanical` permit type, giving Tier-1 HVAC evidence |
| Collin CAD Building Permits (Socrata) | Current to 2026 | Plano, Frisco, McKinney, Allen, Prosper, Celina and others. No trade permits |
| Dallas Permits FY2023-24 (ArcGIS) | Historical, ends 2023-12 | Supplementary |
| Dallas Building Permits (Socrata) | Stale, ends 2019-12 | Disabled by default |

### Honest coverage limits

- **Dallas is not currently covered.** Its public feeds stop in 2023 (GIS) and 2019
  (Socrata). Dallas is the largest city in the market, and the dashboard reports this gap
  per source rather than implying complete DFW coverage.
- **Tier-1 mechanical evidence is a Fort Worth capability.** Collin CAD does not publish
  trade permits, so mechanical scope there must come from permit text.
- **`architect` is almost always `Not verified.`** No free DFW source publishes the
  architect of record. The platform says so instead of guessing.
- **TDLR TABS was excluded** because it requires an account, and paid sources were excluded
  by design.

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
  normalize.py           commercial filtering and mechanical-evidence detection
  assemble.py            permit clustering into projects
  classify.py            HIGH / MEDIUM / NEEDS_VERIFICATION with reasons
  db.py                  SQLite schema and queries
  pipeline.py            orchestration of the stages above
  cli.py                 ingest / assemble / stats / projects
  connectors/            per-source fetch and normalize
tests/                   real code paths, no mocks
docs/DESIGN.md           full design for phases 1-4
```

## Status

- Phase 1 — data pipeline: **implemented and verified against live sources**
- Phase 2 — opportunity intelligence: **classification logic implemented**; the label, score,
  and reasons are persisted per project
- Phase 3 — dashboard: designed, not yet built
- Phase 4 — client report: designed, not yet built

## Compliance

All data is collected from public, no-authentication government sources. Records may be
incomplete or superseded. Confirm any figure with the issuing jurisdiction before commercial
reliance.