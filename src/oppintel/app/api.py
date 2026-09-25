"""Read-mostly JSON API.

This is the machine-readable form of the same service the pages use, so a page and an API
response can never disagree about a project.

Scope is deliberately narrow:

* Only reads, plus saving an opportunity for the signed-in user.
* No ingestion, no administration, no connector or source detail. Those stay behind the CLI,
  which is the operational mechanism.
* Every payload is built from the same decorated project dict the templates render, so the
  "Not verified" rule and the procurement vocabulary are identical in both.

Nothing here exposes a credential, a raw source payload, an internal path, or another user's
data.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from flask import Blueprint, g, jsonify, request

from ..service import DEFAULT_SORT, SORT_OPTIONS, OpportunityFilters

bp = Blueprint("api", __name__, url_prefix="/api")

#: Fields returned for an opportunity in a list. A curated projection rather than the whole
#: row, so a future schema change cannot silently publish a new internal column.
LIST_FIELDS = (
    "id",
    "slug",
    "project_name",
    "address",
    "city",
    "state",
    "project_type",
    "classification",
    "classification_score",
    "mechanical_evidence_tier",
    "mechanical_hvac_evidence",
    "procurement_status",
    "project_status",
    "permit_number",
    "permit_date",
    "estimated_project_value",
    "square_footage",
    "owner",
    "general_contractor",
    "architect",
    "developer",
    "last_verified",
    "source_url",
    "source_name",
    "evidence_label",
    "shares_building",
)

DETAIL_FIELDS = LIST_FIELDS + (
    "property_class",
    "location_precision",
    "disputed_fields",
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _project_payload(project: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """Project a row onto the public field set.

    A missing value is returned as ``null``, never as a placeholder string. A machine-readable
    document that says ``"architect": "Not verified"`` would assert that an architect named
    "Not verified" exists; ``null`` is the honest encoding, and the human pages render the
    marker.
    """
    return {name: _jsonable(project.get(name)) for name in fields}


def _error(message: str, status: int) -> Any:
    return jsonify({"error": message, "status": status}), status


@bp.route("/opportunities")
def list_opportunities() -> Any:
    """List or search opportunities. Mirrors the directory's filters exactly."""
    filters = OpportunityFilters(
        q=request.args.get("q"),
        city=request.args.get("city"),
        project_type=request.args.get("project_type"),
        classification=request.args.get("classification"),
        procurement_status=request.args.get("procurement_status"),
        date_from=request.args.get("date_from"),
        date_to=request.args.get("date_to"),
        include_unverified=request.args.get("include_unverified") in ("1", "true", "on"),
        sort=request.args.get("sort") or DEFAULT_SORT,
        page=_int(request.args.get("page"), 1),
        page_size=_int(request.args.get("page_size"), 20),
    ).normalised()

    result = g.service.list_opportunities(filters)
    return jsonify(
        {
            "market": {"id": g.market.id, "slug": g.market.slug, "name": g.market.name},
            "trade": {"id": g.trade.id, "slug": g.trade.slug, "label": g.trade.label},
            "total": result.total,
            "page": result.page,
            "page_size": result.page_size,
            "total_pages": result.total_pages,
            "sort": filters.sort,
            "filters": {
                "q": filters.q,
                "city": filters.city,
                "project_type": filters.project_type,
                "classification": filters.classification,
                "procurement_status": filters.procurement_status,
                "date_from": filters.date_from,
                "date_to": filters.date_to,
                "include_unverified": filters.include_unverified,
            },
            "results": [
                _project_payload(item, LIST_FIELDS) for item in result.items
            ],
        }
    )


@bp.route("/opportunities/<slug>")
def get_opportunity(slug: str) -> Any:
    """One opportunity, including its sources and per-field verification status."""
    project = g.service.get_by_slug(slug)
    if project is None:
        return _error("Opportunity not found", 404)

    payload = _project_payload(project, DETAIL_FIELDS)
    payload["sources"] = [
        {
            "source_name": src.get("source_name"),
            "source_record_key": src.get("source_record_key"),
            "source_url": src.get("source_url"),
            "source_date": _jsonable(src.get("source_date")),
            "fields_supported": src.get("fields_supported"),
        }
        for src in project.get("sources", [])
    ]
    payload["field_status"] = project.get("field_status", {})
    payload["permits"] = [
        {
            "permit_number": p.get("permit_number"),
            "permit_type": p.get("permit_type"),
            "permit_date": _jsonable(p.get("permit_date")),
            "status": p.get("status"),
            "job_value": p.get("job_value"),
            "square_footage": p.get("square_footage"),
            "work_description": p.get("work_description"),
        }
        for p in project.get("permits", [])
    ]
    payload["discrepancies"] = project.get("discrepancies", [])
    payload["related_record_count"] = (
        project["sibling"].sibling_count if project.get("sibling") else 0
    )
    return jsonify(payload)


@bp.route("/markets")
def list_markets() -> Any:
    from ..config import load_markets

    return jsonify(
        {
            "markets": [
                {
                    "id": m.id,
                    "slug": m.slug,
                    "name": m.name,
                    "short_name": m.short_name,
                    "state": m.state,
                    "active": m.active,
                    "cities": [c.name for c in m.cities],
                    "trades": list(m.trades),
                }
                for m in load_markets().values()
            ]
        }
    )


@bp.route("/trades")
def list_trades() -> Any:
    from ..config import load_trades

    return jsonify(
        {
            "trades": [
                {
                    "id": t.id,
                    "slug": t.slug or t.id,
                    "label": t.label,
                    "short_label": t.short_label,
                    "active": t.active,
                }
                for t in load_trades().values()
            ]
        }
    )


@bp.route("/statistics")
def statistics() -> Any:
    """Market and trade statistics. Every figure is a count of stored rows."""
    stats = g.service.market_statistics()
    return jsonify(
        {
            "market": {"id": g.market.id, "slug": g.market.slug, "name": g.market.name},
            "trade": {"id": g.trade.id, "slug": g.trade.slug, "label": g.trade.label},
            "statistics": stats,
            "cities": g.service.city_statistics(),
            "project_types": g.service.type_statistics(limit=50),
            "data_freshness": {
                "retrieval_date": _jsonable(
                    g.service.data_freshness().get("retrieval_date")
                ),
                "display": g.service.data_freshness().get("display"),
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    )


@bp.route("/saved")
def list_saved() -> Any:
    """The signed-in user's saved opportunities.

    Returns 401 rather than an empty list when unauthenticated, so a client cannot mistake
    "not signed in" for "nothing saved".
    """
    if not g.user:
        return _error("Authentication required", 401)
    items = []
    for project_id in g.accounts.saved_project_ids(g.user.id):
        from ..slugs import slug_for_project

        slug = slug_for_project(g.db, project_id)
        if not slug:
            continue
        project = g.service.get_by_slug(slug)
        if project:
            items.append(_project_payload(project, LIST_FIELDS))
    return jsonify({"total": len(items), "results": items})


@bp.route("/saved/<int:project_id>", methods=["POST", "DELETE"])
def save_opportunity(project_id: int) -> Any:
    if not g.user:
        return _error("Authentication required", 401)
    if request.method == "DELETE":
        g.accounts.unsave_opportunity(g.user.id, project_id)
        return jsonify({"saved": False})
    saved = g.accounts.save_opportunity(g.user.id, project_id)
    return jsonify({"saved": True, "newly_saved": saved})


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default