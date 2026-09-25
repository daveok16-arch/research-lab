"""Tests for the admin-only operations view.

`/admin/data` exposes internal aggregate counts, source coverage and the data quality summary.
It must be reachable only by an authenticated operator, and must expose nothing that would be
dangerous if it were reached.

Three access cases are required and each is asserted here:

* unauthenticated          -> redirect to sign-in (302)
* authenticated non-admin  -> 403
* authenticated admin      -> 200
"""

from __future__ import annotations

from oppintel.db import Database

ADMIN_PATH = "/admin/data"


# =============================================================================
# 1. Unauthenticated
# =============================================================================


def test_unauthenticated_user_is_redirected_to_signin(client):
    """A stranger is sent to sign-in rather than shown the page or a bare error."""
    response = client.get(ADMIN_PATH)
    assert response.status_code == 302
    assert "/signin" in response.headers["Location"]


def test_redirect_returns_the_user_to_the_admin_page_after_signing_in(client):
    """`next` points back here, so signing in lands on the page that was requested."""
    response = client.get(ADMIN_PATH)
    assert "next=" in response.headers["Location"]
    assert ADMIN_PATH in response.headers["Location"]


def test_unauthenticated_user_cannot_read_the_page_body(client):
    """The redirect must not carry the content, even in the body of a 302."""
    body = client.get(ADMIN_PATH).get_data(as_text=True)
    assert "Data quality report" not in body
    assert "Source coverage" not in body


def test_unauthenticated_request_after_following_redirect_does_not_reach_the_page(client):
    """Following the redirect lands on sign-in, not on the operations view."""
    response = client.get(ADMIN_PATH, follow_redirects=True)
    assert response.status_code == 200
    assert "/signin" in response.request.path
    assert "Data quality report" not in response.get_data(as_text=True)


# =============================================================================
# 2. Authenticated non-admin
# =============================================================================


def test_authenticated_non_admin_receives_403(session_client):
    """Signed in but not an operator: forbidden, not redirected.

    The distinction matters. A signed-in customer is not a stranger to be sent round the
    sign-in loop again, so the response states plainly that the page is not for them.
    """
    response = session_client.get(ADMIN_PATH)
    assert response.status_code == 403


def test_403_page_explains_without_disclosing_the_contents(session_client):
    body = session_client.get(ADMIN_PATH).get_data(as_text=True)
    assert "do not have access" in body
    # Nothing about what the page holds.
    assert "Data quality report" not in body
    assert "Source coverage" not in body


def test_403_is_not_a_404(session_client):
    """The page exists; the account simply lacks access. Saying otherwise would be untrue."""
    assert session_client.get(ADMIN_PATH).status_code == 403
    assert session_client.get("/no/such/page").status_code == 404


def test_pro_and_team_levels_do_not_grant_admin(app_db, session_client):
    """A paid tier is not an operator level.

    A subscription must never imply access to internal data, so raising the account to PRO or
    TEAM must not open the page.
    """
    db = Database(app_db.config["APP_CONFIG"].database_path)
    try:
        for level in ("PRO", "TEAM"):
            db.conn.execute("UPDATE app_user SET access_level = ?", (level,))
            db.conn.commit()
            response = session_client.get(ADMIN_PATH)
            assert response.status_code == 403, f"{level} should not reach the admin page"
    finally:
        db.close()


def test_saved_and_preferences_still_work_for_a_non_admin(session_client):
    """The gate must not break the ordinary account pages."""
    assert session_client.get("/saved").status_code == 200
    assert session_client.get("/preferences").status_code == 200


# =============================================================================
# 3. Authenticated admin
# =============================================================================


def test_admin_can_access_the_page(admin_client):
    response = admin_client.get(ADMIN_PATH)
    assert response.status_code == 200


def test_admin_page_shows_the_operations_content(admin_client):
    body = admin_client.get(ADMIN_PATH).get_data(as_text=True)
    assert "Data operations" in body
    assert "Source coverage" in body
    assert "Data quality report" in body


def test_admin_page_is_noindex(admin_client):
    """Even an authorised page must not be indexed."""
    body = admin_client.get(ADMIN_PATH).get_data(as_text=True)
    assert "noindex" in body


def test_admin_page_has_a_distinct_title(admin_client):
    body = admin_client.get(ADMIN_PATH).get_data(as_text=True)
    assert "<title>" in body
    assert "Data operations" in body


def test_revoking_admin_immediately_blocks_the_page(app_db, admin_client):
    """The level is re-read per request, so a revocation takes effect at once."""
    assert admin_client.get(ADMIN_PATH).status_code == 200
    db = Database(app_db.config["APP_CONFIG"].database_path)
    try:
        db.conn.execute("UPDATE app_user SET access_level = 'FREE'")
        db.conn.commit()
    finally:
        db.close()
    assert admin_client.get(ADMIN_PATH).status_code == 403


