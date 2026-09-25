# DFW Construction Opportunity Intelligence — MVP Design

Status: design baseline for MVP. Phase 1 is implemented against this document.

---

## 1. Recommended architecture

### 1.1 Guiding constraints

The MVP must be cheap, honest, and modular. Those three words drive every decision below.

- **Cheap**: only free, public, no-auth data sources. No paid feeds, no scraping behind logins.
- **Honest**: the system may never present a guess as a fact. An unverified field is stored
  as `NULL` and rendered as `Not verified.` Provenance is stored per field, not per record.
- **Modular**: a new city is a new connector plus a mapping file. A new trade is a new
  classification profile. Neither should require touching the core.

### 1.2 Layers

```
                    +-------------------------------------+
                    |  Presentation                       |
                    |  Flask dashboard (Phase 3)          |
                    |  Report generator  (Phase 4)        |
                    +------------------+------------------+
                                       |
                    +------------------v------------------+
                    |  Intelligence                       |
                    |  classification (Phase 2)           |
                    |  scoring / relevance rules          |
                    +------------------+------------------+
                                       |
                    +------------------v------------------+
                    |  Domain model                       |
                    |  Project, Permit, Evidence, Party   |
                    |  Provenance (per-field)             |
                    +------------------+------------------+
                                       |
                    +------------------v------------------+
                    |  Ingestion pipeline                 |
                    |  fetch -> raw landing -> normalize  |
                    |  -> assemble -> upsert -> verify    |
                    +------------------+------------------+
                                       |
                    +------------------v------------------+
                    |  Connectors (pluggable)             |
                    |  fort_worth_permits                 |
                    |  collin_cad_permits                 |
                    |  dallas_permits                     |
                    +-------------------------------------+
```

Each layer depends only on the layer beneath it. The pipeline never talks to an HTTP API
directly; it calls a connector. The dashboard never talks to a connector; it reads the
database through a repository.

### 1.3 Why SQLite

The MVP handles tens of thousands of rows, one writer, and a read-only dashboard. SQLite
gives us ACID, a zero-ops single file, and full SQL for filtering. The schema avoids
SQLite-only constructs so a move to PostgreSQL later is a driver change, not a rewrite.

### 1.4 Why a raw landing zone

Every connector response is written verbatim to `data/raw/` with a content hash before any
parsing happens. This gives us three things a live-only pipeline cannot:

1. **Replay** — we can rebuild the database without re-hitting a government server.
2. **Audit** — the exact bytes behind a given fact remain inspectable.
3. **Rate-limit safety** — a parsing bug never triggers a re-crawl.

### 1.5 Configuration over code

Sources, trade profiles, and classification weights live in `config/*.yaml`. Adding Plano
or adding "electrical contracting" is an edit to a YAML file plus, if the source is genuinely
new, a small connector class.

---

## 2. Data model / schema

### 2.1 The core problem this schema solves

The 20 requested fields do not exist in one place. A city permit gives us address and
permit date but rarely the architect. The architect appears on a separate filing. The
general contractor is sometimes on the permit, sometimes not. Any honest system must
therefore model **facts and their provenance**, not a flat row of columns.

Two rules fall out of that:

- **Rule A (no invention).** A field is populated only from a source value that maps to it.
  No defaults, no inference, no "most likely" filling. Missing means `NULL` in the database.
- **Rule B (per-field provenance).** Every populated field carries the source that
  supplied it, the URL, and when we observed it.

### 2.2 Tables

**`source`** — one row per public data source. Records `name`, `publisher`, `base_url`,
`kind` (`socrata`, `arcgis`, `html`), `reliability`, and `notes`.

**`ingest_run`** — one row per connector execution. Records source, start/finish, status,
row counts, and any error. Makes silent failures visible.

**`raw_record`** — the verbatim landing row. `source_id`, `run_id`, `natural_key`,
`payload` (JSON text), `payload_hash`, `fetched_at`. Unique on `(source_id, natural_key,
payload_hash)` so re-running a crawl is idempotent.

**`permit`** — a normalized permit row: permit number, type, subtype, issued date, status,
job value, square footage, address parts, owner, contractor, plus `source_id` and
`source_url`. This is the immovable, directly-observed evidence.

