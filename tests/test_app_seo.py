"""SEO tests: metadata, canonical URLs, sitemap, robots, structured data.

These assert that the indexing rules are actually enforced, not just documented: private
pages are excluded, filtered views are noindex, and structured data never asserts a fact the
record does not support.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET

from oppintel.db import Database


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
        assert row, address_fragment
        return row["slug"]
    finally:
        db.close()


# --- metadata ------------------------------------------------------------------

def test_every_public_page_has_a_title_and_description(client):
    for path in ("/", "/opportunities", "/markets", "/trades", "/how-it-works", "/reports",
                 "/markets/dfw", "/markets/dfw/dallas", "/trades/commercial-hvac",
                 "/markets/dfw/commercial-hvac"):
        body = client.get(path).get_data(as_text=True)
        title = re.search(r"<title>(.*?)</title>", body, re.S)
        description = re.search(r'<meta name="description" content="(.*?)"', body, re.S)
        assert title and title.group(1).strip(), f"missing title on {path}"
        assert description and description.group(1).strip(), f"missing description on {path}"


def test_titles_are_unique_across_indexable_pages(client):
    """Two pages sharing a title compete with each other and confuse a reader."""
    paths = ["/", "/opportunities", "/markets", "/trades", "/how-it-works", "/reports",
             "/markets/dfw", "/markets/dfw/dallas", "/markets/dfw/fort-worth",
             "/trades/commercial-hvac", "/markets/dfw/commercial-hvac"]
    titles = []
    for path in paths:
        body = client.get(path).get_data(as_text=True)
        titles.append(re.search(r"<title>(.*?)</title>", body, re.S).group(1).strip())
    assert len(titles) == len(set(titles)), f"duplicate titles: {titles}"


def test_canonical_url_is_present_on_public_pages(client):
    for path in ("/", "/opportunities", "/markets/dfw", "/trades/commercial-hvac"):
        body = client.get(path).get_data(as_text=True)
        assert 'rel="canonical"' in body, path


def test_opportunity_page_has_unique_metadata(client, app_db):
    slug = _slug(app_db, "10 ROSS")
    body = client.get(f"/opportunities/{slug}").get_data(as_text=True)
    title = re.search(r"<title>(.*?)</title>", body, re.S).group(1)
    assert "10 ROSS AVE" in title
    assert 'rel="canonical"' in body
    assert f"/opportunities/{slug}" in body


def test_opportunity_meta_description_does_not_overclaim(client, app_db):
    """The description must not promise a bid or a contractor."""
    slug = _slug(app_db, "10 ROSS")
    body = client.get(f"/opportunities/{slug}").get_data(as_text=True)
    description = re.search(r'<meta name="description" content="(.*?)"', body, re.S).group(1)
    lowered = description.lower()
    for claim in ("open bid", "guaranteed", "available for bid", "hiring"):
        assert claim not in lowered, f"description overclaims: {claim}"


# --- indexability --------------------------------------------------------------

def test_filtered_view_is_noindex(client):
    """A filtered result set is a different page and must not compete with the directory."""
    body = client.get("/opportunities?city=Dallas").get_data(as_text=True)
    assert 'name="robots" content="noindex' in body


def test_paginated_view_is_noindex(client):
    body = client.get("/opportunities?page=2").get_data(as_text=True)
    assert 'name="robots" content="noindex' in body


def test_clean_directory_is_indexable(client):
    body = client.get("/opportunities").get_data(as_text=True)
    assert 'name="robots" content="index,follow"' in body


def test_private_pages_are_noindex(client):
    """Account and operations pages must never be indexed.

    /saved and /preferences redirect an anonymous visitor to sign-in, which is itself noindex,
    so the assertion follows the redirect and checks the page actually served.
    """
    for path in ("/saved", "/preferences", "/signin", "/signup", "/admin/data"):
        response = client.get(path, follow_redirects=True)
        assert response.status_code == 200, path
        body = response.get_data(as_text=True)
        assert "noindex" in body, f"{path} resolved to {response.request.path} which is indexable"


def test_error_page_is_noindex(client):
    body = client.get("/no/such/page").get_data(as_text=True)
    assert "noindex" in body


# --- structured data -----------------------------------------------------------

def _json_ld(body: str) -> list[dict]:
    blocks = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', body, re.S
    )
    return [json.loads(b) for b in blocks]


def test_structured_data_is_valid_json(client):
    for path in ("/", "/opportunities", "/markets/dfw", "/markets/dfw/commercial-hvac"):
        assert _json_ld(client.get(path).get_data(as_text=True)), path


def test_opportunity_structured_data_omits_unknown_fields(client, app_db):
    """schema.org has no 'unknown' value, so an absent field must be omitted, not faked."""
    slug = _slug(app_db, "10 ROSS")
    blocks = _json_ld(client.get(f"/opportunities/{slug}").get_data(as_text=True))
    serialized = json.dumps(blocks).lower()
    assert "not verified" not in serialized, "structured data asserted a placeholder value"


def test_structured_data_contains_no_fabricated_contractor(client, app_db):
    slug = _slug(app_db, "10 ROSS")
    serialized = json.dumps(
        _json_ld(client.get(f"/opportunities/{slug}").get_data(as_text=True))
    ).lower()
    assert "generalcontractor" not in serialized.replace(" ", "")
    assert "architect" not in serialized


def test_breadcrumbs_are_present_on_detail_pages(client, app_db):
    slug = _slug(app_db, "10 ROSS")
    blocks = _json_ld(client.get(f"/opportunities/{slug}").get_data(as_text=True))
    assert any(b.get("@type") == "BreadcrumbList" for b in blocks)


# --- sitemap -------------------------------------------------------------------

def _sitemap_locs(client) -> list[str]:
    body = client.get("/sitemap.xml").get_data(as_text=True)
    root = ET.fromstring(body)
    namespace = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    return [el.text for el in root.iter(f"{namespace}loc")]


def test_sitemap_is_valid_xml(client):
    body = client.get("/sitemap.xml").get_data(as_text=True)
    assert body.startswith("<?xml")
    ET.fromstring(body)  # raises if malformed


def test_sitemap_contains_canonical_landing_pages(client):
    locs = _sitemap_locs(client)
    joined = " ".join(locs)
    for path in ("/opportunities", "/markets", "/trades", "/how-it-works", "/reports",
                 "/markets/dfw", "/markets/dfw/dallas", "/trades/commercial-hvac"):
        assert path in joined, path


def test_sitemap_contains_opportunity_pages(client):
    locs = _sitemap_locs(client)
    assert any("/opportunities/" in loc for loc in locs)


def test_sitemap_excludes_private_routes(client):
    """Advertising a noindex page in the sitemap sends crawlers contradictory signals."""
    joined = " ".join(_sitemap_locs(client))
    for private in ("/saved", "/preferences", "/signin", "/signup", "/admin", "/api"):
        assert private not in joined, f"sitemap advertises a private route: {private}"


def test_sitemap_excludes_filtered_urls(client):
    """A query string implies a filtered view, which is noindex."""
    joined = " ".join(_sitemap_locs(client))
    assert "?" not in joined


# --- robots --------------------------------------------------------------------

def test_robots_txt_is_served(client):
    response = client.get("/robots.txt")
    assert response.status_code == 200
    assert "text/plain" in response.headers["Content-Type"]


def test_robots_allows_public_pages(client):
    body = client.get("/robots.txt").get_data(as_text=True)
    assert "User-agent: *" in body
    assert "Allow: /" in body


def test_robots_disallows_private_areas(client):
    body = client.get("/robots.txt").get_data(as_text=True)
    for private in ("/saved", "/preferences", "/admin", "/api", "/signin"):
        assert f"Disallow: {private}" in body, private


def test_robots_keeps_crawlers_off_filtered_views(client):
    body = client.get("/robots.txt").get_data(as_text=True)
    assert "Disallow: /*?" in body


# --- no thin pages -------------------------------------------------------------

def test_no_landing_page_is_generated_for_every_database_city(client):
    """Only configured cities get a page, so the site does not emit thin duplicates."""
    # The market config declares Dallas and Fort Worth as landing pages. A city the database
    # holds but the config does not list must not resolve.
    assert client.get("/markets/dfw/farmersville").status_code == 404
    assert client.get("/markets/dfw/lucas").status_code == 404


def test_inactive_market_and_trade_have_no_pages(client):
    assert client.get("/markets/houston").status_code == 404
    assert client.get("/markets/austin").status_code == 404
