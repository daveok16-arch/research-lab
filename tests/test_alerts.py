"""Alert tests.

The invariant this module exists to protect: **every alert has an underlying event**. An alert
that cannot name the change that caused it is indistinguishable from a notification the product
invented, so these tests assert both that real changes produce alerts and that nothing else
does.
"""

from __future__ import annotations

from datetime import date

import pytest

from oppintel.app.alerts import (
    ALERT_MECHANICAL_ADDED,
    ALERT_NEW_MATCH,
    ALERT_NEW_PERMIT,
    AlertService,
    build_alerts,
)
from oppintel.db import Database
from oppintel.models import Permit, normalize_address
from oppintel.pipeline import Pipeline


def _permit(**overrides) -> Permit:
    defaults = dict(
        source_id="fort_worth_permits",
        permit_number="PB1",
        natural_key="PB1",
        permit_type="Commercial Mechanical Permit",
        permit_subtype="commercial_mechanical",
        permit_date=date(2026, 9, 1),
        status="Issued",
        address="100 MAIN ST",
        city="Fort Worth",
        state="TX",
        work_description="New construction of medical office building",
        land_use="OFFICE BUILDING",
        job_value=5_000_000.0,
        is_commercial=True,
        source_url="https://example.gov/PB1",
        source_date=date(2026, 9, 1),
    )
    defaults.update(overrides)
    return Permit(**defaults)


def _make_user(db: Database, email: str = "watcher@example.com") -> int:
    from werkzeug.security import generate_password_hash

    cursor = db.conn.execute(
        """
        INSERT INTO app_user (email, password_hash, display_name, access_level, is_active,
            created_at)
        VALUES (?, ?, 'Watcher', 'FREE', 1, ?)
        """,
        (email, generate_password_hash("correct-horse-battery"), "2026-09-01T00:00:00+00:00"),
    )
    user_id = int(cursor.lastrowid)
    db.conn.execute(
        """
        INSERT INTO user_preference (user_id, market_id, trade_id, cities, project_types,
            notify_in_app, notify_email, updated_at)
        VALUES (?, 'dfw', 'commercial_hvac', '[]', '[]', 1, 0, '2026-09-01T00:00:00+00:00')
        """,
        (user_id,),
    )
    db.conn.commit()
    return user_id


@pytest.fixture
def alert_db(tmp_path):
    db = Database(tmp_path / "alerts.db")
    db.init_schema()
    db.init_app_schema()
    pipeline = Pipeline(db)
    pipeline._register_sources()
    db.upsert_permit(_permit(), normalize_address("100 MAIN ST"))
    db.commit()
    pipeline.assemble_and_classify()
    yield db, pipeline
    db.close()


def _project_id(db: Database) -> int:
    return int(db.conn.execute("SELECT id FROM project LIMIT 1").fetchone()["id"])


def _watch(db: Database, user_id: int, project_id: int) -> None:
    from oppintel.app.workflow import WorkflowService

    WorkflowService(db).watch(user_id, project_id)


# --- real changes produce alerts ------------------------------------------------

def test_a_new_permit_raises_an_alert_for_a_watcher(alert_db):
    db, pipeline = alert_db
    user_id = _make_user(db)
    project_id = _project_id(db)
    _watch(db, user_id, project_id)

    db.upsert_permit(
        _permit(permit_number="PB2", natural_key="PB2", permit_date=date(2026, 9, 20)),
        normalize_address("100 MAIN ST"),
    )
    db.commit()
    pipeline.assemble_and_classify()

    result = build_alerts(db)
    assert result["created"] >= 1
    alerts = AlertService(db).for_user(user_id)
    assert any(a["kind"] == ALERT_NEW_PERMIT for a in alerts)


def test_an_alert_links_to_the_change_that_caused_it(alert_db):
    db, pipeline = alert_db
    user_id = _make_user(db)
    _watch(db, user_id, _project_id(db))
    db.upsert_permit(
        _permit(permit_number="PB9", natural_key="PB9", permit_date=date(2026, 9, 22)),
        normalize_address("100 MAIN ST"),
    )
    db.commit()
    pipeline.assemble_and_classify()
    build_alerts(db)

    alert = AlertService(db).for_user(user_id)[0]
    assert alert["change_id"] is not None, "an alert must reference its change row"
    assert alert["current_value"]


def test_an_alert_carries_the_previous_and_current_value(alert_db):
    db, pipeline = alert_db
    user_id = _make_user(db)
    _watch(db, user_id, _project_id(db))
    db.upsert_permit(_permit(status="Final CO Issued"), normalize_address("100 MAIN ST"))
    db.commit()
    pipeline.assemble_and_classify()
    build_alerts(db)

    alert = next(
        a for a in AlertService(db).for_user(user_id) if a["kind"] != ALERT_NEW_MATCH
    )
    assert alert["previous_value"] == "Issued"
    assert alert["current_value"] == "Final CO Issued"


# --- nothing else produces an alert ---------------------------------------------

