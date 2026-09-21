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

Ten records per page. The pager anchors are postbacks on the grid's pager row:

```
ctl00$PlaceHolderMain$dgvPermitList$gdvPermitList$ctl13$ctl03   -> page 2
ctl00$PlaceHolderMain$dgvPermitList$gdvPermitList$ctl13$ctl04   -> page 3
...  ctl13$ctlNN  -> page NN-1
```

Each pager POST must again carry the fresh viewstate returned by the previous response.

### Export

There is a "Download results" control:

```
ctl00$PlaceHolderMain$dgvPermitList$gdvPermitList$ctl13$lb4btnExport
```

Verified 2026-09-21: this returns `text/html` (the re-rendered page), not a file. No
`Content-Disposition` header is set and no download URL appears in the response. The export
is therefore a site feature for interactive use, not a machine-readable endpoint, and the
connector does not rely on it. Pagination is used instead.

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