**`project`** — the assembled opportunity. This is what the customer sees. It holds the 20
requested fields, `classification`, `classification_reasons`, and `last_verified`.

**`project_permit`** — many-to-many link between projects and permits. A project is usually
assembled from several permits (building + mechanical + electrical).

**`evidence`** — the heart of the honesty requirement. One row per
`(project_id, field_name)` that we can support:

| column | meaning |
|---|---|
| `field_name` | which project field this supports |
| `value` | the value we are asserting |
| `source_id` | who told us |
| `source_url` | where exactly |
| `source_record_key` | the natural key of the source row |
| `source_date` | the date the source itself carries |
| `observed_at` | when we read it |
| `evidence_type` | `permit`, `permit_scope`, `party_role`, `derived` |
| `excerpt` | the literal text that supports the value |

**`project_classification`** — append-only history of classification decisions, with score,
label, and the list of reasons. Re-classification never erases a prior decision.

**`party`** and **`project_party`** — owners, developers, general contractors, architects,
and mechanical contractors as first-class entities with a role on a project. Kept separate
because "which architect works with which GC" is a durable asset worth building.

### 2.3 Field-to-source map

Which of the 20 requested fields is realistically obtainable from a free public source:

| Field | Free-source obtainability | Typical origin |
|---|---|---|
| `project_name` | Sometimes | permit work description, sub-plat name |
| `address` | Yes | permit situs address |
| `city` | Yes | permit issuer / situs city |
| `state` | Yes | constant `TX` for this market |
| `project_type` | Derived | permit type + land use text |
| `estimated_project_value` | Sometimes | permit job value |
| `square_footage` | Sometimes | permit building area |
| `permit_number` | Yes | permit number |
| `permit_date` | Yes | permit issue date |
| `project_status` | Sometimes | permit current status |
| `owner` | Sometimes | ArcGIS `Owner_Full_Name`, CAD owner |
| `developer` | Rarely | sub-plat / applicant text |
| `general_contractor` | Sometimes | permit contractor field |
| `architect` | Rarely | not on most permit feeds |
| `mechanical_hvac_evidence` | Sometimes | mechanical permit rows, scope text |
| `source_name` | Yes | connector |
| `source_url` | Yes | connector |
| `source_date` | Yes | connector |
| `last_verified` | Yes | ingestion timestamp |

`developer` and `architect` will frequently be `Not verified.` That is the correct
behaviour, and the dashboard surfaces them as such rather than guessing.

### 2.4 The mechanical/HVAC evidence chain

This is the product's core differentiator, so it is modeled explicitly rather than as a
boolean. Evidence comes from three ranked tiers:

1. **Tier 1 — mechanical permit link.** A permit at the same address whose type contains
   `Mechanical`, `HVAC`, or `Refrigeration`. This is the strongest possible signal: the
   building department itself recorded mechanical scope.
2. **Tier 2 — scope text.** A work description or permit type naming mechanical scope
   (e.g. `HVAC`, `chiller`, `air handler`, `make-up air`, `exhaust`, `VAV`).
3. **Tier 3 — trade implication.** A project class (per the customer's stated list:
   healthcare, data centres, manufacturing, industrial, large commercial, offices, retail,
   hospitality, schools, public buildings) that structurally requires mechanical work.

Tier 1 and 2 projects can reach `HIGH`. Tier 3 alone can reach `MEDIUM` at best, because
the customer's own instruction is that classification must rest on *documented evidence,
not guesses*.

---

## 3. Source collection strategy

### 3.1 What reconnaissance actually found

I probed the candidate sources before writing any code. The results shape the strategy:

