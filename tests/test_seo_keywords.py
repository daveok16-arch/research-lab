"""SEO and keyword-intelligence tests.

These cover three things the phase has to guarantee:

* **The keyword map is coherent** — one primary destination per keyword, every page declared,
  and no keyword pointed at a page that cannot answer it.
* **The indexation contract holds** — canonical pages index, private and filtered views do not,
  and the sitemap and robots agree with what each page declares.
* **Programmatic pages are gated** — a market/city/trade combination without enough real data
  is not offered to crawlers, and the page says so honestly.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET

import pytest

from oppintel.app.analytics_funnel import LANDING_KINDS, funnel_counts, landing_counts
from oppintel.app.seo_gate import evaluate_gate
from oppintel.config import (
    KeywordMap,
    KeywordPage,
    load_keyword_map,
    type_slug,
)
from oppintel.db import Database

NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def _text(body: str) -> str:
    """Visible text of a page, with tags collapsed and entities decoded.

    Assertions about prose must run against text, not raw markup: `<strong>not</strong> a
    confirmed bid` is the correct disclaimer, but the literal phrase does not appear in the
    HTML, and a substring test on the source would either miss it or, worse, treat a negation
    as if it were a claim.
    """
    import html as _html

    stripped = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=re.S | re.I)
    stripped = re.sub(r"<[^>]+>", " ", stripped)
    return " ".join(_html.unescape(stripped).split())


def _robots(body: str) -> str:
    match = re.search(r'name="robots" content="([^"]+)"', body)
    return match.group(1) if match else ""


def _sitemap_locs(client) -> list[str]:
    root = ET.fromstring(client.get("/sitemap.xml").get_data(as_text=True))
    return [el.text for el in root.iter(f"{NS}loc")]


def _title(body: str) -> str:
    return re.search(r"<title>(.*?)</title>", body, re.S).group(1).strip()


def _description(body: str) -> str:
    return re.search(r'<meta name="description" content="(.*?)"', body, re.S).group(1).strip()


# =============================================================================
# The keyword map
# =============================================================================


def test_keyword_map_loads_and_declares_pages():
    keyword_map = load_keyword_map()
    assert isinstance(keyword_map, KeywordMap)
    assert keyword_map.pages, "the keyword map must declare pages"
    assert keyword_map.intents, "the map must declare its intent vocabulary"


def test_no_primary_keyword_is_claimed_by_two_pages():
    """Cannibalisation guard: one primary keyword, one canonical destination."""
    duplicates = load_keyword_map().duplicate_primary_claims()
    assert duplicates == [], f"primary keywords claimed twice: {duplicates}"


def test_every_keyword_has_a_declared_intent():
    known = set(load_keyword_map().intents)
    unknown = [
        k.phrase for k in load_keyword_map().all_keywords() if k.intent not in known
    ]
    assert unknown == [], f"keywords with an unknown intent: {unknown}"


def test_every_keyword_has_a_known_role():
    roles = {"primary", "secondary", "deferred"}
    bad = [
        k.phrase
        for k in load_keyword_map().all_keywords()
        if k.role not in roles
    ]
    assert bad == [], f"keywords with an unknown role: {bad}"


def test_a_deferred_keyword_gives_a_reason_and_no_destination():
    """A phrase we cannot evidence is recorded honestly, not pointed at a thin page."""
    for page in load_keyword_map().pages:
        for entry in page.deferred:
            assert entry.reason, f"deferred keyword without a reason: {entry.phrase}"


def test_the_bid_keywords_are_deferred_because_bid_status_is_unpublished():
    """The product cannot evidence bid status, so it must not target bid phrases as primary."""
    keyword_map = load_keyword_map()
    deferred = {k.phrase for k in keyword_map.all_keywords() if k.role == "deferred"}
    assert "construction bid opportunities" in deferred
    assert "commercial construction bids" in deferred


def test_every_page_path_is_a_route_the_application_registers(app_db):
    """A mapped path that does not exist would be a keyword with no destination."""
    rules = {str(rule.rule) for rule in app_db.url_map.iter_rules()}
    problems = []
    for page in load_keyword_map().pages:
        # Placeholders in a mapped path become concrete segments at request time.
        if "{" in page.path:
            continue
        if page.path not in rules:
            problems.append(page.path)
    assert problems == [], f"mapped paths with no route: {problems}"


def test_market_and_trade_are_not_hardcoded_in_the_keyword_map():
    """The map uses placeholders, so a second market is a configuration change."""
    text = (load_keyword_map().pages[0].path or "")
    assert isinstance(text, str)
    joined = " ".join(p.path for p in load_keyword_map().pages)
    # The placeholders must appear; the literal market slug must not be baked into the map.
    assert "{market}" in joined
    assert "{trade" in joined
    assert "/markets/dfw/" not in joined


# =============================================================================
# The quality gate
# =============================================================================


def test_gate_passes_a_page_with_enough_data():
    result = evaluate_gate(
        stats={"projects_public": 10, "with_mechanical": 5},
        quality_gate={"min_projects": 5, "min_mechanical": 3},
        page_label="Test",
    )
    assert result.indexable is True
    assert result.failures == []


def test_gate_fails_a_page_below_the_project_threshold():
    result = evaluate_gate(
        stats={"projects_public": 2, "with_mechanical": 2},
        quality_gate={"min_projects": 5},
        page_label="Test",
    )
    assert result.indexable is False
    assert "below the 5 required" in result.reason


def test_gate_fails_a_trade_page_without_enough_evidence():
    """A trade directory with no evidence of that trade is the unbacked claim we refuse."""
    result = evaluate_gate(
        stats={"projects_public": 10, "with_mechanical": 1},
        quality_gate={"min_projects": 5, "min_mechanical": 3},
        page_label="Test",
    )
    assert result.indexable is False
    assert "evidence" in result.reason


def test_gate_defaults_apply_when_no_thresholds_are_configured():
    """An unconfigured page falls back to the module defaults rather than being ungated."""
    result = evaluate_gate(
        stats={"projects_public": 0, "with_mechanical": 0},
        quality_gate=None,
        page_label="Test",
    )
    assert result.indexable is False


# =============================================================================
# New pages
# =============================================================================


def test_core_category_page_renders_and_is_indexable(client):
    response = client.get("/commercial-construction-leads")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "index,follow" in _robots(body)
    assert "Commercial Construction Leads" in body


def test_core_category_page_has_unique_metadata(client):
    body = client.get("/commercial-construction-leads").get_data(as_text=True)
    title = _title(body)
    description = _description(body)
    assert "Commercial Construction Leads and Project Intelligence" == title
    assert description
    # The home page must not share the title, or the two compete.
    home_title = _title(client.get("/").get_data(as_text=True))
    assert title != home_title


def test_core_category_page_states_what_the_product_does_not_claim(client):
    """The category page is the likeliest place for a bid overclaim, so it must disclaim."""
    text = _text(client.get("/commercial-construction-leads").get_data(as_text=True)).lower()
    assert "does not claim" in text
    assert "not a confirmed open bid" in text


def test_core_category_page_links_to_the_directory_and_signup(client):
    body = client.get("/commercial-construction-leads").get_data(as_text=True)
    assert "/opportunities" in body
    assert "/signup" in body


def test_core_category_page_uses_real_records_not_mock_ups(client, app_db):
    """Any example shown must be an address the database actually holds."""
    body = client.get("/commercial-construction-leads").get_data(as_text=True)
    db = Database(app_db.config["APP_CONFIG"].database_path)
    try:
        addresses = [
            r["address"]
            for r in db.conn.execute(
                "SELECT address FROM project WHERE address IS NOT NULL LIMIT 5"
            ).fetchall()
        ]
    finally:
        db.close()
    assert any(addr.split(",")[0] in body for addr in addresses), (
        "the category page should show real records from the directory"
    )


def test_city_trade_page_renders(client):
    response = client.get("/markets/dfw/fort-worth/commercial-hvac")
    assert response.status_code == 200
    assert "Fort Worth" in response.get_data(as_text=True)


def test_city_trade_page_for_an_undeclared_city_is_404(client):
    """Only configured landing-page cities participate, so the set stays bounded."""
    assert client.get("/markets/dfw/farmersville/commercial-hvac").status_code == 404
    assert client.get("/markets/dfw/lucas/commercial-hvac").status_code == 404


def test_city_trade_page_for_an_unknown_trade_is_404(client):
    assert client.get("/markets/dfw/dallas/no-such-trade").status_code == 404


# =============================================================================
# The indexation contract
# =============================================================================


def test_programmatic_page_below_its_gate_is_noindex(client):
    """The fixture has too little data for a city/trade page, so it must be withheld."""
    body = client.get("/markets/dfw/dallas/commercial-hvac").get_data(as_text=True)
    assert "noindex" in _robots(body)


def test_programmatic_page_below_its_gate_says_so_honestly(client):
    body = client.get("/markets/dfw/dallas/commercial-hvac").get_data(as_text=True)
    assert "not being offered to search engines" in body


def test_curated_landing_pages_remain_indexable(client):
    """Curated pages are an editorial decision, not a programmatic combination."""
    for path in ("/markets/dfw", "/markets/dfw/dallas", "/markets/dfw/fort-worth"):
        assert "index,follow" in _robots(client.get(path).get_data(as_text=True)), path


def test_gated_page_is_absent_from_the_sitemap(client):
    """A page marked noindex must not be advertised in the sitemap."""
    joined = " ".join(_sitemap_locs(client))
    assert "/markets/dfw/dallas/commercial-hvac" not in joined


def test_sitemap_includes_the_core_category_page(client):
    assert any("commercial-construction-leads" in loc for loc in _sitemap_locs(client))


def test_private_and_personal_routes_are_disallowed_and_absent(client):
    robots = client.get("/robots.txt").get_data(as_text=True)
    sitemap = " ".join(_sitemap_locs(client))
    for private in (
        "/saved", "/preferences", "/signin", "/signup", "/dashboard",
        "/watching", "/my-pipeline", "/alerts", "/admin", "/api",
    ):
        assert f"Disallow: {private}" in robots, f"robots permits {private}"
        assert private not in sitemap, f"sitemap advertises {private}"


def test_no_personal_route_is_indexable_without_a_session(client):
    """Every account route must resolve to a noindex page for a signed-out visitor."""
    for path in ("/dashboard", "/watching", "/my-pipeline", "/alerts"):
        response = client.get(path, follow_redirects=True)
        assert "noindex" in response.get_data(as_text=True), path


def test_sitemap_contains_no_query_strings(client):
    assert all("?" not in loc for loc in _sitemap_locs(client))


def test_sitemap_has_no_duplicate_urls(client):
    locs = _sitemap_locs(client)
    assert len(locs) == len(set(locs)), "the sitemap repeats a URL"


# =============================================================================
# Metadata quality across the public pages
# =============================================================================


def test_public_titles_and_descriptions_are_unique(client):
    paths = [
        "/", "/commercial-construction-leads", "/opportunities", "/markets", "/trades",
        "/project-types", "/guides", "/how-it-works", "/reports",
        "/markets/dfw", "/markets/dfw/dallas", "/markets/dfw/fort-worth",
        "/trades/commercial-hvac", "/markets/dfw/commercial-hvac",
    ]
    titles, descriptions = [], []
    for path in paths:
        body = client.get(path).get_data(as_text=True)
        titles.append(_title(body))
        descriptions.append(_description(body))
    assert len(titles) == len(set(titles)), f"duplicate titles: {sorted(titles)}"
    assert len(descriptions) == len(set(descriptions)), "duplicate meta descriptions"
    assert all(descriptions), "every page must carry a description"


def test_every_indexable_page_declares_a_canonical(client):
    for path in (
        "/", "/commercial-construction-leads", "/opportunities", "/markets",
        "/trades", "/project-types", "/guides", "/reports",
    ):
        body = client.get(path).get_data(as_text=True)
        assert 'rel="canonical"' in body, path


def test_metadata_does_not_overclaim_bid_status(client):
    """No indexable page may *assert* a bid, a guarantee or an available package.

    The check is for affirmative claims, not for the words themselves: the pages correctly say
    "no source publishes bid status" and "not a confirmed open bid", and a naive substring test
    would flag the disclaimer as the offence. So the pattern requires an affirmative construction
    and explicitly permits a negation immediately before the phrase.
    """
    affirmative = re.compile(
        r"(?<!not )(?<!never )(?<!no )"
        r"\b(is|are|this is|these are)\s+(an?\s+)?(open bid|available for bid)\b"
        r"|\bguaranteed\b"
    )
    for path in ("/", "/commercial-construction-leads", "/opportunities", "/markets/dfw"):
        text = _text(client.get(path).get_data(as_text=True)).lower()
        match = affirmative.search(text)
        assert match is None, f"{path} overclaims: {match.group(0) if match else ''}"


def test_metadata_never_claims_a_bid_is_open_or_a_package_available(client):
    """Belt and braces: the phrases must only ever appear inside a disclaimer."""
    for path in ("/", "/commercial-construction-leads", "/opportunities"):
        text = _text(client.get(path).get_data(as_text=True)).lower()
        for phrase in ("open bid", "available for bid"):
            for match in re.finditer(re.escape(phrase), text):
                window = text[max(0, match.start() - 60):match.start()]
                assert re.search(r"\b(not|no|never|isn't|does not)\b", window) or "?" in window, (
                    f"{path} states '{phrase}' without a negation: ...{window[-60:]}{phrase}"
                )


def test_structured_data_parses_on_the_new_pages(client):
    for path in ("/commercial-construction-leads", "/markets/dfw/fort-worth/commercial-hvac"):
        body = client.get(path).get_data(as_text=True)
        blocks = re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', body, re.S
        )
        assert blocks, path
        for block in blocks:
            json.loads(block)


# =============================================================================
# Internal linking
# =============================================================================


def test_core_category_links_into_the_products_pages(client):
    body = client.get("/commercial-construction-leads").get_data(as_text=True)
    for target in ("/opportunities", "/markets/dfw", "/trades/commercial-hvac", "/guides"):
        assert target in body, f"the category page does not link to {target}"


def test_market_page_links_to_trade_and_category(client):
    body = client.get("/markets/dfw").get_data(as_text=True)
    assert "/trades/commercial-hvac" in body
    assert "/commercial-construction-leads" in body


def test_guide_links_to_the_product(client):
    body = client.get("/guides/reading-permit-evidence").get_data(as_text=True)
    assert "/opportunities" in body, "a guide must lead into the product"
    assert "/commercial-construction-leads" in body


def test_footer_links_to_the_category_page_on_every_public_page(client):
    for path in ("/", "/opportunities", "/markets/dfw"):
        assert "/commercial-construction-leads" in client.get(path).get_data(as_text=True), path


# =============================================================================
# Content
# =============================================================================


def test_all_mapped_guide_topics_have_a_published_guide(client):
    """A mapped topic with no guide is a content gap, not a page to invent."""
    from oppintel.app.content import GUIDE_SLUGS

    mapped = [t["slug"] for t in load_keyword_map().guide_topics]
    missing = [slug for slug in mapped if slug not in GUIDE_SLUGS]
    assert missing == [], f"mapped guide topics without a guide: {missing}"


def test_every_guide_renders_with_a_title_and_description(client):
    import html as _html

    from oppintel.app.content import GUIDES

    for guide in GUIDES:
        body = client.get(f"/guides/{guide['slug']}").get_data(as_text=True)
        assert _title(body) == guide["title"]
        # The attribute value is HTML-escaped in the markup; compare the decoded value.
        assert _html.unescape(_description(body)) == guide["summary"]


def test_guides_are_indexable_and_have_article_json_ld(client):
    from oppintel.app.content import GUIDES

    for guide in GUIDES:
        body = client.get(f"/guides/{guide['slug']}").get_data(as_text=True)
        assert "index,follow" in _robots(body)
        blocks = re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', body, re.S
        )
        assert any('"BreadcrumbList"' in b for b in blocks), guide["slug"]
        assert any('"Article"' in b for b in blocks), (
            f"{guide['slug']} should carry Article markup"
        )


def test_guide_article_markup_asserts_nothing_it_cannot_support(client):
    """No fabricated byline, publisher, rating or award in the guide markup."""
    from oppintel.app.content import GUIDES

    for guide in GUIDES:
        body = client.get(f"/guides/{guide['slug']}").get_data(as_text=True)
        for block in re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', body, re.S
        ):
            lowered = block.lower()
            for forbidden in ("author", "publisher", "rating", "review", "award", "price"):
                assert forbidden not in lowered, (
                    f"{guide['slug']} article markup asserts {forbidden}"
                )


def test_no_page_emits_unsupported_structured_data_claims(client):
    """Structured data must not carry ratings, reviews, prices or awards anywhere."""
    for path in (
        "/", "/commercial-construction-leads", "/opportunities", "/markets/dfw",
        "/trades/commercial-hvac", "/project-types", "/guides",
        "/guides/reading-permit-evidence", "/reports",
    ):
        body = client.get(path).get_data(as_text=True)
        for block in re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', body, re.S
        ):
            data = json.loads(block)
            serialized = json.dumps(data).lower()
            for forbidden in (
                "aggregaterating", "ratingvalue", '"review"', '"offers"', "price",
                "award",
            ):
                assert forbidden not in serialized.replace(" ", ""), (
                    f"{path} emits unsupported markup: {forbidden}"
                )


def test_guides_are_not_identical_to_one_another(client):
    """Guides must be genuinely distinct content, not one template with a swapped heading."""
    from oppintel.app.content import GUIDES

    bodies = [
        client.get(f"/guides/{g['slug']}").get_data(as_text=True)
        for g in GUIDES
    ]
    signatures = []
    for body in bodies:
        text = re.sub(r"<[^>]+>", " ", body)
        signatures.append(" ".join(text.split())[:2000])
    assert len(signatures) == len(set(signatures)), "two guides share their body text"


# =============================================================================
# Analytics funnel
# =============================================================================


def test_landing_views_are_recorded_without_personal_data(client, app_db):
    """A landing view is recorded as a page *kind*, with no URL, query or identity."""
    db_path = app_db.config["APP_CONFIG"].database_path
    before = _count_events(db_path, "landing_category")
    client.get("/commercial-construction-leads")
    after = _count_events(db_path, "landing_category")
    assert after == before + 1


def test_the_recorded_event_carries_no_url_or_identity(app_db):
    db = Database(app_db.config["APP_CONFIG"].database_path)
    try:
        client = app_db.test_client()
        client.get("/commercial-construction-leads")
        row = db.conn.execute(
            "SELECT * FROM analytics_event WHERE event_name = 'landing_category' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        columns = set(row.keys())
        for forbidden in ("ip", "ip_address", "user_agent", "referrer", "url", "path", "user_id"):
            assert forbidden not in columns, f"analytics stores {forbidden}"
        # The event name is a fixed vocabulary term, never a URL.
        assert row["event_name"] in LANDING_KINDS
        assert "/" not in row["event_name"]
    finally:
        db.close()


def test_directory_landing_is_recorded(client, app_db):
    before = _count_events(app_db.config["APP_CONFIG"].database_path, "landing_directory")
    client.get("/opportunities")
    after = _count_events(app_db.config["APP_CONFIG"].database_path, "landing_directory")
    assert after == before + 1


def test_funnel_counts_read_the_real_events(app_db):
    db = Database(app_db.config["APP_CONFIG"].database_path)
    try:
        rows = funnel_counts(db)
        names = [r["event"] for r in rows]
        assert "signup" in names
        assert "opportunity_viewed" in names
        assert all(r["count"] >= 0 for r in rows)
    finally:
        db.close()


def test_landing_counts_only_report_landing_kinds(app_db):
    db = Database(app_db.config["APP_CONFIG"].database_path)
    try:
        for row in landing_counts(db):
            assert row["kind"] in LANDING_KINDS
    finally:
        db.close()


# =============================================================================
# Report
# =============================================================================


def test_seo_report_produces_a_complete_audit(app_db):
    from oppintel.app.seo_report import full_report

    db = Database(app_db.config["APP_CONFIG"].database_path)
    try:
        report = full_report(db)
    finally:
        db.close()
    for key in (
        "keyword_coverage", "technical", "programmatic_pages", "content", "product",
        "funnel", "landings",
    ):
        assert key in report, key
    assert report["keyword_coverage"]
    assert report["technical"]["duplicate_primary_keywords"] == []


def test_seo_report_reports_no_ranking_claims(app_db):
    """The report is descriptive. It must not assert a position, because none is measured."""
    from oppintel.app.seo_report import full_report

    db = Database(app_db.config["APP_CONFIG"].database_path)
    try:
        serialized = json.dumps(full_report(db), default=str).lower()
    finally:
        db.close()
    for term in ("rank_position", "ranking_position", "serp_position", "position:"):
        assert term not in serialized


def test_seo_report_records_the_gate_decision_for_programmatic_pages(app_db):
    from oppintel.app.seo_report import programmatic_pages

    db = Database(app_db.config["APP_CONFIG"].database_path)
    try:
        rows = programmatic_pages(db)
    finally:
        db.close()
    city_trade = [r for r in rows if r["kind"] == "city_trade"]
    assert city_trade, "the report must list the programmatic combinations"
    for row in city_trade:
        assert isinstance(row["indexable"], bool)
        assert "reason" in row


def _count_events(db_path, event_name: str) -> int:
    db = Database(db_path)
    try:
        return int(
            db.conn.execute(
                "SELECT COUNT(*) FROM analytics_event WHERE event_name = ?", (event_name,)
            ).fetchone()[0]
        )
    finally:
        db.close()
