# Master prompt: rebuild this product from scratch

Paste the prompt below into a fresh agent session to recreate the DFW commercial construction
opportunity intelligence product. It is self-contained: it states the goal, the hard rules that
must hold, the shape of the pipeline, and the deployment target. It does not name any file that
does not need to exist — the structure is the outcome, not the input.

The prompt is deliberately strict about the honesty rules. Those are the part that is easy to get
wrong and hard to notice, and they are what separate this product from a scraper with a map on
top. Everything else is ordinary web work.

---

## The prompt

> Build a web product that turns public DFW building-permit records into commercial construction
> **opportunity intelligence** for an HVAC/mechanical contractor. Two layers, kept strictly apart:
>
> **1. Intelligence layer** — connectors fetch permits from public, no-authentication government
> sources (Fort Worth, Dallas, and Collin County appraisal-district data). Normalise each raw
> record into a permit with a stable natural key; group permits into projects by normalised
> address; classify each project; detect procurement status; diff successive assemblies to detect
> real changes; grade data quality. Persist everything in SQLite.
>
> **2. Application layer** — a Flask site that only *reads* intelligence data through a single
> service module. It re-implements no classification, scoring or evidence logic, and issues no
> `FROM permit` query. Every market, city and trade is resolved from YAML config, never hardcoded.
>
> **The rules below are non-negotiable. Enforce each with a test, because a rule that is not
> tested will be broken by the next change.**
>
> 1. **Never invent a value.** A missing field stays `None` in the database. It renders as
>    "Not verified" in HTML and `null` in JSON — never the string in JSON.
> 2. **Relevance is not procurement.** A project relevant to HVAC is not an open bid. Only a
>    source that states bid language yields `Confirmed open`. No configured source publishes bid
>    status, so that state is unreachable, and every page says so plainly.
> 3. **The evidence standard must be visible.** Every project shows what the source actually
>    supports: the evidence tier, the matched excerpt, and the field it came from. A permit that
>    names mechanical work to rule it out — "no mechanical, electrical, or plumbing work", or
>    "existing MEP systems are to remain as-is" — is *not* evidence. Match keywords with word
>    boundaries and reject a negated occurrence, whether the negation precedes or follows the
>    keyword.
> 4. **Widen discovery, never the claim.** Discovery runs on the commercial base: commercial
>    classification and not known to be closed. A listing may therefore carry no trade evidence,
>    so every listing carries a trade-signal badge stating exactly what the record supports
>    ("Mechanical permit on file", "Mechanical evidence found", "Trade not verified"). A record
>    with no evidence is a commercial opportunity, never a labelled HVAC one. Make the gate a
>    config flag, and let a visitor narrow to the evidence-scoped view.
> 5. **Unknown is not closed.** "We do not know the procurement status" is not "there is nothing
>    to procure". Sources that publish no status at all still yield discoverable projects. Only
>    work known to be finished or dead is withheld from discovery — and it stays reachable by
>    direct URL, where its status is stated.
> 6. **User workflow is not source status.** Pipeline stages are the account's own labels, kept
>    disjoint from procurement states. A test asserts the two vocabularies do not overlap.
> 7. **Every alert has an underlying event**, pointing at a recorded change or a first-match
>    event. Change detection records real differences only — a no-op assembly pass emits nothing.
> 8. **No web route grants privilege.** Admin only via CLI; plans only via CLI. CSRF, rate
>    limiting and security headers are on in production, off in tests that need them off.
> 9. **The application layer holds no intelligence logic.** Assert it with an architecture test.
>
> **Deployment target: one persistent Render web service, with no cron and no systemd** (PID 1 is
> not an init system). Automation must be self-contained in a single process that does both jobs:
>
> * **Supervise the web server** in the foreground on the platform's `$PORT`, restarting it if it
>   exits.
> * **Refresh the data on its own thread**, so a long ingestion never blocks supervision. Fetch
>   the newest pages of every source, assemble and classify, rebuild the search index, then raise
>   alerts from detected changes.
> * **Deepen the refresh instead of repeating it.** A small fixed page cap re-reads the newest
>   pages on every pass and never reaches older history. Double the page cap after each clean
>   pass, up to a configured ceiling; retry a failed pass at the same width. Record the depth in a
>   state file so it survives a restart.
> * Ship a ready Render blueprint. Default to the Free plan (no disk) so it starts, and document
>   the one paid change — a mounted disk — that makes the dataset outlive a deploy. Never point
>   the data directory at a mount path that does not exist.
> * The health check returns `503` only when the database is unreachable; an empty first boot is a
>   `200` with an `"empty"` status, because restarting would not fill it.
>
> **Also build:** search, filters and pagination; a keyword-to-page map with a programmatic-page
> quality gate that the sitemap re-evaluates so the two signals agree; per-market and per-city
> landing pages; an account workspace (watching, pipeline, notes, tags, activity, org peers) with
> per-user rows only; an entitlement layer with no payment code; a JSON API; and an SEO report
> computed from the map and the database that reports no ranking it has not measured.
>
> **Testing:** no mocks. Intelligence tests use real captured payloads; application tests run the
> real Flask app against a real SQLite database built from fixtures that mirror the live shapes
> that matter — a mechanical project, a completed project, a service-only permit, a plumbing-only
> record, a text-only tier-2 claim, and a negation trap. Keep the suite green.
>
> **Documentation:** keep a README that states the honesty rules in prose, an `AGENTS.md` that
> records the non-negotiable invariants and the gotchas for the next agent, and this rebuild
> prompt. Docs describe what is true now, not what changed.

---

## What to verify once it is rebuilt

A rebuild is done when these hold against a real database, not just against fixtures:

```bash
export PYTHONPATH=src SECRET_KEY=dev-only-not-for-production

python -m oppintel.cli initdb
flask --app oppintel.app.wsgi init-app
python -m oppintel.cli ingest --max-pages 1     # a live fetch, not a fixture
python -m oppintel.cli assemble
flask --app oppintel.app.wsgi build-search-index
flask --app oppintel.app.wsgi monitor

python -m pytest tests/ -q                       # all green
python -m oppintel.cli stats                     # discovery base, cities, mechanical counts
flask --app oppintel.app.wsgi report-quality     # "No open data-quality issues."
```

Then, with the app running:

* `/healthz` returns `200` and real counts.
* `/api/opportunities` lists the commercial base, and each row carries a `trade_signal` that
  matches its `mechanical_evidence_tier`.
* `?mechanical_only=1` returns only rows with evidence — none labelled "Trade not verified".
* A known negation case ("no mechanical, electrical, or plumbing work", "MEP systems are to
  remain as-is") is listed without a mechanical tier.
* The number of cities in discovery matches the number of cities the market config covers.