| Source | Platform | Live? | Key fields | Verdict |
|---|---|---|---|---|
| **DallasNow (Accela Citizen Access)** | ASP.NET WebForms | **Yes — current** | record number, type, address, description, status | **Primary for Dallas**; publishes `Commercial Mechanical Permit` |
| Fort Worth Development Permits | ArcGIS REST | Yes, current | address, owner, job value, status, **`Mechanical` permit type** | **Primary** |
| Collin CAD Building Permits | Socrata (data.texas.gov) | Yes, to 2026-12 | address, owner, value, building area, type | **Primary** — but publishes no trade permits, so no Tier-1 evidence |
| Dallas Building Permits | Socrata | **No — ends 2019-12-31** | address, value, land use | Historical only, disabled |
| Dallas Permits FY23-24 | ArcGIS | Yes but ends 2023-12 | address, type, value, area | Historical only, disabled |
| Dallas GIS open data portal | ArcGIS Hub | **Requires sign-in** | — | Excluded |
| TDLR TABS | HTML form | **Requires login** | — | Excluded |
| TxSmartBuy ESBD | JS app | Renders client-side | — | Deferred |

Two findings deserve emphasis because they change the plan.

**Dallas is stale.** The flagship Socrata "Building Permits" dataset stops on 31 Dec 2019.
The GIS snapshot stops in Dec 2023. Dallas remains the largest city in the market, so this
is a real coverage gap, and the honest move is to say so rather than pad the dashboard with
2019 rows presented as fresh.

**Fort Worth is the best source, for a non-obvious reason.** It carries a first-class
`Mechanical` permit type. That is Tier 1 HVAC evidence, available as structured data, with
no inference required. This single fact is why Fort Worth is the launch city for Phase 1.

### 3.2 Source ranking policy

Sources are ranked by *evidentiary strength*, not volume:

1. Permit records with mechanical scope (Tier 1).
2. Permit records with commercial scope.
3. Certificate-of-occupancy and completion records.
4. Secondary references (appraisal district data).

A source is only added to config when all four hold: it is public, it is free, it needs no
authentication, and its terms permit this use. TDLR fails the third test and ESBD is
deferred until its API is understood, so neither is in Phase 1.

### 3.3 Verified coverage limits (measured, not assumed)

Reconnaissance and the first live runs produced findings that materially shape expectations.
They belong in the design document rather than in tribal knowledge.

**Dallas coverage was closed on 2026-09-21.** The Socrata extract (ends 2019-12-31) and the
GIS layer (ends 2023-12-29) are both stale, so Dallas was previously an explicit gap. The
current system is **DallasNow**, an Accela Citizen Access portal at
`aca-prod.accela.com/DALLASTX`, which is public, login-free, and current. The verified request
mechanism is documented in [`docs/dallas_source.md`](dallas_source.md).

**Dallas supplies Tier-1 mechanical evidence.** The portal publishes
`Commercial Mechanical Permit` as a first-class record type. Together with Fort Worth's
`Mechanical` type, two of the three sources in scope can evidence mechanical scope directly.
Collin CAD still cannot, because it publishes no trade permits.

**The Dallas portal publishes no value or area.** There is no declared job value and no floor
area on an Accela record, so those fields remain `Not verified.` for Dallas projects. This
directly limits how many Dallas projects can reach HIGH, because the significance gate needs
one of building class, value, or footprint.

**Dallas trade permits routinely describe maintenance, not projects.** A large share of
`Commercial Mechanical Permit` records are like-for-like equipment replacements whose own
disclaimer states that construction requires a separate permit. The construction-scope and
service-work rules exclude them, which is why 120 Dallas mechanical permits yield 22 projects
rather than 120.

**A high declared value is not the same as a large building.** Collin CAD carries owner
valuation records, so `permitvalue` values in the tens or hundreds of millions appear on
rows with no construction detail. A $200,000,000 record with no mechanical scope stays
`NEEDS_VERIFICATION` rather than being promoted on value alone.

**Some addresses are corridors, not properties.** A minority of records carry an address
with no street number, such as `BLUE RIDGE TRL, PLANO`. The value and the source are real,
but the property is not identified. These are held at `NEEDS_VERIFICATION` with an explicit
reason, and their location precision is recorded as `approximate`.

**Sources can disagree, and the platform does not resolve it.** Because Collin CAD and the
city permit feeds both describe overlapping geographies, the same project can carry different
values from different publishers. Both facts are retained and the field is flagged as
disputed; see §5.5.

### 3.4 Collection mechanics

- **Cadence**: manual or cron-driven, once per day at most. Government permit feeds change
  on business days, and crawling harder adds load to a public server for no benefit.
