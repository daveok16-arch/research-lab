"""SEO quality audit.

Produces the internal report that answers, from stored data, whether the SEO layer is doing
what it claims: which keywords have a destination, which pages are indexable, which are
deliberately withheld, and what the funnel looks like.

Everything here is computed from the keyword map, the live routes and the database. Nothing is
estimated, and no ranking position is reported — ranking data has not been measured, so
claiming it would be the same class of error as inventing a project value.
"""

from __future__ import annotations

from typing import Any

from ..config import load_keyword_map, type_slug
from ..db import Database
from .analytics_funnel import funnel_counts, landing_counts
from .content import GUIDES
from .seo_gate import evaluate_gate


def keyword_coverage(db: Database) -> list[dict[str, Any]]:
    """Every mapped keyword, with its intent, destination and role.

    A `deferred` keyword is reported with its reason rather than being pointed at a page that
    cannot answer it — the honest outcome for a phrase this product cannot evidence.
    """
    keyword_map = load_keyword_map()
    rows: list[dict[str, Any]] = []
    for page in keyword_map.pages:
        for entry in page.keywords:
            rows.append(
                {
                    "keyword": entry.phrase,
                    "intent": entry.intent,
                    "role": entry.role,
                    "page_id": page.id,
                    "page_path": page.path if entry.role != "deferred" else None,
                    "explained_on": page.explained_on if entry.role == "deferred" else None,
                    "reason": entry.reason,
                }
            )
    return rows


def technical_seo(db: Database) -> dict[str, Any]:
    """Titles, descriptions, canonicals and the sitemap/robots contract, as data."""
    keyword_map = load_keyword_map()

    duplicates = keyword_map.duplicate_primary_claims()

    # Titles the map declares, checked for uniqueness without rendering every page.
    declared_titles = [p.title for p in keyword_map.pages if p.title]
    duplicate_titles = sorted(
        {t for t in declared_titles if declared_titles.count(t) > 1}
    )

    sitemap_paths = [p.path for p in keyword_map.pages]
    return {
        "pages_in_map": len(keyword_map.pages),
        "keywords_in_map": len(keyword_map.all_keywords()),
        "duplicate_primary_keywords": [
            {"keyword": phrase, "pages": ids} for phrase, ids in duplicates
        ],
        "duplicate_declared_titles": duplicate_titles,
        "canonical_pages_declared": sorted(set(sitemap_paths)),
        "deferred_keywords": [
            {"keyword": k.phrase, "reason": k.reason}
            for k in keyword_map.all_keywords()
            if k.role == "deferred"
        ],
    }


def programmatic_pages(db: Database) -> list[dict[str, Any]]:
    """Every programmatic combination and whether it currently clears its gate.

    This is the report the spec's quality-gate requirement asks for: for each generated page,
    the real counts observed against the configured threshold, and the decision.
    """
    from ..config import active_market, active_trade, load_markets, load_trades, market_by_slug

    keyword_map = load_keyword_map()
    market = active_market()
    trade = active_trade()
    out: list[dict[str, Any]] = []

    public = (
        "classification IN ('HIGH','MEDIUM') "
        "AND procurement_status IN ('Confirmed open','Evidence found, status unclear')"
    )
    clause, values = trade.evidence_clause("")
    evidence = (clause or "").lstrip(".")

    def counts(city: str | None) -> tuple[int, int]:
        where = public + (" AND city = ?" if city else "")
        params: list[Any] = [city] if city else []
        projects = int(
            db.conn.execute(
                f"SELECT COUNT(*) FROM project WHERE {where}", params
            ).fetchone()[0] or 0
        )
        mechanical = 0
        if evidence:
            mechanical = int(
                db.conn.execute(
                    f"SELECT COUNT(*) FROM project WHERE {where} AND {evidence}",
                    params + list(values),
                ).fetchone()[0] or 0
            )
        return projects, mechanical

    # Market x trade.
    for m in load_markets().values():
        if not m.active:
            continue
        for t in load_trades().values():
            if not (t.active and t.id in (m.trades or [])):
                continue
            projects, mechanical = counts(None)
            gate_page = keyword_map.page("market_trade")
            result = evaluate_gate(
                stats={"projects_public": projects, "with_mechanical": mechanical},
                quality_gate=gate_page.quality_gate if gate_page else {},
                page_label=f"{m.short_name} {t.label}",
            )
            out.append(
                {
                    "page": f"/markets/{m.slug}/{t.slug or t.id}",
                    "kind": "market_trade",
                    "curated": True,
                    "indexable": True,
                    "observed": result.observed,
                    "thresholds": result.thresholds,
                    "reason": "curated page: indexation is an editorial decision",
                }
            )
            # City x trade, the genuinely programmatic combination.
            for page in m.landing_pages:
                city = page.get("city")
                if not city:
                    continue
                projects, mechanical = counts(city)
                gate_page = keyword_map.page("city_trade")
                result = evaluate_gate(
                    stats={"projects_public": projects, "with_mechanical": mechanical},
                    quality_gate=gate_page.quality_gate if gate_page else {},
                    page_label=f"{city} {t.label}",
                )
                out.append(
                    {
                        "page": f"/markets/{m.slug}/{page['slug']}/{t.slug or t.id}",
                        "kind": "city_trade",
                        "curated": False,
                        "indexable": result.indexable,
                        "observed": result.observed,
                        "thresholds": result.thresholds,
                        "reason": result.reason,
                    }
                )
    return out


def content_report(db: Database) -> dict[str, Any]:
    """Published guides, and the content topics the map records as not yet written."""
    keyword_map = load_keyword_map()
    published = {g["slug"] for g in GUIDES}
    mapped = {t["slug"]: t for t in keyword_map.guide_topics}
    missing = [slug for slug in mapped if slug not in published]
    unwritten = [
        {"slug": slug, "keyword": mapped[slug].get("primary_keyword")}
        for slug in missing
    ]
    return {
        "published_guides": sorted(published),
        "published_count": len(published),
        "topics_in_map": len(mapped),
        "topics_without_a_guide": unwritten,
    }


def product_seo(db: Database) -> dict[str, Any]:
    """Indexable opportunity count and the generated page inventory."""
    public = (
        "classification IN ('HIGH','MEDIUM') "
        "AND procurement_status IN ('Confirmed open','Evidence found, status unclear')"
    )
    indexable_projects = int(
        db.conn.execute(f"SELECT COUNT(*) FROM project WHERE {public}").fetchone()[0] or 0
    )
    project_types = [
        r["project_type"]
        for r in db.conn.execute(
            f"SELECT DISTINCT project_type FROM project WHERE {public} "
            f"AND project_type IS NOT NULL ORDER BY project_type"
        ).fetchall()
    ]
    return {
        "indexable_opportunities": indexable_projects,
        "project_type_pages": [type_slug(t) for t in project_types if type_slug(t)],
        "project_type_count": len(project_types),
    }


def full_report(db: Database) -> dict[str, Any]:
    """The complete audit, as data a template or a CLI can render."""
    return {
        "keyword_coverage": keyword_coverage(db),
        "technical": technical_seo(db),
        "programmatic_pages": programmatic_pages(db),
        "content": content_report(db),
        "product": product_seo(db),
        "funnel": funnel_counts(db),
        "landings": landing_counts(db),
    }
