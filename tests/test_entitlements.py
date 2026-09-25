"""Entitlement and subscription tests.

The property that matters commercially: **an account cannot grant itself access.** Payment is
not implemented, so these tests verify the separation instead — that a plan is a catalogue
entry, a subscription is a stored relationship, and the resolved entitlement is derived from
that relationship and nothing else.
"""

from __future__ import annotations

import pytest

from oppintel.app.accounts import User
from oppintel.app.entitlements import (
    FEATURE_ALERTS,
    FEATURE_API,
    FEATURE_EXPORT,
    FEATURE_TEAM,
    FREE_PLAN,
    STATUS_ACTIVE,
    STATUS_CANCELED,
    STATUS_PAST_DUE,
    STATUS_TRIAL,
    SubscriptionService,
)
from oppintel.db import Database


@pytest.fixture
def subs_db(fixture_db):
    return fixture_db


def _make_user(db: Database, email: str, access_level: str = "FREE") -> User:
    db.conn.execute(
        """
        INSERT INTO app_user (email, password_hash, display_name, access_level, is_active,
            created_at)
        VALUES (?, 'x', ?, ?, 1, '2026-09-01T00:00:00+00:00')
        """,
        (email, email.split("@")[0], access_level),
    )
    db.conn.commit()
    row = db.conn.execute("SELECT * FROM app_user WHERE email = ?", (email,)).fetchone()
    return User(
        id=int(row["id"]), email=row["email"], display_name=row["display_name"],
        access_level=row["access_level"], created_at=row["created_at"],
        last_login_at=row["last_login_at"],
    )


def _service(db: Database) -> SubscriptionService:
    service = SubscriptionService(db)
    service.ensure_plans()
    return service


# --- the catalogue --------------------------------------------------------------

def test_plans_are_seeded_with_the_expected_tiers(subs_db):
    service = _service(subs_db)
    assert {p["id"] for p in service.plans()} == {"FREE", "PRO", "TEAM"}


def test_ensuring_plans_is_idempotent(subs_db):
    service = _service(subs_db)
    first = len(service.plans())
    service.ensure_plans()
    assert len(service.plans()) == first


def test_the_free_plan_grants_no_gated_feature(subs_db):
    plan = _service(subs_db).plan(FREE_PLAN)
    assert plan["entitlements"] == []


def test_plans_are_ordered_by_rank(subs_db):
    ranks = [p["rank"] for p in _service(subs_db).plans()]
    assert ranks == sorted(ranks)


# --- resolution -----------------------------------------------------------------

def test_an_anonymous_visitor_gets_no_gated_feature(subs_db):
    entitlement = _service(subs_db).entitlements_for(None)
    assert entitlement.plan_id == FREE_PLAN
    assert not entitlement.has(FEATURE_EXPORT)
    assert not entitlement.is_paid


def test_a_new_account_defaults_to_free(subs_db):
    user = _make_user(subs_db, "free@example.com")
    entitlement = _service(subs_db).entitlements_for(user)
    assert entitlement.plan_id == FREE_PLAN
    assert entitlement.source == "default"
    assert not entitlement.is_paid


def test_a_stored_active_subscription_grants_the_plans_features(subs_db):
    service = _service(subs_db)
    user = _make_user(subs_db, "pro@example.com")
    service.set_subscription(user.id, "PRO", STATUS_ACTIVE)

    entitlement = service.entitlements_for(user)
    assert entitlement.plan_id == "PRO"
    assert entitlement.has(FEATURE_EXPORT)
    assert entitlement.has(FEATURE_ALERTS)
    assert not entitlement.has(FEATURE_TEAM), "PRO must not grant the TEAM feature"
    assert entitlement.is_paid


def test_a_trial_grants_the_plans_features(subs_db):
    service = _service(subs_db)
    user = _make_user(subs_db, "trial@example.com")
    service.set_subscription(user.id, "TEAM", STATUS_TRIAL)
    assert service.entitlements_for(user).has(FEATURE_TEAM)