- **Politeness**: a token-bucket rate limiter per host, a descriptive `User-Agent` naming
  the project and a contact address, and pagination with a hard page cap.
- **Idempotency**: `raw_record` is unique on `(source_id, natural_key, payload_hash)`, so
  repeated runs are safe and store nothing twice.
- **Incremental**: connectors accept a `since` date where the source supports it
  (`$where=permitissueddate > '...'` on Socrata, `File_Date > ...` on ArcGIS).

---

## 4. Research / verification pipeline

### 4.1 Stages

```
1. FETCH      connector -> HTTP (rate limited, retry with backoff)
2. LAND       verbatim payload -> raw_record (+ sha256) + data/raw/*.jsonl
3. NORMALIZE  source row -> Permit, using a per-source field map
4. ASSEMBLE   related permits -> Project (address + time-window clustering)
5. ENRICH     attach parties, derive project_type, build evidence rows
6. CLASSIFY   apply Phase 2 rules -> HIGH / MEDIUM / NEEDS_VERIFICATION
7. PERSIST    upsert project + evidence + classification history
```

Stages 3–7 are pure functions over data. They are unit-testable with fixtures and never
touch the network, which is why the tests can run offline.

### 4.2 Assembly rules

A `project` groups permits that describe one physical development. Clustering is
deliberately conservative:

- **Same normalized address.** Formatting is normalized first (case, directory suffix,
  suite/unit stripping), because `12801 N CENTRAL EXPY Ste:1710` and `12801 N CENTRAL
  EXPY` are the same building.
- **Within a rolling window** (default 540 days) of the earliest permit.
- **Only permits that pass the commercial filter** are clustered. Residential rows are
  excluded before assembly so they can never pollute a commercial project.

Deterministic tie-breaking: when two permits in a cluster disagree, the field resolves by
documented precedence — mechanical permits first, then newest issue date, then lowest
permit number. The chosen value's origin is recorded in `evidence`, so a reviewer can see
why one value won.

### 4.3 Verification discipline

Every project carries `last_verified`, the timestamp of the most recent successful ingest
that touched it. The dashboard shows a data-freshness banner derived from the sources'
own maximum dates, so a user is never misled about recency.

An important distinction the schema keeps: **verification means "a source states this", not
"this is true in the world."** If a permit lists a GC and the GC has since been replaced,
we still report what the permit says and cite it. We are a research tool, not an oracle.

---

## 5. Opportunity classification logic

### 5.1 Labels

- **HIGH** — strong, documented HVAC/mechanical signal *and* a high-value trade class.
- **MEDIUM** — documented commercial-scale project with a structural mechanical need.
- **NEEDS_VERIFICATION** — promising but incomplete; a human should check the source.

The default for a commercial permit with no mechanical evidence is **NEEDS_VERIFICATION**,
never HIGH. Under-claiming is the correct failure mode for a tool whose value rests on
trust.

### 5.2 Scoring

Points are additive, and each is recorded as a human-readable reason so the classification
is never a black box.

**Mechanical evidence (the dominant factor)**

| Signal | Points |
|---|---|
| Tier 1 mechanical permit on the same address | +45 |
| Tier 2 mechanical scope keywords in permit text | +30 |

