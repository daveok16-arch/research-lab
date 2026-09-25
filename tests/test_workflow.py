"""Workflow tests: watching, pipeline, notes, tags, assignment and activity.

These are the account's own working record over an opportunity, so the tests are about
ownership and vocabulary as much as behaviour:

* One account must never see or alter another's private record.
* A pipeline stage is the account's own working state; it must not be confused with the
  project's procurement status.
* A watch must not demote a project already being pursued in the pipeline.
"""

from __future__ import annotations

import pytest

from oppintel.app.workflow import (
    DEFAULT_STAGE,
    MAX_NOTE_LENGTH,
    PIPELINE_STAGES,
    STAGE_LABELS,
    WorkflowError,
    WorkflowService,
    clean_tag,
    normalise_stage,
)
from oppintel.db import Database


@pytest.fixture
def workflow_db(fixture_db):
    """The shared fixture database, plus two accounts with no organization between them."""
    db = fixture_db
    for email in ("first@example.com", "second@example.com"):
        db.conn.execute(
            """
            INSERT INTO app_user (email, password_hash, display_name, access_level, is_active,
                created_at)
            VALUES (?, 'x', ?, 'FREE', 1, '2026-09-01T00:00:00+00:00')
            """,
            (email, email.split("@")[0]),
        )
    db.conn.commit()
    return db


def _user_ids(db: Database) -> tuple[int, int]:
    rows = db.conn.execute("SELECT id FROM app_user ORDER BY id").fetchall()
    return int(rows[0]["id"]), int(rows[1]["id"])


def _project_id(db: Database) -> int:
    return int(db.conn.execute("SELECT id FROM project LIMIT 1").fetchone()["id"])


# --- watching -------------------------------------------------------------------

def test_watching_records_the_relationship(workflow_db):
    db = workflow_db
    user, _ = _user_ids(db)
    service = WorkflowService(db)
    project_id = _project_id(db)

    assert service.watch(user, project_id) is True
    assert service.is_watched(user, project_id) is True
    assert service.watched_ids(user) == [project_id]


def test_watching_twice_is_idempotent(workflow_db):
    db = workflow_db
    user, _ = _user_ids(db)
    service = WorkflowService(db)
    project_id = _project_id(db)
    service.watch(user, project_id)
    assert service.watch(user, project_id) is False


def test_unwatching_removes_the_relationship(workflow_db):
    db = workflow_db
    user, _ = _user_ids(db)
    service = WorkflowService(db)
    project_id = _project_id(db)
    service.watch(user, project_id)
    assert service.unwatch(user, project_id) is True
    assert service.is_watched(user, project_id) is False


def test_watching_a_missing_project_is_refused(workflow_db):
    db = workflow_db
    user, _ = _user_ids(db)
    assert WorkflowService(db).watch(user, 999999) is False


def test_watching_places_the_project_in_the_pipeline(workflow_db):
    db = workflow_db
    user, _ = _user_ids(db)
    service = WorkflowService(db)
    project_id = _project_id(db)
    service.watch(user, project_id)
    assert service.stage_for(user, project_id) == "WATCHING"


def test_watching_does_not_demote_a_project_already_in_pursuit(workflow_db):
    """The promise: a watch asks for monitoring, it does not reset the account's own workflow."""
    db = workflow_db
    user, _ = _user_ids(db)
    service = WorkflowService(db)
    project_id = _project_id(db)
    service.set_stage(user, project_id, "PURSUING")
    service.watch(user, project_id)
    assert service.stage_for(user, project_id) == "PURSUING"


# --- pipeline -------------------------------------------------------------------

def test_every_declared_stage_is_settable(workflow_db):
    db = workflow_db
    user, _ = _user_ids(db)
    service = WorkflowService(db)
    project_id = _project_id(db)
    for key, _label in PIPELINE_STAGES:
        service.set_stage(user, project_id, key)
        assert service.stage_for(user, project_id) == key


def test_an_unknown_stage_falls_back_to_the_entry_stage(workflow_db):
    db = workflow_db
    user, _ = _user_ids(db)
    service = WorkflowService(db)
    project_id = _project_id(db)
    service.set_stage(user, project_id, "NOT_A_STAGE")
    assert service.stage_for(user, project_id) == DEFAULT_STAGE


def test_stage_labels_are_distinct_from_the_procurement_vocabulary(workflow_db):
    """A stage names the account's work, not the project's procurement. They must not overlap."""
    from oppintel.procurement import PROCUREMENT_STATES

    assert not set(STAGE_LABELS.values()) & set(PROCUREMENT_STATES), (
        "a pipeline stage must not read as a procurement state"
    )


