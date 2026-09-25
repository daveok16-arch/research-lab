"""JSON API tests.

The API and the pages read the same service, so these tests focus on the contract a machine
consumer depends on: keys are stable, missing values are null, and nothing internal leaks.
"""

from __future__ import annotations

import json

from oppintel.db import Database


def _get_json(client, path: str):
    response = client.get(path)
    assert response.status_code == 200, path
    assert response.headers["Content-Type"].startswith("application/json")
    return json.loads(response.get_data(as_text=True))


def _slug(app_db, address_fragment: str) -> str:
    db = Database(app_db.config["APP_CONFIG"].database_path)
    try:
        row = db.conn.execute(
            """
            SELECT s.slug FROM project p JOIN project_slug s ON s.project_id = p.id
             WHERE p.address LIKE ? LIMIT 1
            """,
            (f"{address_fragment}%",),
        ).fetchone()
        assert row
        return row["slug"]
    finally:
        db.close()


# --- listing -------------------------------------------------------------------

def test_list_returns_a_paginated_envelope(client):
    payload = _get_json(client, "/api/opportunities")
    for key in ("market", "trade", "total", "page", "page_size", "total_pages",
                "sort", "filters", "results"):
        assert key in payload, key


def test_list_identifies_the_served_market_and_trade(client):
    payload = _get_json(client, "/api/opportunities")
    assert payload["market"]["id"] == "dfw"
    assert payload["trade"]["id"] == "commercial_hvac"


def test_list_excludes_completed_and_service_records(client):
    payload = _get_json(client, "/api/opportunities?page_size=50")
    addresses = {item["address"] for item in payload["results"]}
    assert "20 DONE ST" not in addresses
    assert "30 SERVICE ST" not in addresses


def test_list_excludes_records_without_trade_evidence(client):
    """The trade scoping applies to the API exactly as it does to the pages."""
    payload = _get_json(client, "/api/opportunities?page_size=50")
    for item in payload["results"]:
        assert item["mechanical_evidence_tier"] in (1, 2), item["address"]


def test_missing_values_are_null_not_placeholder_text(client):
    """A machine document must not assert that an architect named 'Not verified' exists."""
    payload = _get_json(client, "/api/opportunities?page_size=50")
    for item in payload["results"]:
        for field in ("architect", "developer", "owner", "general_contractor"):
            value = item[field]
            assert value is None or isinstance(value, str)
            assert value != "Not verified", field


def test_list_pagination_is_bounded(client):
    payload = _get_json(client, "/api/opportunities?page_size=100000")
    assert payload["page_size"] <= 50


def test_list_filters_by_city(client):
    payload = _get_json(client, "/api/opportunities?city=Fort Worth&page_size=50")
    for item in payload["results"]:
        assert item["city"] == "Fort Worth"


def test_list_filters_by_classification(client):
    payload = _get_json(client, "/api/opportunities?classification=HIGH&page_size=50")
    for item in payload["results"]:
        assert item["classification"] == "HIGH"


def test_list_search(client):
    payload = _get_json(client, "/api/opportunities?q=ross")
    assert payload["total"] >= 1
    assert any("ROSS" in (i["address"] or "") for i in payload["results"])


def test_list_invalid_sort_falls_back(client):
    payload = _get_json(client, "/api/opportunities?sort=profit")
    assert payload["sort"] == "recent"


def test_list_rejects_or_ignores_bad_pages(client):
    payload = _get_json(client, "/api/opportunities?page=abc")
    assert payload["page"] == 1


# --- detail --------------------------------------------------------------------

def test_detail_returns_the_expected_shape(client, app_db):
    payload = _get_json(client, f"/api/opportunities/{_slug(app_db, '10 ROSS')}")
    for key in ("id", "address", "sources", "permits", "field_status", "discrepancies"):
        assert key in payload, key


def test_detail_includes_source_records(client, app_db):
    payload = _get_json(client, f"/api/opportunities/{_slug(app_db, '10 ROSS')}")
    assert payload["sources"]
    assert payload["sources"][0]["source_name"]


