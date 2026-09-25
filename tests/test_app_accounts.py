"""Account, saved-opportunity and security tests."""

from __future__ import annotations

from oppintel.db import Database


def _db(app_db) -> Database:
    return Database(app_db.config["APP_CONFIG"].database_path)


def _project_id_with_slug(app_db, address_fragment: str) -> int:
    db = _db(app_db)
    try:
        row = db.conn.execute(
            "SELECT id FROM project WHERE address LIKE ? LIMIT 1", (f"{address_fragment}%",)
        ).fetchone()
        assert row, address_fragment
        return int(row["id"])
    finally:
        db.close()


# --- signup --------------------------------------------------------------------

def test_signup_creates_an_account_and_signs_in(client):
    response = client.post(
        "/signup",
        data={
            "email": "new@example.com",
            "password": "a-good-password",
            "password_confirm": "a-good-password",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Sign Out" in body, "a signed-in user should see the sign-out control"


def test_signup_rejects_mismatched_passwords(app_db):
    c = app_db.test_client()
    response = c.post(
        "/signup",
        data={
            "email": "x@example.com",
            "password": "a-good-password",
            "password_confirm": "different-password",
        },
    )
    assert "do not match" in response.get_data(as_text=True)


def test_signup_rejects_a_short_password(app_db):
    c = app_db.test_client()
    response = c.post(
        "/signup",
        data={"email": "x@example.com", "password": "short", "password_confirm": "short"},
    )
    assert "at least" in response.get_data(as_text=True)


def test_signup_rejects_an_invalid_email(app_db):
    c = app_db.test_client()
    response = c.post(
        "/signup",
        data={"email": "not-an-email", "password": "a-good-password",
              "password_confirm": "a-good-password"},
    )
    assert "valid email" in response.get_data(as_text=True)


def test_duplicate_signup_is_rejected(app_db):
    c = app_db.test_client()
    payload = {"email": "dup@example.com", "password": "a-good-password",
               "password_confirm": "a-good-password"}
    c.post("/signup", data=payload)
    c.get("/signout")
    response = c.post("/signup", data=payload)
    assert "already exists" in response.get_data(as_text=True)


# --- password storage ----------------------------------------------------------

def test_password_is_never_stored_in_plain_text(app_db):
    """A plain digest would be a real vulnerability in a system holding email addresses."""
    c = app_db.test_client()
    c.post(
        "/signup",
        data={"email": "hash@example.com", "password": "a-good-password",
              "password_confirm": "a-good-password"},
    )
    db = _db(app_db)
    try:
        row = db.conn.execute(
            "SELECT password_hash FROM app_user WHERE email = 'hash@example.com'"
        ).fetchone()
        assert row is not None
        stored = row["password_hash"]
        assert "a-good-password" not in stored
        assert stored.startswith("pbkdf2:") or stored.startswith("scrypt:"), stored[:20]
    finally:
        db.close()


def test_password_hash_is_salted_per_account(app_db):
    """Two accounts with the same password must not share a hash."""
    c = app_db.test_client()
    for email in ("a@example.com", "b@example.com"):
        c.post("/signup", data={"email": email, "password": "identical-password",
                                "password_confirm": "identical-password"})
        c.get("/signout")
    db = _db(app_db)
    try:
        hashes = [
            r["password_hash"]
            for r in db.conn.execute("SELECT password_hash FROM app_user")
        ]
        assert len(set(hashes)) == len(hashes), "password hashes are not uniquely salted"
    finally:
        db.close()


# --- sign in -------------------------------------------------------------------

def test_signin_with_correct_credentials(session_client):
    body = session_client.get("/").get_data(as_text=True)
    assert "Sign Out" in body


def test_signin_with_wrong_password_is_rejected(app_db):
    c = app_db.test_client()
    c.post("/signup", data={"email": "user@example.com", "password": "a-good-password",
                            "password_confirm": "a-good-password"})
    c.get("/signout")
    response = c.post("/signin", data={"email": "user@example.com", "password": "wrong"})
    assert "incorrect" in response.get_data(as_text=True)


def test_login_errors_do_not_reveal_whether_an_account_exists(app_db):
    """The same message for both cases, so the form cannot enumerate accounts."""
    c = app_db.test_client()
    c.post("/signup", data={"email": "known@example.com", "password": "a-good-password",
                            "password_confirm": "a-good-password"})
    c.get("/signout")
    wrong_password = c.post(
        "/signin", data={"email": "known@example.com", "password": "wrong-password"}
    ).get_data(as_text=True)
    unknown_user = c.post(
        "/signin", data={"email": "nobody@example.com", "password": "wrong-password"}
    ).get_data(as_text=True)

    def message(body: str) -> str:
        for fragment in ("Email or password is incorrect.", "This account is not active."):
            if fragment in body:
                return fragment
        return ""

    assert message(wrong_password) == message(unknown_user) == "Email or password is incorrect."


def test_signin_does_not_echo_the_submitted_password(app_db):
    c = app_db.test_client()
    response = c.post(
        "/signin", data={"email": "x@example.com", "password": "super-secret-value"}
    )
    assert "super-secret-value" not in response.get_data(as_text=True)


def test_signout_clears_the_session(session_client):
    session_client.get("/signout")
    body = session_client.get("/").get_data(as_text=True)
    assert "Sign In" in body
    assert "Sign Out" not in body


def test_open_redirect_is_blocked(client):
    """A `next` pointing off-site must not be honoured."""
    response = client.get("/signin?next=https://evil.example.com/steal")
    assert response.status_code == 200
    # And after a successful sign-in the redirect stays on this site.
    client.post(
        "/signup",
        data={"email": "redir@example.com", "password": "a-good-password",
              "password_confirm": "a-good-password"},
    )
    client.get("/signout")
    response = client.post(
        "/signin?next=https://evil.example.com/steal",
        data={"email": "redir@example.com", "password": "a-good-password"},
    )
    assert "evil.example.com" not in response.headers.get("Location", "")


# --- saved opportunities -------------------------------------------------------

def test_saving_requires_authentication(app_db):
    project_id = _project_id_with_slug(app_db, "10 ROSS")
    c = app_db.test_client()
    response = c.post(f"/saved/{project_id}", data={"action": "save"})
    assert response.status_code in (302, 401)


def test_save_and_view_a_saved_opportunity(session_client, app_db):
    project_id = _project_id_with_slug(app_db, "10 ROSS")
    session_client.post(f"/saved/{project_id}", data={"action": "save"})
    body = session_client.get("/saved").get_data(as_text=True)
    assert "10 ROSS AVE" in body


def test_unsaving_removes_it(session_client, app_db):
    project_id = _project_id_with_slug(app_db, "10 ROSS")
    session_client.post(f"/saved/{project_id}", data={"action": "save"})
    session_client.post(f"/saved/{project_id}", data={"action": "remove"})
    body = session_client.get("/saved").get_data(as_text=True)
    assert "10 ROSS AVE" not in body


def test_saving_stores_only_the_relationship(app_db):
    """No project fields may be copied, so a re-ingest cannot leave a stale saved record."""
    c = app_db.test_client()
    c.post("/signup", data={"email": "saver@example.com", "password": "a-good-password",
                            "password_confirm": "a-good-password"})
    project_id = _project_id_with_slug(app_db, "10 ROSS")
    c.post(f"/saved/{project_id}", data={"action": "save"})

    db = _db(app_db)
    try:
        columns = [
            row[1] for row in db.conn.execute("PRAGMA table_info(saved_opportunity)")
        ]
        assert set(columns) == {"user_id", "project_id", "saved_at", "note"}, columns
        count = db.conn.execute(
            "SELECT COUNT(*) FROM saved_opportunity WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
        assert count == 1
    finally:
        db.close()


def test_saving_twice_does_not_duplicate(session_client, app_db):
    project_id = _project_id_with_slug(app_db, "10 ROSS")
    session_client.post(f"/saved/{project_id}", data={"action": "save"})
    session_client.post(f"/saved/{project_id}", data={"action": "save"})
    db = _db(app_db)
    try:
        assert db.conn.execute("SELECT COUNT(*) FROM saved_opportunity").fetchone()[0] == 1
    finally:
        db.close()


def test_cannot_save_a_nonexistent_project(session_client):
    response = session_client.post("/saved/999999", data={"action": "save"})
    assert response.status_code in (302, 404)


def test_saved_page_requires_sign_in(app_db):
    response = app_db.test_client().get("/saved")
    assert response.status_code == 302


# --- preferences ---------------------------------------------------------------

def test_preferences_require_sign_in(app_db):
    assert app_db.test_client().get("/preferences").status_code == 302


def test_preferences_default_to_the_active_market_and_trade(session_client, app_db):
    db = _db(app_db)
    try:
        row = db.conn.execute("SELECT * FROM user_preference LIMIT 1").fetchone()
        assert row is not None
        assert row["market_id"] == "dfw"
        assert row["trade_id"] == "commercial_hvac"
    finally:
        db.close()


def test_preferences_can_be_updated(session_client):
    response = session_client.post(
        "/preferences",
        data={"market_id": "dfw", "trade_id": "commercial_hvac",
              "cities": ["Dallas"], "notify_in_app": "1"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Preferences saved" in response.get_data(as_text=True)


def test_email_notification_is_off_by_default(session_client, app_db):
    """Email delivery is not implemented, so the preference must not claim it is on."""
    db = _db(app_db)
    try:
        row = db.conn.execute("SELECT notify_email FROM user_preference LIMIT 1").fetchone()
        assert row["notify_email"] == 0
    finally:
        db.close()


# --- session security ----------------------------------------------------------

def test_session_cookie_is_httponly(app_db):
    assert app_db.config["SESSION_COOKIE_HTTPONLY"] is True


def test_session_cookie_is_samesite_lax(app_db):
    assert app_db.config["SESSION_COOKIE_SAMESITE"] == "Lax"


def test_deactivated_account_cannot_act_on_a_stale_session(session_client, app_db):
    """The user is re-read per request, so deactivation takes effect immediately."""
    db = _db(app_db)
    try:
        db.conn.execute("UPDATE app_user SET is_active = 0")
        db.conn.commit()
    finally:
        db.close()
    response = session_client.get("/saved")
    assert response.status_code == 302, "a deactivated account should not reach a private page"