def test_pipeline_counts_cover_every_stage(workflow_db):
    db = workflow_db
    user, _ = _user_ids(db)
    WorkflowService(db).set_stage(user, _project_id(db), "TARGET")
    counts = WorkflowService(db).pipeline_counts(user)
    assert set(counts) == {key for key, _ in PIPELINE_STAGES}
    assert counts["TARGET"] == 1


def test_follow_up_date_is_stored_and_cleared(workflow_db):
    db = workflow_db
    user, _ = _user_ids(db)
    service = WorkflowService(db)
    project_id = _project_id(db)
    service.set_follow_up(user, project_id, "2026-10-01")
    row = WorkflowService(db).pipeline_rows(user)[0]
    assert row["follow_up_date"] == "2026-10-01"
    service.set_follow_up(user, project_id, None)
    assert WorkflowService(db).pipeline_rows(user)[0]["follow_up_date"] is None


def test_removing_from_the_pipeline_keeps_the_save_and_watch(workflow_db):
    db = workflow_db
    user, _ = _user_ids(db)
    service = WorkflowService(db)
    project_id = _project_id(db)
    service.watch(user, project_id)
    assert service.remove_from_pipeline(user, project_id) is True
    assert service.stage_for(user, project_id) is None
    assert service.is_watched(user, project_id) is True


# --- ownership isolation --------------------------------------------------------

def test_one_account_cannot_see_anothers_pipeline(workflow_db):
    db = workflow_db
    first, second = _user_ids(db)
    service = WorkflowService(db)
    project_id = _project_id(db)
    service.set_stage(first, project_id, "TARGET")
    assert service.stage_for(second, project_id) is None
    assert service.pipeline_rows(second) == []


def test_one_account_cannot_see_anothers_notes(workflow_db):
    db = workflow_db
    first, second = _user_ids(db)
    service = WorkflowService(db)
    project_id = _project_id(db)
    service.add_note(first, project_id, "Private to first")
    assert service.notes_for(second, project_id) == []


def test_deleting_a_note_is_scoped_to_the_owner(workflow_db):
    db = workflow_db
    first, second = _user_ids(db)
    service = WorkflowService(db)
    project_id = _project_id(db)
    note_id = service.add_note(first, project_id, "Mine")
    assert service.delete_note(second, note_id) is False
    assert service.notes_for(first, project_id) != []
    assert service.delete_note(first, note_id) is True


def test_one_account_cannot_see_anothers_tags(workflow_db):
    db = workflow_db
    first, second = _user_ids(db)
    service = WorkflowService(db)
    project_id = _project_id(db)
    service.add_tag(first, project_id, "priority")
    assert service.tags_for(second, project_id) == []


def test_one_account_cannot_remove_anothers_tag(workflow_db):
    db = workflow_db
    first, second = _user_ids(db)
    service = WorkflowService(db)
    project_id = _project_id(db)
    service.add_tag(first, project_id, "priority")
    assert service.remove_tag(second, project_id, "priority") is False
    assert WorkflowService(db).tags_for(first, project_id) == ["priority"]


def test_unwatched_state_is_per_account(workflow_db):
    db = workflow_db
    first, second = _user_ids(db)
    service = WorkflowService(db)
    project_id = _project_id(db)
    service.watch(first, project_id)
    assert service.is_watched(second, project_id) is False


# --- notes ----------------------------------------------------------------------

def test_a_note_is_stored_and_counted(workflow_db):
    db = workflow_db
    user, _ = _user_ids(db)
    service = WorkflowService(db)
    project_id = _project_id(db)
    service.add_note(user, project_id, "Called the GC")
    assert service.note_count(user, project_id) == 1
    assert service.notes_for(user, project_id)[0]["body"] == "Called the GC"


def test_an_empty_note_is_refused(workflow_db):
    db = workflow_db
    user, _ = _user_ids(db)
    with pytest.raises(WorkflowError):
        WorkflowService(db).add_note(user, _project_id(db), "   ")


def test_an_overlong_note_is_refused(workflow_db):
    db = workflow_db
    user, _ = _user_ids(db)
    with pytest.raises(WorkflowError):
        WorkflowService(db).add_note(user, _project_id(db), "x" * (MAX_NOTE_LENGTH + 1))


def test_a_note_on_a_missing_project_is_refused(workflow_db):
    db = workflow_db
    user, _ = _user_ids(db)
    with pytest.raises(WorkflowError):
        WorkflowService(db).add_note(user, 999999, "text")


# --- tags -----------------------------------------------------------------------

def test_tags_are_stored_sorted(workflow_db):
    db = workflow_db
    user, _ = _user_ids(db)
    service = WorkflowService(db)
    project_id = _project_id(db)
    service.add_tag(user, project_id, "zebra")
    service.add_tag(user, project_id, "alpha")
    assert service.tags_for(user, project_id) == ["alpha", "zebra"]


