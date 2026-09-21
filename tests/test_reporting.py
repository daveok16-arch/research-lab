"""Tests for the coverage and validation reports.

These assert the reporting properties the customer's question depends on: coverage dates are
reported per source, unverified fields are labelled rather than omitted, and a field backed
only by a historical source is not presented as confirmed.
"""

from __future__ import annotations

from datetime import date

from oppintel.db import Database
from oppintel.models import Permit, Project, normalize_address
from oppintel.pipeline import Pipeline
from oppintel.reporting import (
    CONFIRMED,
    NOT_VERIFIED,
    PARTIALLY_VERIFIED,
    coverage_report,
    field_verdict,
    validation_report,
)


def _seed(tmp_path, *, source_id="fort_worth_permits", coverage="current"):
    db = Database(tmp_path / "t.db")
    db.init_schema()
    pipeline = Pipeline(db)
    pipeline._register_sources()
    if coverage != "current":
        db.conn.execute(
            "UPDATE source SET market_coverage = ? WHERE id = ?", (coverage, source_id)
        )
        db.commit()

    permit = Permit(
        source_id=source_id,
        permit_number="PB1",
        natural_key="PB1",
        permit_type="Commercial Building Permit",
        permit_subtype="New",
        permit_date=date(2026, 9, 1),
        status="Issued",
        address="100 MAIN ST",
        city="Fort Worth",
        state="TX",
        work_description="New construction of medical office building",
        owner="ACME HEALTH LLC",
        job_value=5_000_000.0,
        square_footage=40_000.0,
        is_commercial=True,
        source_url="https://example.gov/PB1",
        source_date=date(2026, 9, 1),
    )
    db.upsert_permit(permit, normalize_address(permit.address))
    db.commit()
    pipeline.assemble_and_classify()
    return db


def test_coverage_report_has_the_required_columns(tmp_path):
    db = _seed(tmp_path)
    report = coverage_report(db)
    for column in ("City", "Source", "Earliest Date", "Latest Date", "Records",
                   "Commercial Records", "Mechanical Evidence", "Notes"):
        assert column in report
    db.close()


def test_coverage_report_shows_actual_dates_not_nulls(tmp_path):
    db = _seed(tmp_path)
    report = coverage_report(db)
    assert "2026-09-01" in report
    db.close()


def test_coverage_report_separates_projects_by_city(tmp_path):
    db = _seed(tmp_path)
    report = coverage_report(db)
    assert "Fort Worth" in report
    db.close()


def test_validation_report_labels_missing_fields_as_not_verified(tmp_path):
    db = _seed(tmp_path)
    row = db.conn.execute("SELECT id FROM project").fetchone()
    report = validation_report(db, [row["id"]])
    assert "NOT VERIFIED" in report
    # The architect is genuinely absent from every free source and must be labelled as such.
    assert "architect" in report
    assert "no public source could substantiate it" in report
    db.close()


def test_validation_report_includes_classification_reasons(tmp_path):
    db = _seed(tmp_path)
    row = db.conn.execute("SELECT id FROM project").fetchone()
    report = validation_report(db, [row["id"]])
    assert "Classification reasons" in report
    assert "Mechanical" in report or "No mechanical" in report
    db.close()


def test_current_source_field_is_confirmed(tmp_path):
    db = _seed(tmp_path, coverage="current")
    row = db.conn.execute("SELECT id, owner FROM project").fetchone()
    verdict, evidence = field_verdict(db, row["id"], "owner", row["owner"])
    assert verdict == CONFIRMED
    assert evidence
    db.close()


def test_historical_source_field_is_only_partially_verified(tmp_path):
    """A value backed solely by a source that stopped publishing is not 'confirmed'."""
    db = _seed(tmp_path, source_id="dallas_gis_permits", coverage="historical")
    row = db.conn.execute("SELECT id, owner FROM project").fetchone()
    verdict, _ = field_verdict(db, row["id"], "owner", row["owner"])
    assert verdict == PARTIALLY_VERIFIED
    db.close()


def test_absent_field_has_no_verdict_but_not_verified(tmp_path):
    db = _seed(tmp_path)
    row = db.conn.execute("SELECT id, architect FROM project").fetchone()
    verdict, evidence = field_verdict(db, row["id"], "architect", None)
    assert verdict == NOT_VERIFIED
    assert evidence == []
    db.close()


def test_value_without_evidence_is_treated_as_unverified(tmp_path):
    """A field set with no supporting row should be reported as unsupported, not trusted."""
    db = _seed(tmp_path)
    row = db.conn.execute("SELECT id FROM project").fetchone()
    verdict, _ = field_verdict(db, row["id"], "owner", "SOMEONE NOT IN EVIDENCE")
    assert verdict == NOT_VERIFIED
    db.close()


def test_validation_report_states_when_there_are_no_discrepancies(tmp_path):
    db = _seed(tmp_path)
    row = db.conn.execute("SELECT id FROM project").fetchone()
    report = validation_report(db, [row["id"]])
    assert "No source discrepancies detected" in report
    db.close()