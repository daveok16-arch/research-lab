"""Architecture tests: market and trade are configuration, not hardcoded.

The requirement is that DFW and HVAC are the first vertical rather than a built-in limitation.
These tests assert that directly: the application reads its market and trade from configuration,
and a different configuration produces a different application without code changes.

They also assert the boundary that keeps the intelligence layer authoritative — that the web
layer does not re-implement classification, eligibility or scoring.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
import yaml

from oppintel.config import (
    MarketConfig,
    TradeConfig,
    active_market,
    active_trade,
    load_markets,
    load_trades,
    market_by_slug,
    trade_by_slug,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# --- configuration is the source of truth --------------------------------------

def test_active_market_comes_from_configuration():
    market = active_market()
    assert isinstance(market, MarketConfig)
    assert market.id == "dfw"
    assert market.slug == "dfw"
    assert market.active is True


def test_active_trade_comes_from_configuration():
    trade = active_trade()
    assert isinstance(trade, TradeConfig)
    assert trade.id == "commercial_hvac"
    assert trade.slug == "commercial-hvac"
    assert trade.active is True


def test_market_declares_its_cities_trades_and_sources():
    market = active_market()
    assert market.cities, "a market must declare its cities"
    assert market.trades, "a market must declare its trades"
    assert market.sources, "a market must declare its sources"
    assert "Dallas" in market.city_names
    assert "Fort Worth" in market.city_names


def test_trade_declares_its_evidence_rules():
    """Discovery scoping is trade configuration, not application code."""
    trade = active_trade()
    assert trade.discovery, "a trade must declare how its evidence is identified"
    assert trade.discovery["evidence_field"] == "mechanical_evidence_tier"
    assert trade.discovery["evidence_values"] == [1, 2]


def test_adding_a_market_is_a_config_change(tmp_path):
    """A second market loads and resolves without touching application code."""
    config = {
        "defaults": {"active_market": "dfw"},
        "markets": [
            {
                "id": "dfw", "slug": "dfw", "name": "Dallas–Fort Worth", "short_name": "DFW",
                "active": True, "trades": ["commercial_hvac"],
                "cities": [{"slug": "dallas", "name": "Dallas"}],
                "sources": ["fort_worth_permits"],
            },
            {
                "id": "houston", "slug": "houston", "name": "Greater Houston",
                "short_name": "Houston", "active": False,
                "trades": ["commercial_hvac"],
                "cities": [{"slug": "houston", "name": "Houston"}],
                "sources": [],
            },
        ],
    }
    path = tmp_path / "markets.yaml"
    path.write_text(yaml.safe_dump(config))

    markets = load_markets(path)
    assert set(markets) == {"dfw", "houston"}
    assert markets["houston"].name == "Greater Houston"
    assert markets["houston"].active is False


def test_market_city_slug_round_trips():
    market = active_market()
    assert market.city_slug("Dallas") == "dallas"
    assert market.city_name("dallas") == "Dallas"
    assert market.city_slug("Fort Worth") == "fort-worth"
    assert market.city_name("fort-worth") == "Fort Worth"


def test_unknown_slug_lookups_return_none():
    assert market_by_slug("atlantis") is None
    assert trade_by_slug("roofing") is None


def test_only_one_market_is_active_in_the_shipped_configuration():
    """The MVP serves one market. More can be added, but only one is live."""
    active = [m for m in load_markets().values() if m.active]
    assert len(active) == 1


def test_only_one_trade_is_active_in_the_shipped_configuration():
    """HVAC is the first vertical, not a limitation — but only one trade is live today."""
    active = [t for t in load_trades().values() if t.active]
    assert len(active) == 1
    assert active[0].id == "commercial_hvac"


def test_inactive_markets_are_configured_but_not_ingested():
    """Planned markets exist so expansion is visibly a config change."""
    markets = load_markets()
    assert "houston" in markets
    assert markets["houston"].sources == [], "a planned market must not claim sources"
    assert markets["houston"].active is False


def test_market_and_trade_have_seo_metadata():
    assert active_market().seo.get("title")
    assert active_trade().seo.get("title")


# --- the application does not hardcode DFW or HVAC -----------------------------

def _app_source() -> str:
    """All application-layer source, concatenated."""
    app_dir = REPO_ROOT / "src" / "oppintel" / "app"
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(app_dir.rglob("*.py"))
    )


def test_application_code_does_not_hardcode_the_market_name():
    """'Dallas' or 'DFW' may appear in prose, never as a matching value."""
    source = _app_source()
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"') or stripped.startswith("'"):
            continue
        # A hardcoded market would show up as a comparison or a literal assignment.
        assert '== "Dallas"' not in line, f"hardcoded market comparison: {line}"
        assert "== 'DFW'" not in line, f"hardcoded market comparison: {line}"
        assert 'market_id = "dfw"' not in line, f"hardcoded market id: {line}"


def test_application_code_does_not_hardcode_the_trade_evidence_field():
    """The evidence field comes from the trade profile, not from a literal in the app."""
    source = _app_source()
    assert 'mechanical_evidence_tier IN' not in source, (
        "the evidence field must be read from the trade profile, not written into a query"
    )


def test_service_reads_the_evidence_field_from_configuration():
    from oppintel.service import OpportunityService

    source = inspect.getsource(OpportunityService._trade_evidence_clause)
    assert "self.trade.discovery" in source


# --- the application does not duplicate intelligence logic ---------------------

def test_application_does_not_implement_classification():
    """Scoring and gate logic must live in the intelligence layer only."""
    source = _app_source()
    for forbidden in (
        "def classify(", "classification_score =", "mechanical_evidence_tier =",
        "def score(", "high_min_score",
    ):
        assert forbidden not in source, f"intelligence logic duplicated in the app: {forbidden}"


def test_application_does_not_read_the_raw_permit_tables_for_listing():
    """Listings must come from assembled projects, not from re-deriving them."""
    from oppintel.service import OpportunityService

    source = inspect.getsource(OpportunityService.list_opportunities)
    assert "FROM project p" in source
    assert "FROM permit" not in source


def test_application_reuses_the_eligibility_module():
    """Eligibility must be the single implementation, shared with the report layer."""
    from oppintel.service import OpportunityService

    source = inspect.getsource(OpportunityService)
    assert "from .eligibility import evaluate" in _app_source() or "evaluate(" in source


def test_application_reuses_the_grouping_module():
    from oppintel.service import OpportunityService

    source = inspect.getsource(OpportunityService)
    assert "building_key" in source


def test_application_does_not_reimplement_procurement_status():
    """Procurement wording is shared with the report layer, not rewritten per page."""
    source = _app_source()
    assert "def procurement_status(" not in source


# --- trade and market scoping in the served application ------------------------

def test_application_serves_the_configured_market_and_trade(client, app_db):
    body = client.get("/").get_data(as_text=True)
    market = active_market()
    trade = active_trade()
    assert market.short_name in body
    assert trade.short_label in body


def test_trade_slug_in_url_matches_configuration(client):
    trade = active_trade()
    assert client.get(f"/trades/{trade.slug}").status_code == 200


def test_market_slug_in_url_matches_configuration(client):
    market = active_market()
    assert client.get(f"/markets/{market.slug}").status_code == 200


def test_configured_landing_pages_are_the_only_city_pages(client):
    """Landing pages are declared, so the site cannot emit a page per database city."""
    market = active_market()
    for page in market.landing_pages:
        assert client.get(f"/markets/{market.slug}/{page['slug']}").status_code == 200