def test_adding_the_same_tag_twice_is_idempotent(workflow_db):
    db = workflow_db
    user, _ = _user_ids(db)
    service = WorkflowService(db)
    project_id = _project_id(db)
    assert service.add_tag(user, project_id, "priority") is True
    assert service.add_tag(user, project_id, "priority") is False


def test_tags_are_sanitised(workflow_db):
    """Tags are rendered back into markup, so the characters are stripped at the boundary."""
    assert clean_tag("<script>alert(1)</script>") == "script alert 1 /script"
    assert clean_tag("  hello   world  ") == "hello world"
    assert clean_tag("a" * 100) == "a" * 40


def test_an_empty_tag_is_refused(workflow_db):
    db = workflow_db
    user, _ = _user_ids(db)
    with pytest.raises(WorkflowError):
        WorkflowService(db).add_tag(user, _project_id(db), "   ")


def test_tag_count_is_capped(workflow_db):
    db = workflow_db
    user, _ = _user_ids(db)
    service = WorkflowService(db)
    project_id = _project_id(db)
    for index in range(20):
        service.add_tag(user, project_id, f"tag{index}")
    with pytest.raises(WorkflowError):
        service.add_tag(user, project_id, "one-too-many")


# --- assignment and organization ------------------------------------------------

def test_a_user_without_an_organization_has_no_peers(workflow_db):
    db = workflow_db
    user, _ = _user_ids(db)
    assert WorkflowService(db).peers_for(user) == []


def test_assignment_to_a_non_peer_is_refused(workflow_db):
    db = workflow_db
    first, second = _user_ids(db)
    with pytest.raises(WorkflowError):
        WorkflowService(db).assign(first, _project_id(db), second)


def test_assignment_to_a_peer_in_the_same_organization_is_allowed(workflow_db):
    db = workflow_db
    first, second = _user_ids(db)
    db.conn.execute(
        "INSERT INTO organization (name, slug, created_at) VALUES ('Acme', 'acme', '2026-09-01')"
    )
    org_id = int(db.conn.execute("SELECT id FROM organization").fetchone()["id"])
    for user_id in (first, second):
        db.conn.execute(
            "INSERT INTO organization_member (org_id, user_id, role, created_at) "
            "VALUES (?, ?, 'MEMBER', '2026-09-01')",
            (org_id, user_id),
        )
    db.conn.commit()

    service = WorkflowService(db)
    assert {p["id"] for p in service.peers_for(first)} == {second}
    assert service.assign(first, _project_id(db), second) is True


def test_assignment_can_be_cleared(workflow_db):
    db = workflow_db
    user, _ = _user_ids(db)
    service = WorkflowService(db)
    project_id = _project_id(db)
    service.assign(user, project_id, user)
    service.assign(user, project_id, None)
    assert service.pipeline_rows(user)[0]["assigned_to"] is None


# --- activity -------------------------------------------------------------------

def test_every_mutation_is_recorded_in_the_activity_log(workflow_db):
    db = workflow_db
    user, _ = _user_ids(db)
    service = WorkflowService(db)
    project_id = _project_id(db)
    service.watch(user, project_id)
    service.set_stage(user, project_id, "TARGET")
    service.add_note(user, project_id, "note")
    service.add_tag(user, project_id, "tag")

    actions = {row["action"] for row in service.activity_for(user, project_id)}
    assert {"watched", "pipeline_stage", "note_added", "tag_added"} <= actions


def test_activity_is_scoped_to_the_account(workflow_db):
    db = workflow_db
    first, second = _user_ids(db)
    service = WorkflowService(db)
    project_id = _project_id(db)
    service.watch(first, project_id)
    assert service.recent_activity(second) == []


# --- summary --------------------------------------------------------------------

def test_summary_counts_are_real(workflow_db):
    db = workflow_db
    user, _ = _user_ids(db)
    service = WorkflowService(db)
    project_id = _project_id(db)
    service.watch(user, project_id)
    service.add_note(user, project_id, "note")
    db.conn.execute(
        "INSERT INTO saved_opportunity (user_id, project_id, saved_at) VALUES (?, ?, ?)",
        (user, project_id, "2026-09-01"),
    )
    db.conn.commit()

    summary = service.summary(user)
    assert summary["watching"] == 1
    assert summary["notes"] == 1
    assert summary["saved"] == 1
    assert summary["pipeline"] == 1
    assert summary["stages"]["WATCHING"] == 1


def test_normalise_stage_is_case_insensitive():
    assert normalise_stage("pursuing") == "PURSUING"
    assert normalise_stage(None) == DEFAULT_STAGE
    assert normalise_stage("") == DEFAULT_STAGE
