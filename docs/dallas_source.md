# Dallas current permit source: discovered mechanism

Investigated and verified 2026-09-21. This document records the request mechanism behind the
current City of Dallas public permit interface, so the finding is reproducible and auditable.

## What was ruled out first

| Candidate | Result |
|---|---|
| Dallas open data (Socrata) `e7gq-4sah` | Max `issued_date` = **2019-12-31**. Stale. |
| Dallas GIS `T_BU_Permits_FY2023_24` (ArcGIS) | Max `ISSUE_DATE` = **2023-12-29**. Stale. |
| Dallas GIS `NewPermit_2008_2024` (ArcGIS) | Max date 2024-11-11. Stale. |
| `gisservices-dallasgis.opendata.arcgis.com` | Requires sign-in. |
| `developdallas.dallascityhall.com` | DNS does not resolve (000). |
| `dallascityhall.com/.../permit_reports2.aspx` | HTTP 401. |
| TDLR TABS | Requires an account. |

None of these can supply current Dallas commercial permits.

## What the current system is

The City's "DallasNow" citizen portal is **Accela Citizen Access**, agency code `DALLASTX`:

```
https://aca-prod.accela.com/DALLASTX/Cap/CapHome.aspx?module=Building&TabName=Building
```

It is a public, no-login interface that returns **current** records. A search on
2026-09-21 returned records dated 2026-09-21, including record numbers of the form
`COM-ALT-ADD-26-002163` and `REC26-...`.

### Permit types available (61 options), the commercially relevant ones

The `ddlGSPermitType` option values encode `Module/Category/SubCategory/Variant`, which is
how the server-side filter is expressed:

| Label | Value |
|---|---|
| Commercial New Construction Permit | `Building/Commercial/New/NA` |
| Commercial Alteration Addition Permit | `Building/Commercial/Alteration Addition/NA` |
| **Commercial Mechanical Permit** | `Building/Commercial/Mechanical/NA` |
| Commercial Electrical Permit | `Building/Commercial/Electrical/NA` |
| Commercial Plumbing Permit | `Building/Commercial/Plumbing/NA` |
| Commercial Roofing Permit | `Building/Commercial/Roofing/NA` |
| Certificate of Occupancy | `Building/Certificate of Occupancy/NA/NA` |
| Site Plan Review | `Building/Site Plan/Review/NA` |

`Commercial Mechanical Permit` is a first-class mechanical scope category, which is Tier-1
HVAC evidence available directly from the City.

## The request mechanism

The page is ASP.NET WebForms. The search is a `__doPostBack` from an anchor, not a plain
form GET, and it is guarded by three things that must all be satisfied:

1. **Session-scoped `__VIEWSTATE`.** The token must come from a `GET` performed in the
   *same* `requests.Session`. Reusing a previously saved token produces
   `/DALLASTX/Error.aspx`.
2. **Session cookies**, of which `ACA_CS_KEY`, `ACA_SS_STORE` and `.ASPXANONYMOUS` matter.
3. **`Origin` and `Referer` headers.** This is the non-obvious requirement. Without them the
   POST is rejected with an error page. With them the identical payload returns results.

### Request

```
POST https://aca-prod.accela.com/DALLASTX/Cap/CapHome.aspx?module=Building&TabName=Building
Content-Type: application/x-www-form-urlencoded

__VIEWSTATE            = <from a GET in this session>
__VIEWSTATEGENERATOR   = <from the same GET>
__VIEWSTATEENCRYPTED   = <empty>
__EVENTTARGET          = ctl00$PlaceHolderMain$btnNewSearch
__EVENTARGUMENT        =
ctl00$PlaceHolderMain$generalSearchForm$ddlSearchType = 0
ctl00$PlaceHolderMain$generalSearchForm$ddlGSPermitType = Building/Commercial/Mechanical/NA
ctl00$PlaceHolderMain$generalSearchForm$txtGSStartDate = MM/DD/YYYY
ctl00$PlaceHolderMain$generalSearchForm$txtGSEndDate   = MM/DD/YYYY
... plus every other hidden field from the form, unmodified
```

All remaining hidden inputs on the form must be echoed back unchanged. Omitting them also
produces an error page, so the connector parses and replays the whole form rather than
hard-coding a subset.

### Response

HTML. Results are in the `ctl00_PlaceHolderMain_dgvPermitList_gdvPermitList` grid, one
`<tr class="ACA_TabRow...">` per record, with columns:

`Date | Record Number | Record Type | Address | Description | Project Name | Expiration Date | Status | Action | Short Notes`

Address is a single combined field, e.g. `11056 SHADY TRL, Dallas TX 75229`.

### Pagination

Ten records per page. The pager is a **windowed** control, and its shape was mapped
empirically against a live result set on 2026-09-21:

| Control | Label | Lands on |
|---|---|---|
| `ctl13$ctl02` | `< Prev` | previous page |
| `ctl13$ctl03` … `ctl13$ctl11` | `2` … `10` | pages 2 … 10 |
| `ctl13$ctl12` | `...` | the ellipsis, which jumps **ten** pages on |
| `ctl13$ctl14` | `Next >` | the next page |