def test_detail_reports_field_verification_status(client, app_db):
    """A consumer must be able to tell a verified field from an unverified one."""
    payload = _get_json(client, f"/api/opportunities/{_slug(app_db, '10 ROSS')}")
    status = payload["field_status"]
    assert status.get("address") in ("CONFIRMED", "PARTIALLY VERIFIED", "NOT VERIFIED")
    assert status.get("architect") == "NOT VERIFIED"


def test_unknown_slug_returns_404_json(client):
    response = client.get("/api/opportunities/nope")
    assert response.status_code == 404
    assert json.loads(response.get_data(as_text=True))["status"] == 404


# --- statistics ----------------------------------------------------------------

def test_statistics_are_real_counts(client):
    payload = _get_json(client, "/api/statistics")
    stats = payload["statistics"]
    for key in ("projects_total", "projects_public", "high", "medium",
                "with_mechanical", "tier1", "cities"):
        assert isinstance(stats[key], int), key
    assert stats["projects_public"] >= stats["with_mechanical"]


def test_statistics_include_freshness(client):
    payload = _get_json(client, "/api/statistics")
    assert "data_freshness" in payload
    assert payload["generated_at"]


def test_statistics_include_city_and_type_breakdowns(client):
    payload = _get_json(client, "/api/statistics")
    assert isinstance(payload["cities"], list)
    assert isinstance(payload["project_types"], list)


# --- configuration exposure ----------------------------------------------------

def test_markets_endpoint_lists_active_and_planned(client):
    payload = _get_json(client, "/api/markets")
    ids = {m["id"] for m in payload["markets"]}
    assert {"dfw", "houston"}.issubset(ids)
    dfw = next(m for m in payload["markets"] if m["id"] == "dfw")
    assert dfw["active"] is True
    assert dfw["cities"]


def test_trades_endpoint_lists_the_active_trade(client):
    payload = _get_json(client, "/api/trades")
    hvac = next(t for t in payload["trades"] if t["id"] == "commercial_hvac")
    assert hvac["active"] is True


# --- saved opportunities -------------------------------------------------------

def test_saved_requires_authentication(client):
    response = client.get("/api/saved")
    assert response.status_code == 401


def test_saved_returns_the_users_opportunities(session_client, app_db):
    db = Database(app_db.config["APP_CONFIG"].database_path)
    try:
        project_id = db.conn.execute(
            "SELECT id FROM project WHERE address LIKE '10 ROSS%' LIMIT 1"
        ).fetchone()["id"]
    finally:
        db.close()

    session_client.post(f"/api/saved/{project_id}")
    payload = json.loads(session_client.get("/api/saved").get_data(as_text=True))
    assert payload["total"] == 1
    assert payload["results"][0]["address"].startswith("10 ROSS")


def test_saved_can_be_removed_via_delete(session_client, app_db):
    db = Database(app_db.config["APP_CONFIG"].database_path)
    try:
        project_id = db.conn.execute(
            "SELECT id FROM project WHERE address LIKE '10 ROSS%' LIMIT 1"
        ).fetchone()["id"]
    finally:
        db.close()

    session_client.post(f"/api/saved/{project_id}")
    session_client.delete(f"/api/saved/{project_id}")
    payload = json.loads(session_client.get("/api/saved").get_data(as_text=True))
    assert payload["total"] == 0


# --- nothing internal leaks ----------------------------------------------------

def test_api_exposes_no_operational_detail(client, app_db):
    """No credentials, no paths, no internal scoring internals, no raw payloads."""
    bodies = [
        client.get("/api/opportunities?page_size=5").get_data(as_text=True),
        client.get(f"/api/opportunities/{_slug(app_db, '10 ROSS')}").get_data(as_text=True),
        client.get("/api/statistics").get_data(as_text=True),
    ]
    for body in bodies:
        for forbidden in (
            "password", "secret", "token", "sqlite", "oppintel.db",
            "classification_reasons", "project_key", "raw_record",
            "__VIEWSTATE", "connector",
        ):
            assert forbidden not in body, f"internal detail leaked: {forbidden}"


def test_api_has_no_write_endpoint_for_ingestion(client):
    """Ingestion and administration stay behind the CLI."""
    for path, method in (
        ("/api/ingest", "post"),
        ("/api/admin", "post"),
        ("/api/sources", "post"),
    ):
        response = getattr(client, method)(path)
        assert response.status_code in (404, 405), path