def test_deactivating_an_admin_account_blocks_the_page(app_db, admin_client):
    assert admin_client.get(ADMIN_PATH).status_code == 200
    db = Database(app_db.config["APP_CONFIG"].database_path)
    try:
        db.conn.execute("UPDATE app_user SET is_active = 0")
        db.conn.commit()
    finally:
        db.close()
    # A deactivated account is no longer a session at all, so it is sent to sign-in.
    assert admin_client.get(ADMIN_PATH).status_code == 302


# =============================================================================
# No privilege escalation through the web
# =============================================================================


def test_no_web_route_can_grant_the_admin_level(app_db, session_client):
    """There must be no request that raises a user's own privileges.

    The level is granted only by the CLI. These are the plausible web routes that could
    otherwise be added by accident; all must be absent.
    """
    for path in (
        "/admin/grant",
        "/admin/promote",
        "/admin/users",
        "/preferences/grant-admin",
        "/api/admin/grant",
    ):
        response = session_client.get(path)
        assert response.status_code == 404, f"{path} unexpectedly exists"

    # And the account is still not an operator.
    db = Database(app_db.config["APP_CONFIG"].database_path)
    try:
        row = db.conn.execute("SELECT access_level FROM app_user").fetchone()
        assert row["access_level"] == "FREE"
    finally:
        db.close()


def test_preferences_cannot_set_the_access_level(app_db, session_client):
    """A crafted preferences submission must not change the account level."""
    session_client.post(
        "/preferences",
        data={
            "market_id": "dfw",
            "trade_id": "commercial_hvac",
            "access_level": "ADMIN",
            "is_admin": "1",
        },
    )
    db = Database(app_db.config["APP_CONFIG"].database_path)
    try:
        row = db.conn.execute("SELECT access_level FROM app_user").fetchone()
        assert row["access_level"] == "FREE"
    finally:
        db.close()


def test_signup_cannot_choose_the_admin_level(app_db):
    """A crafted signup form must not create an operator account."""
    c = app_db.test_client()
    c.post(
        "/signup",
        data={
            "email": "sneaky@example.com",
            "password": "a-good-password",
            "password_confirm": "a-good-password",
            "access_level": "ADMIN",
            "is_admin": "1",
        },
    )
    db = Database(app_db.config["APP_CONFIG"].database_path)
    try:
        row = db.conn.execute(
            "SELECT access_level FROM app_user WHERE email = 'sneaky@example.com'"
        ).fetchone()
        assert row is not None
        assert row["access_level"] == "FREE"
    finally:
        db.close()


# =============================================================================
# No intelligence ingestion surface is exposed
# =============================================================================


def test_admin_page_exposes_no_ingestion_endpoint(admin_client):
    """The operations view reports state; it must not offer a way to run the pipeline."""
    body = admin_client.get(ADMIN_PATH).get_data(as_text=True).lower()
    for surface in (
        "/ingest",
        "run ingestion",
        "trigger ingestion",
        "run pipeline",
        "connector",
        "scrape",
    ):
        assert surface not in body, f"the admin page exposes an ingestion surface: {surface}"


def test_admin_page_exposes_no_source_url_or_credential(admin_client):
    """No clickable source endpoints, no credentials, no database path."""
    body = admin_client.get(ADMIN_PATH).get_data(as_text=True)
    import re

    assert not re.findall(r"https?://", body), "the admin page renders an outbound URL"
    for secret in ("password", "secret", "api_key", "token", "oppintel.db", "sqlite"):
        assert secret not in body.lower(), f"the admin page leaks: {secret}"


def test_no_ingestion_route_is_registered(app_db):
    """Ingestion stays behind the CLI, not on the web surface."""
    rules = {str(rule) for rule in app_db.url_map.iter_rules()}
    for path in ("/ingest", "/admin/ingest", "/api/ingest", "/pipeline", "/admin/pipeline"):
        assert path not in rules, f"{path} is exposed on the web surface"


def test_admin_route_is_not_linked_from_any_public_page(client):
    """Nothing in the public UI points at the operations view."""
    for path in ("/", "/opportunities", "/markets", "/trades", "/how-it-works", "/reports"):
        body = client.get(path).get_data(as_text=True)
        assert "/admin" not in body, f"{path} links to the admin view"


def test_robots_disallows_the_admin_path(client):
    """A crawler is told not to look, independently of the auth check."""
    body = client.get("/robots.txt").get_data(as_text=True)
    assert "Disallow: /admin" in body


def test_admin_path_is_absent_from_the_sitemap(client):
    body = client.get("/sitemap.xml").get_data(as_text=True)
    assert "/admin" not in body