This matters more than it looks. Pager indices are **not** a stable arithmetic function of
the page number: `ctl12` means page 11 only while the window sits at pages 1–10. Once the
window shifts, computing `ctl{page+1}` lands on the wrong control — and because the response
is still a valid results page, the failure is silent. An earlier version of the connector
used that arithmetic and oscillated between two pages, collecting 10 records where 309
existed.

The connector therefore keys off the anchor **label**: it finds the `Next >` anchor and
follows that, and treats the anchor's absence as the end of the result set. There is no
`Next >` on the final page, so termination is natural rather than inferred from a count.

A second trap: the document contains **16** `aca_pagination` regions, because a hidden
"resume application" dialog reuses the same CSS classes. Only the last one belongs to the
live result grid, so the connector takes the last matching anchor.

### Result counter

The counter saturates and then grows as the search is paged:

* page 1 of a large set reports the literal `100+`
* page 11 of the same set reports `200+`

It is therefore a **lower bound that increases with pagination**, never an exact total. The
connector does not use it to decide when to stop; it paginates until the `Next >` anchor
disappears.

### Export

There is a "Download results" control:

```
ctl00$PlaceHolderMain$dgvPermitList$gdvPermitList$ctl13$lb4btnExport
```

Verified 2026-09-21: this returns `text/html` (the re-rendered page), not a file. No
`Content-Disposition` header is set and no download URL appears in the response. The export
is a site feature for interactive use, not a machine-readable endpoint, and the connector
does not rely on it. Pagination is used instead.

### Reliability

The portal returns intermittent `502` responses under sustained pagination. Because a crawl
issues one POST per page, an un-retried failure aborts the whole sequence and silently
truncates the result set. `_post` therefore retries transient statuses (`429`, `500`, `502`,
`503`, `504`) with exponential backoff, and the permit write path commits every 250 rows so
an interrupted crawl keeps the records it already retrieved.

### Record types fetched

The connector searches by record type, because the interface filters on one type at a time.
The exact option values below were read from the live form on 2026-09-21. The value encodes
`Module/Category/SubCategory/Variant`, which is the shape the server expects.

**Commercial construction (the primary targets)**

| Label | Option value |
|---|---|
| Commercial New Construction Permit | `Building/Commercial/New/NA` |
| Commercial Alteration Addition Permit | `Building/Commercial/Alteration Addition/NA` |
| **Commercial Mechanical Permit** | `Building/Commercial/Mechanical/NA` |
| Commercial Electrical Permit | `Building/Commercial/Electrical/NA` |
| Commercial Plumbing Permit | `Building/Commercial/Plumbing/NA` |
| Commercial Roofing Permit | `Building/Commercial/Roofing/NA` |
| Commercial Accessory Structure Permit | `Building/Commercial/Accessory/NA` |
| Commercial Demolition Permit | `Building/Commercial/Demolition/NA` |
| Commercial Solar/PV Permit | `Building/Commercial/SolarPV/NA` |
| Commercial Pool/Spa Permit | `Building/Commercial/PoolSpa/NA` |
| Commercial Foundation Repair | `Building/Commercial/Foundation/NA` |

**Project-stage records**

| Label | Option value |
|---|---|
| Certificate of Occupancy | `Building/Certificate of Occupancy/NA/NA` |
| Site Plan Review | `Building/Site Plan/Review/NA` |
| Phase - New Construction | `Building/Commercial/Phase/New` |
| Phase - Alteration Addition | `Building/Commercial/Phase/Alteration Addition` |
| Excavation and Grading Permit | `Building/Grading/NA/NA` |
| Fire Prevention Construction Permit | `Building/Fire/NA/NA` |

**Residential counterparts** (60 types in total; these are excluded) follow the same shape,
e.g. `Building/Residential/Mechanical/NA`, `Building/Residential/New/NA`.

`Commercial Mechanical Permit` is a first-class mechanical scope category, which is what
makes Tier-1 HVAC evidence available directly from the City.

## Coverage measured

| Search | Result |
|---|---|
| Commercial New Construction, 2026-09-01..2026-09-21 | 25 records |
| Commercial Mechanical, 2026-09-01..2026-09-21 | 100+ records |
| Commercial Electrical, 2026-09-01..2026-09-21 | 100+ records |
| Commercial Alteration Addition, 2026-09-01..2026-09-21 | 100+ records |

The form's default start date is `10/09/2015`, which is the earliest date the interface
accepts by default. Latest observed record date is the current date, so coverage is
**current**.

Note the result counter saturates: it reports the literal string `100+` once a search
exceeds 100 matches instead of a true total. The connector therefore paginates until the
grid stops yielding new records rather than trusting the count, and records this limit in
its notes.

## What this means for the platform

Dallas moves from "no current source" to "current source available", and it supplies
Tier-1 mechanical evidence (`Commercial Mechanical Permit`) as structured data — something
only Fort Worth previously provided. The three classification gates are unchanged; Dallas
records flow through the same pipeline and the same evidence standards.