**Property class** (per the customer's priority list)

| Class | Points |
|---|---|
| Healthcare, data centre, manufacturing, industrial | +35 |
| Large commercial, office, retail, hospitality, school, public | +25 |
| Other commercial | +5 |

**Scale**

| Signal | Points |
|---|---|
| Value ≥ $5,000,000 | +20 |
| Value ≥ $1,000,000 | +15 |
| Value ≥ $250,000 | +8 |
| Square footage ≥ 50,000 | +12 |
| Square footage ≥ 10,000 | +6 |

**Construction phase**

| Signal | Points |
|---|---|
| Status indicates issued / approved / under construction | +15 |
| Permit dated within the last 180 days | +10 |

### 5.3 Thresholds and gates

| Score | Label | Condition |
|---|---|---|
| ≥ 70 | HIGH | requires Tier-1/Tier-2 mechanical evidence **and** commercial scale **and** a stated building class, value, or footprint |
| 40 – 69 | MEDIUM | requires a commercial class and a value or size signal |
| < 40 | NEEDS_VERIFICATION | everything else, including thin records |

Four gates are enforced in code rather than by arithmetic alone. Each was added because a
live Phase 1 run produced a false positive that motivated it:

1. **Mechanical-evidence gate.** A project cannot be HIGH on property class and value
   alone. Without this rule the numeric threshold is reachable by a large warehouse with no
   mechanical scope on record.
2. **Scale gate.** A Tier-2 (scope-text) HIGH must also clear $250,000 or 10,000 sq ft. A
   live run surfaced a $18,000 roof replacement — "Mechanically attach 4.5″ Polyiso Rigid
   Insulation… on high sides of HVAC curbs" — which matched the word *mechanical* as an
   adverb and was otherwise a roofing job, not an HVAC opportunity. Tier-1 mechanical
   permits are exempt, because a mechanical permit is direct evidence of mechanical work
   whatever the declared job value.
3. **Construction-scope gate.** At assembly time, a project must contain at least one permit
   that describes construction work. A standalone plumbing or mechanical permit is a
   service call. Without this gate a two-page Fort Worth sample produced 1,229 "projects"
   that were mostly residential service calls; with it, the same sample produces 68 genuine
   commercial projects.
4. **Significance gate.** A HIGH record must identify a building class, a declared value, or
   a footprint. The 2026-09-21 Dallas run produced 27 projects that were each a single
   mechanical permit with no other context. Mechanical scope was confirmed, but nothing
   established that the work was significant, so the label overstated the evidence. These
   are now MEDIUM with a stated reason; HIGH fell from 30 to 2 on that run.

Two further correctness rules apply to source text, both found the same way:

- **Boilerplate is stripped before matching.** Dallas Accela appends a disclaimer to every
  trade permit stating that the permit authorises work only for the approved trade and that
  construction of any structure requires a separate permit. That disclaimer contains the
  word "Construction", so leaving it in place meant the disclaimer *stating that a permit is
  not construction work* was itself satisfying the construction-scope gate.
- **Service-work language disqualifies a permit.** A description containing "like for like",
  "replace existing unit", or "changeout" describes maintenance rather than a project, and is
  excluded regardless of any construction keyword elsewhere in the text.

The keyword matcher also requires whole-word matches. A substring test makes the roofing
adverb "mechanically" register as mechanical scope.

### 5.4 Worked example

A Fort Worth hospital permit, $12M, 90,000 sq ft, status Issued, filed 30 days ago, with a
sibling mechanical permit at the same address:

```
45 (tier-1 mechanical) + 35 (healthcare) + 20 (value) + 12 (size) + 15 (status) + 10 (recency)
= 137  ->  HIGH
```

The same project with no mechanical permit and no mechanical scope text scores 92, which
still clears the numeric threshold — and is nonetheless held at MEDIUM by the evidence
gate, with `classification_reasons` explaining that mechanical scope is unconfirmed.

---

### 5.5 Contradictory values across sources

When two public sources state different values for the same field, the platform must not
choose one silently. A quietly chosen value is indistinguishable from an invented one, and it
fails the moment a customer checks a second source.

The rule is therefore: **store both facts and identify the discrepancy.**

The worked case the customer raised:

```
TDLR value:        $250,000,000
City permit value: $264,000,000
```

Neither is averaged, summed, or replaced. Both are recorded as separate evidence rows with
their own source, URL, record key, and date. The project field additionally carries:

- `disputed_fields` — the list of field names in disagreement
- `discrepancies` — the competing values with their citations
- a classification reason stating that the disagreement is unresolved

Numeric comparison uses a 0.5% relative tolerance so that ordinary rounding differences
($4,300,000 versus $4,300,000.00) are not reported as disputes. Text comparison ignores case
and whitespace, but `ACME HOLDINGS LLC` versus `ACME HOLDINGS INC` is a genuine discrepancy.

Precedence still decides which value a record *displays*, so a project is readable. The
discrepancy list is what stops that decision from being invisible.

---

## 6. Dashboard structure

### 6.1 Phase 3 scope

A read-only Flask app serving server-rendered HTML. No JavaScript framework, no build step,
no accounts. It reads SQLite directly.

**Views**

1. **Opportunity list** — the default screen. Ranked projects with classification badge,
   city, type, value, size, status, and a mechanical-evidence indicator.
2. **Project detail** — the complete record. All 20 fields, each with its source link and
   `last_verified`. Fields with no supporting evidence read `Not verified.` An evidence
   table lists every fact and exactly where it came from.
3. **Data freshness** — per-source last-observed date and the source's own maximum record
   date, so the Dallas gap is visible rather than hidden.

### 6.2 Filters (all requested filters, in SQL)

- free-text search over name, address, owner, GC, architect
- city
- project type
- value range (min/max)
- permit date range
- classification
- HVAC/mechanical evidence present (yes/no)
- source

### 6.3 Design principles

- **Unverified is visible, not blank.** Empty cells render `Not verified.` so absence of
  data is legible and never mistaken for absence of the feature.
- **Every fact is clickable.** Each source-derived value links to its original record.
- **Compliance note on page.** A footer stating the data is public, may be incomplete, and
  should be confirmed with the issuing jurisdiction before commercial reliance.

---

## 7. Client report structure

### 7.1 Format and audience

A single self-contained HTML file, printable to PDF, generated per run. It is written for a
commercial HVAC contractor's business-development lead: what the project is, why it may
matter, and how to verify it independently.

### 7.2 Layout

```
Header     Business name, report title, market (DFW), generated date,
           count of opportunities, and the data-freshness caveat.
Summary    Distribution by classification, city, and trade class.
Opportunities  One block per project, ordered by score.
Footer     Sourcing methodology, compliance and disclaimer.
```

Each opportunity block carries exactly the requested fields:

```
PROJECT                  <project_name or "Not verified.">
LOCATION                 <address, city, TX>
VALUE                    <estimated_project_value or "Not verified.">
SIZE                     <square_footage or "Not verified.">
PROJECT TYPE             <project_type>
OWNER                    <owner or "Not verified.">
GENERAL CONTRACTOR       <general_contractor or "Not verified.">
ARCHITECT                <architect or "Not verified.">
HVAC/MECHANICAL EVIDENCE  <Tier + literal excerpt, or "Not verified.">
STATUS                   <project_status or "Not verified.">
WHY IT MAY MATTER        <generated from classification_reasons>
SOURCE LINKS             <one link per contributing record>
LAST VERIFIED            <last_verified timestamp>
```

### 7.3 The "why it may matter" generator

Built from `classification_reasons` as a deterministic template, never free-form generated
prose. Because an LLM could embellish, and embellishment is exactly what the critical data
rule forbids, this section is assembled from recorded reasons only. Example output:

> Mechanical permit on file at this address (permit PM26-09104). Healthcare project class.
> Estimated value $12,000,000. Filed within the last 180 days.

### 7.4 Report honesty rules

- If a project has no mechanical evidence, the report says so plainly. It does not sell it.
- Reports are dated and state the data-freshness limits for their market.
- No summarised total is presented as complete when a major city (Dallas) is known stale.

### 7.5 Procurement status

Whether work is currently out to bid is a claim about the market, not about the permit
record. No configured source publishes bid status, so the platform cannot know it and must
not imply it. Four values are permitted:

| Value | Meaning |
|---|---|
| `Confirmed open` | a source explicitly advertises the work for bid or award |
| `Evidence found, status unclear` | a source shows active work, but says nothing about procurement |
| `Not verified` | nothing supports any procurement statement |
| `Closed` | a source shows the work is finished, expired or withdrawn |

`Confirmed open` is therefore effectively unreachable in this market, which is the correct
outcome rather than a limitation to work around. An issued permit reads as
`Evidence found, status unclear`, never as an open bid.

`Closed` is kept distinct from `Not verified` deliberately. "This work is finished" and "we
cannot tell whether this is live" call for different actions, and collapsing them would hide
a fact the sources did establish. Measured on the live database, 688 projects are `Closed`
and are excluded from customer output.

Completion is tested before activity, because a status can carry both. `Final CO Issued`
contains the active word "issued" but means the building is finished. Matching is
word-boundary based, so `Incomplete Submittal` — an active status — is not read as "complete".

Every customer brief carries this notice verbatim:

> Public permit evidence does not by itself confirm that the HVAC/mechanical package is
> currently available for bid.

### 7.6 Two report formats

**Customer brief** (`reports/out/customer_brief.md`). Five opportunities, ordered by
strength. Header carries the recipient, date and market. Each opportunity carries location,
project type, mechanical evidence, construction/activity status, procurement status with an
explanation, project significance, value, square footage, owner/developer, GC, architect,
"why it was identified", "what is not verified", any source disagreement, and the source
records with a resolvable URL. It contains no scoring arithmetic, no permit grids and no
database internals.

**Internal research report** (`reports/out/internal_report.md`). The full audit trail:
field-level verification with verdicts and excerpts, the raw classification reasoning, every
contributing permit, source provenance with the number of fields each record supports, and
any discrepancies.

Both are generated from the same records by one class, so the two formats cannot disagree.

### 7.7 Customer-brief eligibility

An opportunity reaches a customer brief only when every criterion in `eligibility.py` holds:

1. not completed (`Closed` or `Not verified` procurement is rejected)
2. commercial construction evidence
3. mechanical/HVAC evidence at tier 1 or 2
4. classification HIGH or MEDIUM
5. at least one significance fact — a building class, declared value or footprint
6. not service, repair, maintenance or replacement work
7. not plumbing-only or electrical-only

Confirmed open bidding is **not** required, because no configured source can establish it.
Measured on the live database, 43 of 3,940 projects are customer-brief eligible. The reason
each selected opportunity was included is stored and printed, so selection is explainable
rather than a black box.

### 7.8 Building identity and duplicate suites

A single building can hold many suite-level records, and each assembles into its own project.
Measured live, 316 base street addresses hold more than one project and the largest group is
22. Presented naively, a five-opportunity brief could show five suites of one office tower as
five independent projects.

The approach is deliberately conservative:

- A `building_key` groups projects sharing a normalised base address, with suite designators
  removed.
- Projects are **never merged**. Each keeps its own permits, evidence and classification.
- The relationship is reported as *uncertain*, because a shared street number does not prove
  a shared building.
- Selection prefers at most one project per building key. Siblings are used to fill remaining
  slots if necessary, and the report states the relationship plainly when that happens.

An address with no street number yields no key and never participates in grouping.

---

## 8. MVP implementation plan

### Phase 1 — Data pipeline *(implemented)*

- `config/sources.yaml`, `config/trades.yaml`
- `models.py` — `Project`, `Permit`, `Evidence`, dataclasses
- `db.py` — schema creation, upsert, queries
- `provenance.py` — the not-verified rule and evidence construction
- `normalize.py` — per-source field maps, address and value normalization
- `assemble.py` — permit clustering into projects
- `connectors/` — `base.py`, `fort_worth_permits.py`, `collin_cad_permits.py`,
  `dallas_permits.py`, plus a registry
- `pipeline.py` — orchestration of fetch → land → normalize → assemble → persist
- `cli.py` — `ingest`, `initdb`, `stats`

**Exit criterion**: a live run against Fort Worth and Collin CAD produces real projects in
SQLite, with per-field evidence and no invented values.

### Phase 2 — Intelligence layer

- `classify.py` implementing §5, emitting reasoned labels
- `project_classification` history written on every change
- Golden-file tests for the gating rule and each threshold

### Phase 3 — Dashboard

- Flask app, list/detail/freshness views, all §6 filters
- Templates styled for dense tabular reading

### Phase 4 — Client report

- Report generator rendering §7 to HTML
- Optional PDF via print stylesheet

### Explicitly out of scope for the MVP

Payments, multi-tenant accounts, mobile apps, paid data feeds, LLM-written narrative,
real-time alerting, and any automated outreach. The MVP exists to prove that valuable,
*verifiable* DFW opportunities can be discovered — not to scale prematurely.

### Modularity contract

Adding a city means: add a `sources.yaml` entry, add a connector class, register it.
Adding a trade means: add a `trades.yaml` profile with its keywords and weights. The core
domain, pipeline, and dashboard remain unchanged.