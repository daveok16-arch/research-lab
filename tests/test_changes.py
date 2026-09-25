"""Change detection tests.

The property under test is that the monitoring layer records *real differences and nothing
else*. A change detector that emits events when nothing moved produces an alert feed nobody
trusts, and a detector that misses a real change makes the monitoring promise hollow. Both
directions are asserted here, against a real database built by the real pipeline.
"""

from __future__ import annotations

from datetime import date

import pytest

from oppintel.changes import (
    CLOSED,
    MECHANICAL_ADDED,
    NEW_PERMIT,
    NEW_PROJECT,
    NOTIFIABLE_KINDS,
    diff_project,
    snapshot_values,
    state_hash,
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


@pytest.fixture
def monitored_db(tmp_path):
    """A database with one assembled project, ready to diff on a second pass."""
    db = Database(tmp_path / "monitor.db")
    db.init_schema()
    db.init_app_schema()
    pipeline = Pipeline(db)
    pipeline._register_sources()
    db.upsert_permit(_permit(), normalize_address("100 MAIN ST"))
    db.commit()
    pipeline.assemble_and_classify()
    yield db, pipeline
    db.close()


def _changes(db) -> list[dict]:
    return [dict(r) for r in db.conn.execute(
        "SELECT * FROM project_change ORDER BY id"
    ).fetchall()]


# --- the first pass records entry, not a fabricated history --------------------

def test_first_pass_records_a_new_project_event(monitored_db):
    db, _ = monitored_db
    changes = _changes(db)
    assert len(changes) == 1
    assert changes[0]["change_kind"] == NEW_PROJECT
    assert "entered the system" in changes[0]["summary"]


def test_first_pass_does_not_invent_earlier_history(monitored_db):
    """Only the observed event exists. No backdated 'permit discovered' entry is created."""
    db, _ = monitored_db
    kinds = {c["change_kind"] for c in _changes(db)}
    assert kinds == {NEW_PROJECT}


# --- a no-op pass emits nothing -------------------------------------------------

def test_a_pass_with_no_new_data_records_no_change(monitored_db):
    db, pipeline = monitored_db
    before = len(_changes(db))
    pipeline.assemble_and_classify()
    assert len(_changes(db)) == before, "a re-run with identical data must not create an event"


def test_repeated_noop_passes_remain_silent(monitored_db):
    db, pipeline = monitored_db
    before = len(_changes(db))
    for _ in range(3):
        pipeline.assemble_and_classify()
    assert len(_changes(db)) == before


# --- real changes are detected, with before and after --------------------------

def test_a_new_permit_at_the_address_is_detected(monitored_db):
    db, pipeline = monitored_db
    db.upsert_permit(
        _permit(permit_number="PB2", natural_key="PB2", permit_date=date(2026, 9, 20)),
        normalize_address("100 MAIN ST"),
    )
    db.commit()
    pipeline.assemble_and_classify()

    new_permit = [c for c in _changes(db) if c["change_kind"] == NEW_PERMIT]
    assert new_permit, "a newly filed permit must be detected"
    assert "PB2" in new_permit[0]["summary"]


def test_a_status_change_is_detected_with_previous_and_current(monitored_db):
    db, pipeline = monitored_db
    db.upsert_permit(
        _permit(status="Final CO Issued"), normalize_address("100 MAIN ST")
    )
    db.commit()
    pipeline.assemble_and_classify()

    status_changes = [
        c for c in _changes(db)
        if c["field_name"] == "project_status" and c["change_kind"] != NEW_PROJECT
    ]
    assert status_changes
    assert status_changes[-1]["previous_value"] == "Issued"
    assert status_changes[-1]["current_value"] == "Final CO Issued"


def test_a_closure_is_recorded_as_a_closure(monitored_db):
    db, pipeline = monitored_db
    db.upsert_permit(_permit(status="Final CO Issued"), normalize_address("100 MAIN ST"))
    db.commit()
    pipeline.assemble_and_classify()

    assert any(c["change_kind"] == CLOSED for c in _changes(db)), (
        "a project becoming closed must be recorded as a closure, not a generic status change"
    )


def test_a_value_change_is_detected(monitored_db):
    db, pipeline = monitored_db
    db.upsert_permit(
        _permit(job_value=7_500_000.0), normalize_address("100 MAIN ST")
    )
    db.commit()
    pipeline.assemble_and_classify()

    value_changes = [c for c in _changes(db) if c["field_name"] == "estimated_project_value"]
    assert value_changes
    assert "7,500,000" in value_changes[-1]["summary"]


def test_a_change_carries_a_source_link_where_the_source_publishes_one(monitored_db):
    db, pipeline = monitored_db
    db.upsert_permit(
        _permit(permit_number="PB3", natural_key="PB3", permit_date=date(2026, 9, 21)),
        normalize_address("100 MAIN ST"),
    )
    db.commit()
    pipeline.assemble_and_classify()

    new_permit = [c for c in _changes(db) if c["change_kind"] == NEW_PERMIT][0]
    assert new_permit["source_url"], "a change must carry the source that evidences it"


# --- the diff function, exercised directly -------------------------------------

def test_diff_returns_nothing_when_values_are_equal():
    project = {"id": 1, "classification": "HIGH", "procurement_status": "Evidence found, status unclear"}
    values = snapshot_values(project)
    assert diff_project(project, values) == []


def test_diff_reports_first_sight_as_a_new_project():
    project = {"id": 7, "address": "10 ROSS AVE"}
    changes = diff_project(project, None)
    assert len(changes) == 1
    assert changes[0].change_kind == NEW_PROJECT
    assert changes[0].project_id == 7


def test_diff_normalises_whitespace_and_numeric_formatting():
    """A serialisation difference must not manufacture an event."""
    project = {"id": 1, "owner": "ACME LLC", "estimated_project_value": 5.0}
    previous = snapshot_values({"id": 1, "owner": "  ACME LLC ", "estimated_project_value": 5})
    assert diff_project(project, previous) == []


def test_diff_treats_an_empty_string_as_absent():
    """A source starting to publish an empty field has not changed a fact."""
    project = {"id": 1, "developer": None}
    previous = snapshot_values({"id": 1, "developer": ""})
    assert diff_project(project, previous) == []


def test_mechanical_evidence_gain_is_classified_as_added():
    project = {"id": 1, "mechanical_evidence_tier": 2}
    previous = snapshot_values({"id": 1, "mechanical_evidence_tier": None})
    changes = diff_project(project, previous)
    assert [c.change_kind for c in changes] == [MECHANICAL_ADDED]
    assert changes[0].is_notifiable


def test_state_hash_is_stable_across_key_order():
    assert state_hash({"a": 1, "b": 2}) == state_hash({"b": 2, "a": 1})


def test_state_hash_changes_when_a_tracked_value_changes():
    assert state_hash({"a": 1}) != state_hash({"a": 2})


def test_notifiable_kinds_exclude_plain_corrections():
    """Routine corrections appear on the timeline but must not raise an alert."""
    assert "evidence_updated" not in NOTIFIABLE_KINDS
    assert NEW_PERMIT in NOTIFIABLE_KINDS
    assert CLOSED in NOTIFIABLE_KINDS


# --- monitoring is optional when the application tables are absent -------------

def test_intelligence_only_database_still_assembles(tmp_path):
    """The intelligence layer must work against a database the web app never touched."""
    db = Database(tmp_path / "pure.db")
    db.init_schema()
    assert db.has_app_schema() is False
    pipeline = Pipeline(db)
    pipeline._register_sources()
    db.upsert_permit(_permit(), normalize_address("100 MAIN ST"))
    db.commit()
    pipeline.assemble_and_classify()
    assert db.conn.execute("SELECT COUNT(*) FROM project").fetchone()[0] == 1
    db.close()