def test_a_lapsed_subscription_grants_nothing_beyond_free(subs_db):
    """Access is lost automatically when a subscription stops being active."""
    service = _service(subs_db)
    user = _make_user(subs_db, "lapsed@example.com")
    service.set_subscription(user.id, "TEAM", STATUS_PAST_DUE)

    entitlement = service.entitlements_for(user)
    assert entitlement.plan_id == FREE_PLAN
    assert not entitlement.has(FEATURE_TEAM)
    assert not entitlement.is_paid
    # The status is still surfaced, so the account is told why rather than silently downgraded.
    assert entitlement.status == STATUS_PAST_DUE


def test_a_cancelled_subscription_grants_nothing_beyond_free(subs_db):
    service = _service(subs_db)
    user = _make_user(subs_db, "cancelled@example.com")
    service.set_subscription(user.id, "PRO", STATUS_CANCELED)
    assert not service.entitlements_for(user).has(FEATURE_EXPORT)


def test_an_operator_holds_every_feature(subs_db):
    user = _make_user(subs_db, "admin@example.com", access_level="ADMIN")
    entitlement = _service(subs_db).entitlements_for(user)
    assert entitlement.source == "operator"
    for feature in (FEATURE_EXPORT, FEATURE_ALERTS, FEATURE_TEAM, FEATURE_API):
        assert entitlement.has(feature)


def test_an_operator_level_does_not_make_an_account_paid(subs_db):
    """Operator access is internal, not a commercial tier."""
    user = _make_user(subs_db, "admin2@example.com", access_level="ADMIN")
    assert not _service(subs_db).entitlements_for(user).is_paid


# --- there is no self-service upgrade -------------------------------------------

def test_an_unknown_plan_is_refused(subs_db):
    service = _service(subs_db)
    user = _make_user(subs_db, "bad@example.com")
    with pytest.raises(ValueError):
        service.set_subscription(user.id, "ENTERPRISE_FREE_FOREVER", STATUS_ACTIVE)


def test_no_web_route_reaches_the_subscription_writer(subs_db):
    """The service method that grants access must not be callable from a request."""
    from oppintel.app.main import create_app

    rules = {str(rule) for rule in create_app().url_map.iter_rules()}
    for path in rules:
        assert "subscription" not in path and "upgrade" not in path, (
            f"{path} could grant a plan over the web"
        )


def test_changing_plan_clears_the_renewal_marker(subs_db):
    service = _service(subs_db)
    user = _make_user(subs_db, "switch@example.com")
    service.set_subscription(user.id, "PRO", STATUS_ACTIVE)
    service.set_subscription(user.id, "TEAM", STATUS_ACTIVE)
    row = service.subscription_for(user.id)
    assert row["plan_id"] == "TEAM"
    assert row["renewed_at"] is None


def test_clearing_the_subscription_returns_the_account_to_free(subs_db):
    service = _service(subs_db)
    user = _make_user(subs_db, "clear@example.com")
    service.set_subscription(user.id, "PRO", STATUS_ACTIVE)
    service.clear_subscription(user.id)
    assert service.entitlements_for(user).plan_id == FREE_PLAN


# --- the API reports the resolved truth ------------------------------------------

def test_the_api_reports_free_for_an_account_with_no_subscription(client):
    client.post(
        "/signup",
        data={
            "email": "apiuser@example.com",
            "password": "correct-horse-battery",
            "password_confirm": "correct-horse-battery",
        },
        follow_redirects=True,
    )
    payload = client.get("/api/me").get_json()
    assert payload["entitlement"]["plan"] == FREE_PLAN
    assert payload["entitlement"]["is_paid"] is False
    assert payload["entitlement"]["features"] == []


def test_the_api_reports_anonymous_without_a_session(client):
    payload = client.get("/api/me").get_json()
    assert payload["authenticated"] is False
    assert payload["entitlement"]["plan"] == FREE_PLAN
