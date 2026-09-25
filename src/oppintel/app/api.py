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
from .workflow import WorkflowError

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
        min_value=request.args.get("min_value"),
        max_value=request.args.get("max_value"),
        mechanical_only=request.args.get("mechanical_only") in ("1", "true", "on"),
        freshness_days=_int(request.args.get("freshness_days"), 0) or None,
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
                "min_value": filters.min_value,
                "max_value": filters.max_value,
                "mechanical_only": filters.mechanical_only,
                "freshness_days": filters.freshness_days,
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
    # Recorded changes only. An empty list means the system has detected no change, which the
    # client can state plainly rather than treating as missing data.
    payload["changes"] = [
        {
            "field_name": change["field_name"],
            "change_kind": change["change_kind"],
            "summary": change["summary"],
            "previous_value": change.get("previous_value"),
            "current_value": change.get("current_value"),
            "source_name": change.get("source_name"),
            "source_url": change.get("source_url"),
            "detected_at": _jsonable(change["detected_at"]),
        }
        for change in g.service.changes_for(project["id"], limit=25)
    ]
    # Why the project is relevant, from the same module the pages use.
    from .matching import evaluate_match

    prefs = g.accounts.get_preferences(g.user.id) if g.user else {}
    payload["match_reasons"] = [
        {"kind": r.kind, "label": r.label, "detail": r.detail}
        for r in evaluate_match(
            project, trade=g.trade, market=g.market, preferences=prefs
        ).reasons
    ]
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
    items = g.service.projects_by_ids(g.accounts.saved_project_ids(g.user.id))
    return jsonify({"total": len(items), "results": [_project_payload(p, LIST_FIELDS) for p in items]})


@bp.route("/saved/<int:project_id>", methods=["POST", "DELETE"])
def save_opportunity(project_id: int) -> Any:
    if not g.user:
        return _error("Authentication required", 401)
    if request.method == "DELETE":
        g.accounts.unsave_opportunity(g.user.id, project_id)
        return jsonify({"saved": False})
    saved = g.accounts.save_opportunity(g.user.id, project_id)
    return jsonify({"saved": True, "newly_saved": saved})


# =====================================================================
# Watching, pipeline, notes and alerts
#
# The machine-readable form of the account workspace. Every write is scoped to the
# authenticated user, so a project id from another account is simply not reachable.
# =====================================================================


@bp.route("/watching")
def list_watching() -> Any:
    if not g.user:
        return _error("Authentication required", 401)
    items = g.service.projects_by_ids(g.workflow.watched_ids(g.user.id))
    return jsonify(
        {"total": len(items), "results": [_project_payload(p, LIST_FIELDS) for p in items]}
    )


@bp.route("/watching/<int:project_id>", methods=["POST", "DELETE"])
def watch_opportunity(project_id: int) -> Any:
    if not g.user:
        return _error("Authentication required", 401)
    if request.method == "DELETE":
        g.workflow.unwatch(g.user.id, project_id)
        return jsonify({"watching": False})
    created = g.workflow.watch(g.user.id, project_id)
    return jsonify({"watching": True, "newly_watched": created})


@bp.route("/pipeline")
def list_pipeline() -> Any:
    """The account's own workflow entries, with the stage and follow-up date."""
    if not g.user:
        return _error("Authentication required", 401)
    rows = g.workflow.pipeline_rows(g.user.id)
    stage_by_id = {int(r["project_id"]): r for r in rows}
    projects = g.service.projects_by_ids(list(stage_by_id))
    results = []
    for project in projects:
        payload = _project_payload(project, LIST_FIELDS)
        row = stage_by_id.get(int(project["id"]), {})
        payload["stage"] = row.get("stage")
        payload["follow_up_date"] = _jsonable(row.get("follow_up_date"))
        payload["assigned_to"] = row.get("assigned_to")
        results.append(payload)
    return jsonify(
        {"total": len(results), "counts": g.workflow.pipeline_counts(g.user.id), "results": results}
    )


@bp.route("/pipeline/<int:project_id>", methods=["POST", "DELETE"])
def update_pipeline(project_id: int) -> Any:
    if not g.user:
        return _error("Authentication required", 401)
    if request.method == "DELETE":
        g.workflow.remove_from_pipeline(g.user.id, project_id)
        return jsonify({"in_pipeline": False})
    payload = request.get_json(silent=True) or {}
    stage = payload.get("stage") or request.form.get("stage") or "NEW"
    try:
        g.workflow.set_stage(
            g.user.id, project_id, stage,
            follow_up_date=payload.get("follow_up_date") or request.form.get("follow_up_date"),
        )
    except WorkflowError as exc:
        return _error(str(exc), 400)
    return jsonify({"in_pipeline": True, "stage": g.workflow.stage_for(g.user.id, project_id)})


@bp.route("/notes/<int:project_id>")
def list_notes(project_id: int) -> Any:
    if not g.user:
        return _error("Authentication required", 401)
    notes = g.workflow.notes_for(g.user.id, project_id)
    return jsonify(
        {
            "total": len(notes),
            "notes": [
                {"id": n["id"], "body": n["body"], "created_at": _jsonable(n["created_at"])}
                for n in notes
            ],
        }
    )


@bp.route("/notes/<int:project_id>", methods=["POST"])
def add_note(project_id: int) -> Any:
    if not g.user:
        return _error("Authentication required", 401)
    payload = request.get_json(silent=True) or {}
    body = payload.get("body") or request.form.get("body", "")
    try:
        note_id = g.workflow.add_note(g.user.id, project_id, body)
    except WorkflowError as exc:
        return _error(str(exc), 400)
    return jsonify({"id": note_id}), 201


@bp.route("/alerts")
def list_alerts() -> Any:
    """Event-driven alerts. Each carries the change that caused it, when there was one."""
    if not g.user:
        return _error("Authentication required", 401)
    unread_only = request.args.get("unread") in ("1", "true", "on")
    items = g.alerts.for_user(g.user.id, unread_only=unread_only)
    return jsonify(
        {
            "unread": g.alerts.unread_count(g.user.id),
            "total": len(items),
            "alerts": [
                {
                    "id": a["id"],
                    "kind": a["kind"],
                    "title": a["title"],
                    "body": a["body"],
                    "project_id": a["project_id"],
                    "slug": a.get("slug"),
                    "previous_value": a.get("previous_value"),
                    "current_value": a.get("current_value"),
                    "source_name": a.get("source_name"),
                    "source_url": a.get("source_url"),
                    "created_at": _jsonable(a["created_at"]),
                    "read_at": _jsonable(a.get("read_at")),
                }
                for a in items
            ],
        }
    )


@bp.route("/alerts/<int:alert_id>/read", methods=["POST"])
def read_alert(alert_id: int) -> Any:
    if not g.user:
        return _error("Authentication required", 401)
    g.alerts.mark_read(g.user.id, alert_id)
    return jsonify({"unread": g.alerts.unread_count(g.user.id)})


@bp.route("/me")
def whoami() -> Any:
    """The caller's identity and resolved entitlements.

    Reports what the stored subscription and access level actually grant, so a client cannot
    infer a paid capability it does not hold.
    """
    entitlement = g.entitlement
    return jsonify(
        {
            "authenticated": bool(g.user),
            "user": (
                {"display_name": g.user.display, "access_level": g.user.access_level}
                if g.user
                else None
            ),
            "entitlement": {
                "plan": entitlement.plan_id,
                "plan_name": entitlement.plan_name,
                "status": entitlement.status,
                "features": sorted(entitlement.features),
                "is_paid": entitlement.is_paid,
            },
        }
    )


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default