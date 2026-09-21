"""Tests for multi-source contradiction handling.

The rule under test: when two sources disagree, both facts are retained and the disagreement
is recorded. Nothing is merged, averaged, or silently resolved.
"""

from __future__ import annotations

from datetime import date

from oppintel.discrepancy import (
    find_discrepancies,
    mark_disputed,
    values_agree,
)
from oppintel.models import Project
from oppintel.provenance import assert_field


def _assert(project: Project, field: str, value, source_id: str, source_name: str, url: str):
    assert_field(
        project, field, value,
        source_id=source_id, source_name=source_name, source_url=url,
        source_date=date(2026, 9, 1),
    )


def test_agreeing_values_are_not_a_discrepancy():
    p = Project(project_key="x")
    _assert(p, "estimated_project_value", 2_000_000.0, "fw", "Fort Worth", "https://a")
    _assert(p, "estimated_project_value", 2_000_000.0, "dallas", "Dallas", "https://b")
    assert find_discrepancies(p) == []


def test_rounding_differences_are_not_treated_as_disputes():
    """Sources round differently; a 0.5% difference is the same fact, not a contradiction."""
    assert values_agree(4_300_000.0, 4_300_000.0)
    assert values_agree(4_300_000, "4300000")
    assert values_agree(100.0, 100.4)


def test_materially_different_values_are_a_discrepancy():
    """The documented example: TDLR $250M versus the city permit $264M."""
    p = Project(project_key="x")
    _assert(p, "estimated_project_value", 250_000_000.0, "tdlr", "TDLR", "https://tdlr")
    _assert(p, "estimated_project_value", 264_000_000.0, "city", "City permit", "https://city")

    found = find_discrepancies(p)
    assert len(found) == 1
    assert found[0].field_name == "estimated_project_value"
    assert len(found[0].values) == 2
    values = {v["value"] for v in found[0].values}
    assert values == {"250000000.0", "264000000.0"}


def test_both_values_and_their_sources_are_preserved():
    """Neither figure may be dropped, and each keeps its own citation."""
    p = Project(project_key="x")
    _assert(p, "estimated_project_value", 250_000_000.0, "tdlr", "TDLR", "https://tdlr/1")
    _assert(p, "estimated_project_value", 264_000_000.0, "city", "City permit", "https://city/1")

    found = find_discrepancies(p)
    urls = {v["source_url"] for v in found[0].values}
    names = {v["source_name"] for v in found[0].values}
    assert urls == {"https://tdlr/1", "https://city/1"}
    assert names == {"TDLR", "City permit"}


def test_disputed_field_is_flagged_not_silently_merged():
    p = Project(project_key="x")
    _assert(p, "estimated_project_value", 250_000_000.0, "tdlr", "TDLR", "https://tdlr")
    _assert(p, "estimated_project_value", 264_000_000.0, "city", "City permit", "https://city")

    mark_disputed(p, find_discrepancies(p))
    assert "estimated_project_value" in p.disputed_fields
    assert any("Sources disagree" in r for r in p.classification_reasons)
    # Neither figure is averaged or merged away: both survive as separate sourced facts.
    recorded = {e.value for e in p.evidence if e.field_name == "estimated_project_value"}
    assert recorded == {"250000000.0", "264000000.0"}


def test_discrepancy_records_both_values_for_display():
    p = Project(project_key="x")
    _assert(p, "estimated_project_value", 250_000_000.0, "tdlr", "TDLR", "https://tdlr")
    _assert(p, "estimated_project_value", 264_000_000.0, "city", "City permit", "https://city")
    mark_disputed(p, find_discrepancies(p))

    item = p.discrepancies[0]
    assert item["field_name"] == "estimated_project_value"
    assert len(item["values"]) == 2


def test_text_disagreement_is_detected():
    p = Project(project_key="x")
    _assert(p, "owner", "ACME HOLDINGS LLC", "a", "Source A", "https://a")
    _assert(p, "owner", "ACME HOLDINGS INC", "b", "Source B", "https://b")
    found = find_discrepancies(p)
    assert len(found) == 1 and found[0].field_name == "owner"


def test_case_and_whitespace_differences_are_not_disputes():
    p = Project(project_key="x")
    _assert(p, "owner", "ACME LLC", "a", "Source A", "https://a")
    _assert(p, "owner", "acme  llc", "b", "Source B", "https://b")
    assert find_discrepancies(p) == []


def test_three_way_disagreement_reports_all_values():
    p = Project(project_key="x")
    _assert(p, "square_footage", 10_000.0, "a", "A", "https://a")
    _assert(p, "square_footage", 12_000.0, "b", "B", "https://b")
    _assert(p, "square_footage", 14_000.0, "c", "C", "https://c")
    found = find_discrepancies(p)
    assert len(found) == 1
    assert len(found[0].values) == 3


def test_single_source_is_never_a_discrepancy():
    p = Project(project_key="x")
    _assert(p, "estimated_project_value", 1.0, "a", "A", "https://a")
    assert find_discrepancies(p) == []


def test_no_evidence_is_never_a_discrepancy():
    assert find_discrepancies(Project(project_key="x")) == []


def test_mark_disputed_on_no_discrepancies_is_a_no_op():
    p = Project(project_key="x")
    mark_disputed(p, [])
    assert p.disputed_fields == []
    assert p.discrepancies == []


def test_pipeline_records_discrepancies_end_to_end(tmp_path):
    """Two sources disagreeing on one project must surface through the real pipeline."""
    from oppintel.db import Database
    from oppintel.models import Permit, normalize_address
    from oppintel.pipeline import Pipeline

    db = Database(tmp_path / "t.db")
    db.init_schema()
    pipeline = Pipeline(db)
    pipeline._register_sources()

    common = dict(
        permit_number="PB1", permit_type="Commercial Building Permit",
        permit_subtype="New", permit_date=date(2026, 9, 1), status="Issued",
        address="100 MAIN ST", city="Fort Worth", state="TX",
        work_description="New construction of medical office building",
        is_commercial=True,
    )
    a = Permit(source_id="fort_worth_permits", natural_key="A", job_value=250_000_000.0,
               owner="ACME LLC", **common)
    b = Permit(source_id="collin_cad_permits", natural_key="B", job_value=264_000_000.0,
               owner="ACME LLC", **common)

    for permit in (a, b):
        db.upsert_permit(permit, normalize_address(permit.address))
    db.commit()
    pipeline.assemble_and_classify()

    row = db.conn.execute("SELECT * FROM project").fetchone()
    import json

    disputed = json.loads(row["disputed_fields"])
    assert "estimated_project_value" in disputed
    details = json.loads(row["discrepancies"])
    value_rows = [d for d in details if d["field_name"] == "estimated_project_value"]
    assert len(value_rows) == 1
    assert len(value_rows[0]["values"]) == 2
    db.close()