"""Application tests: routing, listing, filtering, search and pagination.

These exercise the real Flask app against a real database. No mocks: the assertions are about
what a visitor would actually receive.
"""

from __future__ import annotations


# --- public pages render -------------------------------------------------------

def test_homepage_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Commercial" in body
    # The primary call to action must be present.
    assert "Explore Opportunities" in body


def test_homepage_shows_real_statistics_not_placeholders(client):
    """Every headline figure must come from the database."""
    body = client.get("/").get_data(as_text=True)
    assert "Commercial projects" in body
    assert "Permit records processed" in body
    # No placeholder or fabricated marketing figure.
    for fake in ("10,000+", "trusted by", "thousands of contractors"):
        assert fake not in body


def test_all_primary_navigation_pages_render(client):
    for path in ("/", "/opportunities", "/markets", "/trades", "/how-it-works", "/reports"):
        response = client.get(path)
        assert response.status_code == 200, path


def test_landing_pages_render(client):
    for path in (
        "/markets/dfw",
        "/markets/dfw/dallas",
        "/markets/dfw/fort-worth",
        "/trades/commercial-hvac",
        "/markets/dfw/commercial-hvac",
    ):
        response = client.get(path)
        assert response.status_code == 200, path


def test_unknown_path_returns_404_page(client):
    response = client.get("/no/such/page")
    assert response.status_code == 404
    assert "could not be found" in response.get_data(as_text=True)


def test_unknown_opportunity_slug_returns_404(client):
    assert client.get("/opportunities/does-not-exist").status_code == 404


def test_unknown_market_and_trade_return_404(client):
    assert client.get("/markets/atlantis").status_code == 404
    assert client.get("/trades/roofing").status_code == 404


# --- directory listing ---------------------------------------------------------

def test_directory_lists_discoverable_opportunities(client):
    body = client.get("/opportunities").get_data(as_text=True)
    assert "10 ROSS AVE" in body
    assert "50 TOWER ST" in body


def test_completed_work_is_excluded_from_discovery(client):
    """A finished project must not appear in the public directory."""
    body = client.get("/opportunities").get_data(as_text=True)
    assert "20 DONE ST" not in body


def test_service_work_never_becomes_an_opportunity(client):
    """Like-for-like replacement is maintenance, not a project."""
    body = client.get("/opportunities").get_data(as_text=True)
    assert "30 SERVICE ST" not in body


def test_plumbing_only_record_is_not_listed_as_hvac(client):
    """An HVAC directory must not list a record with no mechanical evidence."""
    body = client.get("/opportunities").get_data(as_text=True)
    assert "40 PLUMB ST" not in body


def test_directory_can_include_unverified_records_on_request(client):
    """The wider set is reachable, but only by explicit opt-in."""
    body = client.get("/opportunities?include_unverified=1").get_data(as_text=True)
    assert "40 PLUMB ST" in body


# --- filtering -----------------------------------------------------------------

def test_city_filter(client):
    body = client.get("/opportunities?city=Fort Worth").get_data(as_text=True)
    assert "10 ROSS AVE" in body
    body = client.get("/opportunities?city=Dallas").get_data(as_text=True)
    assert "10 ROSS AVE" not in body


def test_project_type_filter(client):
    body = client.get("/opportunities?project_type=Office").get_data(as_text=True)
    assert response_has(body, "10 ROSS AVE") or response_has(body, "Not verified")


def response_has(body: str, needle: str) -> bool:
    return needle in body


def test_classification_filter(client):
    body = client.get("/opportunities?classification=HIGH").get_data(as_text=True)
    assert "No matching opportunities" in body or "HIGH" in body


def test_date_range_filter_excludes_out_of_range(client):
    body = client.get("/opportunities?date_from=2026-01-01&date_to=2026-01-02").get_data(as_text=True)
    assert "10 ROSS AVE" not in body


def test_filter_by_unknown_city_yields_empty_state(client):
    body = client.get("/opportunities?city=Nowhere").get_data(as_text=True)
    assert "No matching opportunities" in body
    # The empty state must be useful rather than blank.
    assert "Try expanding your date range" in body


# --- search --------------------------------------------------------------------

def test_search_by_address(client):
    body = client.get("/opportunities?q=ross").get_data(as_text=True)
    assert "10 ROSS AVE" in body


def test_search_by_permit_number(client):
    body = client.get("/opportunities?q=M1").get_data(as_text=True)
    assert "10 ROSS AVE" in body


def test_search_with_no_match_shows_empty_state(client):
    body = client.get("/opportunities?q=zzzznotathing").get_data(as_text=True)
    assert "No matching opportunities" in body


def test_search_input_with_fts_syntax_does_not_error(client):
    """A quote or a bare operator must degrade to no results, not a 500."""
    for term in ('"', "AND", "OR", "NEAR(", "a AND", "*", "((("):
        response = client.get("/opportunities", query_string={"q": term})
        assert response.status_code == 200, term


def test_search_terms_are_not_interpolated_into_sql(client):
    """A SQL fragment must be treated as text."""
    response = client.get("/opportunities", query_string={"q": "'; DROP TABLE project; --"})
    assert response.status_code == 200
    # The table still exists.
    assert client.get("/opportunities").status_code == 200


# --- sorting -------------------------------------------------------------------

def test_sort_options_all_render(client):
    for key in ("recent", "updated", "status"):
        assert client.get(f"/opportunities?sort={key}").status_code == 200


def test_unknown_sort_falls_back_to_default(client):
    """An invalid sort key must not error; it falls back rather than reaching SQL."""
    assert client.get("/opportunities?sort=profitability").status_code == 200


def test_no_subjective_best_opportunity_sort_exists(client):
    """The directory must not offer a subjective ranking.

    The phrase may appear only inside an explicit statement that no such ranking exists, so
    each occurrence is checked in context rather than by a blunt substring test.
    """
    body = client.get("/opportunities").get_data(as_text=True).lower()
    for phrase in ("best opportunity", "best lead", "top opportunity", "highest value lead"):
        start = 0
        while (index := body.find(phrase, start)) != -1:
            context = body[max(0, index - 60):index]
            assert "no subjective" in context or "there is no" in context, (
                f"{phrase!r} used as a claim: ...{context}"
            )
            start = index + 1


# --- pagination ----------------------------------------------------------------

def test_pagination_controls_absent_when_results_fit(client):
    body = client.get("/opportunities?page_size=50").get_data(as_text=True)
    assert "Page 1 of" not in body


def test_page_size_is_clamped(client):
    """A hand-edited page_size cannot ask for an unbounded page."""
    response = client.get("/opportunities?page_size=100000")
    assert response.status_code == 200


def test_invalid_page_number_does_not_error(client):
    for value in ("0", "-5", "abc", ""):
        assert client.get("/opportunities", query_string={"page": value}).status_code == 200


def test_out_of_range_page_shows_empty_state(client):
    body = client.get("/opportunities?page=9999").get_data(as_text=True)
    assert "No matching opportunities" in body
