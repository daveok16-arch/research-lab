"""Opportunity service: the boundary between the intelligence data and the web application.

Every read the public site performs goes through this module. It exists so that no route,
template or frontend helper ever writes SQL against the intelligence tables, and so the
eligibility, procurement and grouping rules that govern what may be shown publicly are applied
in exactly one place.

What this layer does **not** do: it does not classify, score, assemble or evaluate evidence.
Those decisions are already made and stored by the intelligence pipeline. The service reads
them. If a rule about what counts as an opportunity changes, it changes in
`eligibility.py` / `classify.py`, not here.

Two rules are enforced at this boundary rather than being left to the UI:

* **Public by default excludes closed and unverified work.** A public directory listing a
  finished building as an opportunity would be a false claim, so the filter lives here.
* **Missing information is returned as None, never as a placeholder.** Rendering
  "Not verified" is the template's job; the service must not invent a value to make a
  template simpler.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable

from .config import MarketConfig, TradeConfig, load_sources
from .db import Database
from .eligibility import evaluate
from .grouping import building_key, group_projects, sibling_info_for
from .procurement import CLOSED, EVIDENCE_FOUND, NOT_VERIFIED, CONFIRMED_OPEN
from .search_index import quote_for_fts

#: Classifications the public site may show, in display order.
PUBLIC_CLASSIFICATIONS = ("HIGH", "MEDIUM")

#: Procurement states that disqualify a project from public *discovery*. A closed project may
#: still be reachable by direct URL, where its status is stated plainly.
DISCOVERABLE_PROCUREMENT = (CONFIRMED_OPEN, EVIDENCE_FOUND)

#: Sort options exposed to the public. Deliberately factual orderings only: there is no
#: "best opportunity" sort, because that would be an invented judgement.
SORT_OPTIONS = {
    "recent": ("Most recent permit", "p.permit_date DESC NULLS LAST, p.id"),
    "updated": ("Recently updated", "p.updated_at DESC, p.id"),
    "status": ("Project status", "p.project_status ASC, p.permit_date DESC NULLS LAST, p.id"),
}

DEFAULT_SORT = "recent"

PAGE_SIZE = 20
MAX_PAGE_SIZE = 50


@dataclass
class OpportunityFilters:
    """Filters accepted from the public UI.

    Every field is optional and every field maps to a stored column. Nothing here is derived
    or scored, so a filter can never disagree with the record it filters on.
    """

    q: str | None = None
    city: str | None = None
    project_type: str | None = None
    classification: str | None = None
    procurement_status: str | None = None
    market: str | None = None
    trade: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    include_unverified: bool = False
    sort: str = DEFAULT_SORT
    page: int = 1
    page_size: int = PAGE_SIZE

    def normalised(self) -> OpportunityFilters:
        """Clamp values so a hand-edited query string cannot cause a large or invalid query."""
        page = max(1, int(self.page or 1))
        size = int(self.page_size or PAGE_SIZE)
        size = max(1, min(size, MAX_PAGE_SIZE))
        return OpportunityFilters(
            q=(self.q or "").strip() or None,
            city=(self.city or "").strip() or None,
            project_type=(self.project_type or "").strip() or None,
            classification=(self.classification or "").strip() or None,
            procurement_status=(self.procurement_status or "").strip() or None,
            market=(self.market or "").strip() or None,
            trade=(self.trade or "").strip() or None,
            date_from=(self.date_from or "").strip() or None,
            date_to=(self.date_to or "").strip() or None,
            include_unverified=bool(self.include_unverified),
            sort=self.sort if self.sort in SORT_OPTIONS else DEFAULT_SORT,
            page=page,
            page_size=size,
        )

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    def as_query_string(self, **overrides: Any) -> str:
        """Rebuild the filter set as a query string, for pagination and canonical links."""
        params: dict[str, Any] = {
            "q": self.q,
            "city": self.city,
            "project_type": self.project_type,
            "classification": self.classification,
            "procurement_status": self.procurement_status,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "sort": self.sort if self.sort != DEFAULT_SORT else None,
        }
        if self.include_unverified:
            params["include_unverified"] = "1"
        params.update(overrides)
        pairs = [f"{k}={_quote(str(v))}" for k, v in params.items() if v not in (None, "")]
        return "&".join(pairs)


def _quote(value: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(value)


@dataclass
class OpportunityPage:
    """One page of results plus everything a template needs to render the pagination."""

    items: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = PAGE_SIZE
    filters: OpportunityFilters = field(default_factory=OpportunityFilters)

    @property
    def total_pages(self) -> int:
        if self.page_size <= 0:
            return 1
        return max(1, (self.total + self.page_size - 1) // self.page_size)

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages

    @property
    def first_index(self) -> int:
        return 0 if not self.items else self.offset + 1

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def last_index(self) -> int:
        return self.offset + len(self.items)

    @property
    def is_empty(self) -> bool:
        return not self.items


class OpportunityService:
    """Read access to projects, scoped to what may be shown publicly."""

    def __init__(self, db: Database, market: MarketConfig, trade: TradeConfig):
        self.db = db
        self.market = market
        self.trade = trade

    # --- filters --------------------------------------------------------------

    def available_cities(self) -> list[str]:
        """Cities that actually hold discoverable opportunities.

        Sourced from the database rather than the market config, so the filter never offers a
        city with nothing behind it.
        """
        rows = self.db.conn.execute(
            """
            SELECT city, COUNT(*) AS n
              FROM project
             WHERE classification IN (?, ?)
               AND procurement_status IN (?, ?)
               AND city IS NOT NULL
             GROUP BY city
             ORDER BY n DESC, city
            """,
            PUBLIC_CLASSIFICATIONS + DISCOVERABLE_PROCUREMENT,
        ).fetchall()
        return [r["city"] for r in rows]

    def available_project_types(self) -> list[str]:
        rows = self.db.conn.execute(
            """
            SELECT project_type, COUNT(*) AS n
              FROM project
             WHERE classification IN (?, ?)
               AND procurement_status IN (?, ?)
               AND project_type IS NOT NULL
             GROUP BY project_type
             ORDER BY n DESC, project_type
            """,
            PUBLIC_CLASSIFICATIONS + DISCOVERABLE_PROCUREMENT,
        ).fetchall()
        return [r["project_type"] for r in rows]

    def procurement_options(self) -> list[str]:
        """Procurement states present among public projects, in the order they should read."""
        rows = self.db.conn.execute(
            """
            SELECT DISTINCT procurement_status FROM project
             WHERE classification IN (?, ?) AND procurement_status IS NOT NULL
            """,
            PUBLIC_CLASSIFICATIONS,
        ).fetchall()
        present = {r["procurement_status"] for r in rows}
        order = (CONFIRMED_OPEN, EVIDENCE_FOUND, NOT_VERIFIED, CLOSED)
        return [state for state in order if state in present]

    # --- query building -------------------------------------------------------

    def _trade_evidence_clause(self, filters: OpportunityFilters) -> tuple[str | None, list[Any]]:
        """The clause that scopes results to the active trade's own evidence.

        This is what stops an HVAC directory listing a project with no mechanical activity.
        The requirement comes from `trades.yaml`, so a future plumbing or electrical trade
        declares its own evidence field rather than needing new application code.

        Relaxed when the visitor explicitly asks for a wider set, so a filtered view can still
        reach a record the default discovery view hides.
        """
        if filters.include_unverified or filters.procurement_status:
            return None, []
        field = (self.trade.discovery or {}).get("evidence_field")
        values = (self.trade.discovery or {}).get("evidence_values") or []
        if not field or not values:
            return None, []
        # A column name cannot be bound as a parameter, so it is validated instead. This is
        # the only place an identifier from configuration reaches SQL.
        if not re.fullmatch(r"[a-z_]+", str(field)):
            raise ValueError(f"Invalid discovery evidence_field: {field!r}")
        placeholders = ",".join("?" for _ in values)
        return f"p.{field} IN ({placeholders})", list(values)

    def _base_where(self, filters: OpportunityFilters) -> tuple[str, list[Any]]:
        """Build the WHERE clause for a public listing.

        Returns SQL and parameters separately; every user value is bound, never interpolated.
        """
        clauses: list[str] = ["p.classification IN (?, ?)"]
        params: list[Any] = list(PUBLIC_CLASSIFICATIONS)

        # Discovery excludes closed work; an explicit opt-in includes it with the status shown.
        if filters.include_unverified:
            clauses.append("p.procurement_status IS NOT NULL")
        else:
            clauses.append("p.procurement_status IN (?, ?)")
            params.extend(DISCOVERABLE_PROCUREMENT)

        trade_clause, trade_params = self._trade_evidence_clause(filters)
        if trade_clause:
            clauses.append(trade_clause)
            params.extend(trade_params)

        if filters.city:
            clauses.append("p.city = ?")
            params.append(filters.city)
        if filters.project_type:
            clauses.append("p.project_type = ?")
            params.append(filters.project_type)
        if filters.classification:
            clauses.append("p.classification = ?")
            params.append(filters.classification)
        if filters.procurement_status:
            clauses.append("p.procurement_status = ?")
            params.append(filters.procurement_status)
        if filters.date_from:
            clauses.append("p.permit_date >= ?")
            params.append(filters.date_from)
        if filters.date_to:
            clauses.append("p.permit_date <= ?")
            params.append(filters.date_to)

        return " AND ".join(clauses), params

    def _search_ids(self, term: str) -> list[int] | None:
        """Project ids matching a free-text term, or None when no term was given."""
        query = quote_for_fts(term)
        if not query:
            return None
        try:
            rows = self.db.conn.execute(
                "SELECT project_id FROM project_search WHERE project_search MATCH ? ORDER BY rank",
                (query,),
            ).fetchall()
        except Exception:
            # A malformed FTS expression must degrade to "no results", not a 500.
            return []
        return [int(r["project_id"]) for r in rows]

    # --- listing --------------------------------------------------------------

    def list_opportunities(self, filters: OpportunityFilters | None = None) -> OpportunityPage:
        """Page of opportunities matching the filters, with a total for pagination."""
        filters = (filters or OpportunityFilters()).normalised()
        where, params = self._base_where(filters)

        if filters.q:
            ids = self._search_ids(filters.q)
            if not ids:
                return OpportunityPage(items=[], total=0, page=filters.page,
                                       page_size=filters.page_size, filters=filters)
            placeholders = ",".join("?" for _ in ids)
            where += f" AND p.id IN ({placeholders})"
            params.extend(ids)

        total = int(
            self.db.conn.execute(
                f"SELECT COUNT(*) FROM project p WHERE {where}", params
            ).fetchone()[0]
        )

        order_by = SORT_OPTIONS[filters.sort][1]
        rows = self.db.conn.execute(
            f"""
            SELECT p.*, s.slug
              FROM project p
              LEFT JOIN project_slug s ON s.project_id = p.id
             WHERE {where}
             ORDER BY {order_by}
             LIMIT ? OFFSET ?
            """,
            params + [filters.page_size, filters.offset],
        ).fetchall()

        items = self.annotate_siblings([self.decorate(dict(r)) for r in rows])
        return OpportunityPage(
            items=items, total=total, page=filters.page, page_size=filters.page_size,
            filters=filters,
        )

    def decorate(self, project: dict[str, Any]) -> dict[str, Any]:
        """Add presentation-only fields to a project row.

        These are formatting helpers and relationship flags derived from stored values, never
        new facts. The provenance of every factual field continues to come from `evidence`.
        """
        project["city_slug"] = self.market.city_slug(project.get("city"))
        project["evidence_label"] = self.evidence_label(project)
        project["has_mechanical_evidence"] = project.get("mechanical_evidence_tier") in (1, 2)
        project["last_verified_display"] = self.format_month(project.get("last_verified"))
        project["permit_date_display"] = self.format_date(project.get("permit_date"))
        project["is_closed"] = project.get("procurement_status") == CLOSED
        project["building_key"] = building_key(project.get("address"), project.get("city"))

        # Whether this record would pass the customer-brief criteria. Shown as a badge so the
        # public list and the customer brief cannot tell different stories about a project.
        project["is_report_eligible"] = evaluate(_SqliteRow(project)).eligible
        return project

    def annotate_siblings(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Mark which results share a base address with another result in the same page.

        Two suites of one building are genuinely separate permits, so both belong in a complete
        directory — hiding one would be its own kind of dishonesty. What must not happen is that
        they read as two unrelated buildings. Each is therefore flagged, and the card states the
        relationship, so a visitor cannot mistake one tower's suites for two opportunities.

        This runs per page rather than across the whole result set: a sibling on page 4 is not
        presented next to this one, so there is nothing to disambiguate.
        """
        counts: dict[str, int] = {}
        for item in items:
            key = item.get("building_key")
            if key:
                counts[key] = counts.get(key, 0) + 1
        for item in items:
            key = item.get("building_key")
            item["sibling_count"] = max(0, counts.get(key, 1) - 1) if key else 0
            item["shares_building"] = item["sibling_count"] > 0
        return items

    @staticmethod
    def evidence_label(project: dict[str, Any]) -> str:
        tier = project.get("mechanical_evidence_tier")
        if tier == 1:
            return "Confirmed mechanical permit"
        if tier == 2:
            return "Mechanical scope on record"
        return "No mechanical evidence"

    @staticmethod
    def format_month(value: Any) -> str:
        if not value:
            return "Not verified"
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            text = str(value)
            return text[:7] if len(text) >= 7 else text
        return parsed.strftime("%B %Y")

    @staticmethod
    def format_date(value: Any) -> str:
        if not value:
            return "Not verified"
        try:
            return date.fromisoformat(str(value)[:10]).strftime("%d %b %Y")
        except ValueError:
            return str(value)[:10]

    # --- single opportunity ---------------------------------------------------

    def get_by_slug(self, slug: str) -> dict[str, Any] | None:
        """Retrieve one opportunity by its public slug, with everything the page needs.

        Returns None when the slug is unknown so the caller can render a 404 rather than an
        empty page.
        """
        row = self.db.conn.execute(
            """
            SELECT p.*, s.slug
              FROM project p
              JOIN project_slug s ON s.project_id = p.id
             WHERE s.slug = ?
            """,
            (slug,),
        ).fetchone()
        if row is None:
            return None
        project = self.decorate(dict(row))
        project["permits"] = self.permits_for(project["id"])
        project["sources"] = self.sources_for(project["id"])
        project["evidence"] = self.evidence_for(project["id"])
        project["field_status"] = self.field_status_for(project)
        project["discrepancies"] = self.discrepancies_for(project)
        project["sibling"] = self.sibling_info_for(project)
        return project

    def related_for(self, project: dict[str, Any], limit: int = 4) -> list[dict[str, Any]]:
        """Other discoverable opportunities in the same city.

        Ordered by the same factual rules as the directory. Offers nothing about which is
        "better"; it is simply nearby work in the same city.
        """
        rows = self.db.conn.execute(
            """
            SELECT p.*, s.slug
              FROM project p
              LEFT JOIN project_slug s ON s.project_id = p.id
             WHERE p.city = ?
               AND p.id <> ?
               AND p.classification IN (?, ?)
               AND p.procurement_status IN (?, ?)
             ORDER BY p.permit_date DESC NULLS LAST, p.id
             LIMIT ?
            """,
            (project.get("city"), project["id"])
            + PUBLIC_CLASSIFICATIONS + DISCOVERABLE_PROCUREMENT + (limit,),
        ).fetchall()
        return self.annotate_siblings([self.decorate(dict(r)) for r in rows])

    def sibling_info_for(self, project: dict[str, Any]):
        """Whether other public projects share this base street address.

        Reported rather than merged: a shared street number is a hint that two records belong to
        one building, not proof. The visitor is told the relationship is uncertain.
        """
        key = project.get("building_key")
        if not key:
            from .grouping import SiblingInfo

            return SiblingInfo(building_key=None)
        rows = self.db.conn.execute(
            """
            SELECT id, address, city FROM project
             WHERE classification IN (?, ?)
               AND procurement_status IN (?, ?)
               AND city IS NOT NULL
            """,
            PUBLIC_CLASSIFICATIONS + DISCOVERABLE_PROCUREMENT,
        ).fetchall()
        groups = group_projects(
            rows,
            address_getter=lambda r: r["address"],
            city_getter=lambda r: r["city"],
            id_getter=lambda r: r["id"],
        )
        return sibling_info_for(project["id"], groups, id_getter=lambda r: r["id"])

    def permits_for(self, project_id: int) -> list[dict[str, Any]]:
        rows = self.db.conn.execute(
            """
            SELECT pm.*, s.name AS source_display_name
              FROM permit pm
              JOIN project_permit pp ON pp.permit_id = pm.id
              LEFT JOIN source s ON s.id = pm.source_id
             WHERE pp.project_id = ?
             ORDER BY pm.permit_date, pm.permit_number
            """,
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def sources_for(self, project_id: int) -> list[dict[str, Any]]:
        """Distinct source records behind a project, each with a resolvable URL.

        A customer must be able to open the underlying record. Where a source publishes no
        per-record URL, that is stated rather than hidden.
        """
        rows = self.db.conn.execute(
            """
            SELECT source_name, source_url, source_record_key,
                   MIN(source_date) AS source_date,
                   COUNT(DISTINCT field_name) AS fields_supported
              FROM evidence
             WHERE project_id = ?
             GROUP BY source_name, source_url, source_record_key
             ORDER BY source_name, source_record_key
            """,
            (project_id,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            key = (row["source_name"], row["source_record_key"] or "")
            if key in seen:
                continue
            seen.add(key)
            item = dict(row)
            item["has_url"] = bool(item.get("source_url"))
            out.append(item)
        return out

    def evidence_for(self, project_id: int) -> dict[str, list[dict[str, Any]]]:
        """Evidence grouped by field, which is what the detail page renders."""
        rows = self.db.conn.execute(
            """
            SELECT e.*, s.market_coverage AS coverage
              FROM evidence e
              LEFT JOIN source s ON s.id = e.source_id
             WHERE e.project_id = ?
             ORDER BY e.field_name, e.id
            """,
            (project_id,),
        ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["field_name"], []).append(dict(row))
        return grouped

    def field_status_for(self, project: dict[str, Any]) -> dict[str, str]:
        """Verdict per displayed field, so a template never decides what is verified."""
        from .report_generator import BRIEF_FIELDS
        from .reporting import field_verdict

        out: dict[str, str] = {}
        for name, _label in BRIEF_FIELDS:
            value = project.get(name)
            verdict, _evidence = field_verdict(self.db, project["id"], name, value)
            out[name] = verdict
        return out

    def discrepancies_for(self, project: dict[str, Any]) -> list[dict[str, Any]]:
        import json

        return json.loads(project.get("discrepancies") or "[]")

    # --- statistics -----------------------------------------------------------

    def market_statistics(self) -> dict[str, Any]:
        """Real counts from the database, for the homepage and landing pages.

        Every figure is a count of stored rows. Nothing here is estimated, projected or
        rounded up, because a public statistic the database cannot support is a fabricated one.
        """
        return self.statistics_for(where="1=1", params=[])

    def _evidence_predicate(self, alias: str = "p") -> str:
        """A literal predicate for the trade's evidence, with values inlined.

        Used only for `SUM(CASE WHEN ...)` and similar expressions where a bound parameter
        cannot appear. The values come from the trade profile and are validated integers, so
        inlining them is safe; the column name is validated by `TradeConfig.evidence_field`.
        """
        clause, values = self.trade.evidence_clause(alias)
        if not clause:
            return "0"
        # With no alias the helper still returns a qualified name; drop the leading dot.
        rendered = clause.lstrip(".") if not alias else clause
        for value in values:
            if not isinstance(value, (int, float)):
                raise ValueError(f"Evidence values must be numeric, got {value!r}")
            rendered = rendered.replace("?", str(value), 1)
        return rendered

    def statistics_for(self, *, where: str, params: list[Any]) -> dict[str, Any]:
        """Counts scoped to a subset of projects, plus the market-wide record totals.

        `scoped` applies the caller's WHERE clause and its bound parameters. `global_` runs
        against the whole database and binds nothing, which is what the record totals need —
        binding the scope parameters to a query with no placeholders raises at runtime.

        The evidence predicates come from the trade profile rather than a literal column, so a
        second trade declares its own evidence instead of needing new application code.
        """
        public_suffix = " AND classification IN (?, ?) AND procurement_status IN (?, ?)"
        public_params = list(params) + list(PUBLIC_CLASSIFICATIONS) + list(DISCOVERABLE_PROCUREMENT)

        evidence_clause, evidence_params = self.trade.evidence_clause("p")
        strong_clause, strong_params = self.trade.strong_evidence_clause("p")

        def scoped(sql: str) -> int:
            row = self.db.conn.execute(sql, tuple(params)).fetchone()
            return int(row[0] or 0)

        def public(sql: str, extra: list[Any] | None = None) -> int:
            row = self.db.conn.execute(
                sql, tuple(public_params) + tuple(extra or [])
            ).fetchone()
            return int(row[0] or 0)

        def global_(sql: str) -> int:
            row = self.db.conn.execute(sql).fetchone()
            return int(row[0] or 0)

        public_where = f"{where}{public_suffix}"
        return {
            "projects_total": scoped(f"SELECT COUNT(*) FROM project p WHERE {where}"),
            "projects_public": public(f"SELECT COUNT(*) FROM project p WHERE {public_where}"),
            "high": public(
                f"SELECT COUNT(*) FROM project p WHERE {public_where} AND p.classification = 'HIGH'"
            ),
            "medium": public(
                f"SELECT COUNT(*) FROM project p WHERE {public_where} AND p.classification = 'MEDIUM'"
            ),
            "with_mechanical": (
                public(
                    f"SELECT COUNT(*) FROM project p WHERE {public_where} AND {evidence_clause}",
                    evidence_params,
                )
                if evidence_clause
                else 0
            ),
            "tier1": (
                public(
                    f"SELECT COUNT(*) FROM project p WHERE {public_where} AND {strong_clause}",
                    strong_params,
                )
                if strong_clause
                else 0
            ),
            "cities": public(
                f"SELECT COUNT(DISTINCT p.city) FROM project p WHERE {public_where}"
            ),
            "permit_records": global_("SELECT COUNT(*) FROM permit"),
            "evidence_records": global_("SELECT COUNT(*) FROM evidence"),
        }

    def city_statistics(self) -> list[dict[str, Any]]:
        rows = self.db.conn.execute(
            f"""
            SELECT city, COUNT(*) AS n,
                   SUM(CASE WHEN {self._evidence_predicate("")} THEN 1 ELSE 0 END) AS mech,
                   SUM(CASE WHEN classification='HIGH' THEN 1 ELSE 0 END) AS high
              FROM project
             WHERE classification IN (?, ?) AND procurement_status IN (?, ?)
               AND city IS NOT NULL
             GROUP BY city
             ORDER BY n DESC, city
            """,
            tuple(PUBLIC_CLASSIFICATIONS) + tuple(DISCOVERABLE_PROCUREMENT),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["slug"] = self.market.city_slug(item["city"])
            out.append(item)
        return out

    def type_statistics(self, *, limit: int = 12) -> list[dict[str, Any]]:
        rows = self.db.conn.execute(
            """
            SELECT project_type, COUNT(*) AS n,
                   SUM(CASE WHEN classification='HIGH' THEN 1 ELSE 0 END) AS high
              FROM project
             WHERE classification IN (?, ?) AND procurement_status IN (?, ?)
               AND project_type IS NOT NULL
             GROUP BY project_type
             ORDER BY n DESC, project_type
             LIMIT ?
            """,
            PUBLIC_CLASSIFICATIONS + DISCOVERABLE_PROCUREMENT + (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def data_freshness(self) -> dict[str, Any]:
        """When the underlying data was last collected.

        Public pages state this so a visitor is never led to believe the information is live.
        """
        row = self.db.conn.execute(
            "SELECT MAX(retrieval_date) AS latest, MAX(updated_at) AS updated FROM source_coverage"
        ).fetchone()
        latest = row["latest"] if row else None
        return {
            "retrieval_date": latest,
            "display": self.format_month(latest),
            "sources": self.db.conn.execute(
                """
                SELECT s.name, c.earliest_date, c.latest_date, c.retrieval_date,
                       c.record_count, c.pagination_notes, s.market_coverage
                  FROM source s LEFT JOIN source_coverage c ON c.source_id = s.id
                 ORDER BY COALESCE(c.record_count, 0) DESC, s.name
                """
            ).fetchall(),
        }

    def latest_verified_display(self) -> str:
        return self.format_month(
            self.db.conn.execute("SELECT MAX(last_verified) FROM project").fetchone()[0]
        )

    # --- saved opportunities --------------------------------------------------

    def saved_count(self, user_id: int) -> int:
        return int(
            self.db.conn.execute(
                "SELECT COUNT(*) FROM saved_opportunity WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
        )

    def is_saved(self, user_id: int | None, project_id: int) -> bool:
        if not user_id:
            return False
        return bool(
            self.db.conn.execute(
                "SELECT 1 FROM saved_opportunity WHERE user_id = ? AND project_id = ?",
                (user_id, project_id),
            ).fetchone()
        )


class _SqliteRow:
    """Adapts a plain dict to the row interface `eligibility.evaluate` expects."""

    def __init__(self, data: dict[str, Any]):
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data.get(key)

    def keys(self):
        return self._data.keys()

    def __contains__(self, key: str) -> bool:
        return key in self._data