def test_no_change_means_no_alert(alert_db):
    db, pipeline = alert_db
    user_id = _make_user(db)
    _watch(db, user_id, _project_id(db))
    # A second pass with identical data.
    pipeline.assemble_and_classify()
    result = build_alerts(db)
    assert result["created"] == 0
    assert AlertService(db).unread_count(user_id) == 0


def test_an_unwatched_project_raises_no_alert(alert_db):
    db, pipeline = alert_db
    user_id = _make_user(db)
    project_id = _project_id(db)
    db.upsert_permit(
        _permit(permit_number="PB5", natural_key="PB5", permit_date=date(2026, 9, 23)),
        normalize_address("100 MAIN ST"),
    )
    db.commit()
    pipeline.assemble_and_classify()

    build_alerts(db)
    assert AlertService(db).unread_count(user_id) == 0, (
        "only watched projects may alert; a save is a bookmark, not a monitoring promise"
    )
    # The change was still recorded, which is what makes the distinction meaningful.
    assert db.conn.execute("SELECT COUNT(*) FROM project_change").fetchone()[0] > 0


def test_a_user_with_notifications_off_receives_no_alert(alert_db):
    db, pipeline = alert_db
    user_id = _make_user(db)
    _watch(db, user_id, _project_id(db))
    db.conn.execute("UPDATE user_preference SET notify_in_app = 0 WHERE user_id = ?", (user_id,))
    db.conn.commit()

    db.upsert_permit(
        _permit(permit_number="PB6", natural_key="PB6", permit_date=date(2026, 9, 24)),
        normalize_address("100 MAIN ST"),
    )
    db.commit()
    pipeline.assemble_and_classify()
    build_alerts(db)
    assert AlertService(db).unread_count(user_id) == 0


def test_a_repeated_generation_pass_does_not_duplicate_alerts(alert_db):
    db, pipeline = alert_db
    user_id = _make_user(db)
    _watch(db, user_id, _project_id(db))
    db.upsert_permit(
        _permit(permit_number="PB7", natural_key="PB7", permit_date=date(2026, 9, 25)),
        normalize_address("100 MAIN ST"),
    )
    db.commit()
    pipeline.assemble_and_classify()

    build_alerts(db)
    first = AlertService(db).summary()["total"]
    build_alerts(db)
    assert AlertService(db).summary()["total"] == first


# --- the integrity check --------------------------------------------------------

def test_no_alert_exists_without_an_event(alert_db):
    db, pipeline = alert_db
    user_id = _make_user(db)
    _watch(db, user_id, _project_id(db))
    db.upsert_permit(
        _permit(permit_number="PB8", natural_key="PB8", permit_date=date(2026, 9, 26)),
        normalize_address("100 MAIN ST"),
    )
    db.commit()
    pipeline.assemble_and_classify()
    build_alerts(db)

    service = AlertService(db)
    assert service.alerts_without_event() == []
    assert service.summary()["orphaned"] == 0


def test_a_first_match_alert_is_the_only_kind_without_a_change(alert_db):
    db, _ = alert_db
    user_id = _make_user(db)
    service = AlertService(db)
    service.record_new_match(user_id, _project_id(db), "New match", "Matches your city.")
    assert service.summary()["orphaned"] == 0
    assert service.for_user(user_id)[0]["kind"] == ALERT_NEW_MATCH


def test_a_first_match_alert_is_recorded_at_most_once(alert_db):
    db, _ = alert_db
    user_id = _make_user(db)
    service = AlertService(db)
    assert service.record_new_match(user_id, _project_id(db), "New match", "A") is True
    assert service.record_new_match(user_id, _project_id(db), "New match", "A") is False
    assert service.summary()["total"] == 1


# --- reading and scoping --------------------------------------------------------

def test_marking_read_is_scoped_to_the_owner(alert_db):
    db, _ = alert_db
    owner = _make_user(db, "owner@example.com")
    other = _make_user(db, "other@example.com")
    service = AlertService(db)
    service.record_new_match(owner, _project_id(db), "New match", "A")
    alert_id = service.for_user(owner)[0]["id"]

    assert service.mark_read(other, alert_id) is False, "a user must not read another's alert"
    assert service.unread_count(owner) == 1
    assert service.mark_read(owner, alert_id) is True
    assert service.unread_count(owner) == 0


def test_mark_all_read_only_affects_the_caller(alert_db):
    db, _ = alert_db
    owner = _make_user(db, "owner2@example.com")
    other = _make_user(db, "other2@example.com")
    service = AlertService(db)
    service.record_new_match(owner, _project_id(db), "New match", "A")
    service.record_new_match(other, _project_id(db), "New match", "A")

    service.mark_all_read(owner)
    assert service.unread_count(owner) == 0
    assert service.unread_count(other) == 1


def test_unread_filter_returns_only_unread(alert_db):
    db, _ = alert_db
    user_id = _make_user(db)
    service = AlertService(db)
    service.record_new_match(user_id, _project_id(db), "New match", "A")
    service.mark_all_read(user_id)
    assert service.for_user(user_id, unread_only=True) == []
    assert len(service.for_user(user_id)) == 1
