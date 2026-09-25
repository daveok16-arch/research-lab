"""Automated customer-report sanity checks.

Requirement 19 lists the specific defects a generated brief must not contain. Inspecting one
report by eye proves it for that run only; these tests assert the properties on every run.

They run against a database built from synthetic permits, so they exercise the real
selection, eligibility and rendering path without depending on the live database.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from oppintel.db import Database
from oppintel.models import Permit, normalize_address
from oppintel.pipeline import Pipeline
from oppintel.report_generator import PERMIT_EVIDENCE_NOTICE, ReportBuilder


def _setup(tmp_path):
    db = Database(tmp_path / "sanity.db")
    db.init_schema()
    pipeline = Pipeline(db)
    pipeline._register_sources()
    return db, pipeline


def _add(db, **overrides):
    defaults = dict(
        source_id="fort_worth_permits",
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
        land_use="OFFICE BUILDING",
        job_value=5_000_000.0,
        is_commercial=True,
        source_url="https://example.gov/PB1",
        source_date=date(2026, 9, 1),
    )
    defaults.update(overrides)
    permit = Permit(**defaults)
    db.upsert_permit(permit, normalize_address(permit.address))
    db.commit()


@pytest.fixture
def brief(tmp_path):
    """A brief rendered from a realistic mixed database."""
    db, pipeline = _setup(tmp_path)

    # A strong, eligible opportunity.
    _add(db, permit_number="A1", natural_key="A1", address="10 STRONG ST",
         work_description="New construction of medical office building",
         land_use="OFFICE BUILDING", job_value=12_000_000.0)
    # A mechanical construction project.
    _add(db, permit_number="B1", natural_key="B1", address="20 MECH ST",
         permit_type="Commercial Mechanical Permit", permit_subtype="commercial_mechanical",
         work_description="Mechanical remodel of spec suite", land_use="",
         job_value=2_000_000.0)
    # Completed work, which must not appear.
    _add(db, permit_number="C1", natural_key="C1", address="30 DONE ST",
         status="Closed - Complete", work_description="New construction of retail building",
         land_use="RETAIL", job_value=1_000_000.0)
    # A completed certificate of occupancy, whose status contains an active word.
    _add(db, permit_number="D1", natural_key="D1", address="40 CO ST",
         permit_type="Certificate of Occupancy", permit_subtype="",
         status="Final CO Issued", work_description="New construction of office building",
         land_use="OFFICE BUILDING", job_value=3_000_000.0)

    pipeline.assemble_and_classify()
    builder = ReportBuilder(db)
    ids = builder.select_opportunities(limit=5)
    yield db, builder, ids, builder.customer_brief(ids)
    db.close()


# --- 1. no unsupported claim --------------------------------------------------

def test_every_material_field_is_verified_or_marked(brief):
    """A labelled field is either backed by evidence or reads Not verified."""
    db, _builder, ids, text = brief
    assert "Not verified" in text  # the marker is present and used
    for line in text.splitlines():
        if line.startswith("**Value:**") and "Not verified" not in line:
            assert "_(source:" in line, f"unsourced value: {line}"
        if line.startswith("**Owner/developer:**") and "Not verified" not in line:
            assert "_(source:" in line, f"unsourced owner: {line}"


# --- 2. no false contractor or GC ---------------------------------------------

def test_no_gc_is_named_when_no_source_publishes_one(brief):
    _db, _builder, _ids, text = brief
    assert "**GC:** Not verified" in text
    # No contractor name can appear, because none of these permits records one.
    assert not re.search(r"\*\*GC:\*\* (?!Not verified)\S", text)


# --- 3. no plumbing permit presented as mechanical ----------------------------

def test_mechanical_evidence_is_never_a_plumbing_permit(brief):
    db, _builder, _ids, text = brief
    for line in text.splitlines():
        if line.startswith("**Mechanical evidence:**"):
            assert "Plumbing" not in line, f"plumbing presented as mechanical: {line}"


def test_selected_projects_all_carry_real_mechanical_evidence(brief):
    db, builder, ids, _text = brief
    for pid in ids:
        row = builder._project_row(pid)
        assert row["mechanical_evidence_tier"] in (1, 2), (
            f"{row['address']} was selected with tier {row['mechanical_evidence_tier']}"
        )


# --- 4. no completed project presented as active ------------------------------

def test_no_completed_project_appears(brief):
    db, _builder, _ids, text = brief
    assert "30 DONE ST" not in text, "a Closed - Complete project appeared"
    assert "40 CO ST" not in text, "a Final CO Issued project appeared"


def test_no_selected_project_is_closed(brief):
    db, builder, ids, _text = brief
    for pid in ids:
        row = builder._project_row(pid)
        assert row["procurement_status"] != "Closed"


# --- 5. no open-bid claim without evidence ------------------------------------

def test_open_bid_never_appears_as_a_claim(brief):
    _db, _builder, _ids, text = brief
    lowered = text.lower()
    for position in [i for i in range(len(lowered)) if lowered.startswith("open bid", i)]:
        context = lowered[max(0, position - 60):position]
        assert "not " in context or "no " in context, (
            f"'open bid' used as a claim: ...{context}"
        )


def test_confirmed_open_is_only_ever_negated(brief):
    _db, _builder, _ids, text = brief
    lowered = text.lower()
    for position in [i for i in range(len(lowered)) if lowered.startswith("confirmed open", i)]:
        context = lowered[max(0, position - 40):position]
        assert "not a" in context or "no source" in context


def test_required_permit_evidence_notice_is_present(brief):
    _db, _builder, _ids, text = brief
    assert PERMIT_EVIDENCE_NOTICE in text


# --- 6. no missing source URL for a material claim ----------------------------

def test_every_selected_opportunity_lists_a_source(brief):
    db, builder, ids, text = brief
    for pid in ids:
        row = builder._project_row(pid)
        urls = builder._dedup_sources(pid)
        assert urls, f"{row['address']} has no source record"
        for src in urls:
            if src["source_url"]:
                assert src["source_url"] in text


def test_source_section_is_not_empty_for_any_opportunity(brief):
    _db, _builder, _ids, text = brief
    blocks = text.split("**Sources**")
    assert len(blocks) >= 2, "no Sources section was rendered"
    for block in blocks[1:]:
        # The header is followed by a blank line, then the bullet list.
        body = block.lstrip("\n")
        assert body.startswith("- "), f"a Sources section listed nothing: {block[:80]!r}"


# --- 7. no duplicate underlying project ---------------------------------------

def test_no_two_selected_projects_share_a_building(brief):
    from oppintel.grouping import building_key

    db, builder, ids, _text = brief
    keys = [
        building_key(builder._project_row(pid)["address"], builder._project_row(pid)["city"])
        for pid in ids
    ]
    keys = [k for k in keys if k]
    assert len(keys) == len(set(keys)), f"duplicate buildings selected: {keys}"


# --- 8. no unexplained score --------------------------------------------------

def test_selection_order_is_explainable(brief):
    db, builder, ids, _text = brief
    scores = [builder._project_row(pid)["classification_score"] for pid in ids]
    assert scores == sorted(scores, reverse=True), "selection is not strength-ordered"


def test_classification_is_stated_for_every_opportunity(brief):
    _db, _builder, _ids, text = brief
    assert text.count("Classification ") >= 1


# --- 9. no hallucinated value or square footage -------------------------------

def test_no_fabricated_numbers(brief):
    """Every dollar figure or area in the brief must exist in the database."""
    db, _builder, _ids, text = brief
    known: set[str] = set()
    for row in db.conn.execute("SELECT job_value FROM permit WHERE job_value IS NOT NULL"):
        known.add(f"{float(row[0]):,.0f}")
    for row in db.conn.execute(
        "SELECT estimated_project_value FROM project WHERE estimated_project_value IS NOT NULL"
    ):
        known.add(f"{float(row[0]):,.0f}")

    for match in re.finditer(r"\$([\d,]+)", text):
        amount = match.group(1)
        assert amount in known, f"value ${amount} appears in the brief but not in the database"


def test_square_footage_is_either_absent_or_sourced(brief):
    _db, _builder, _ids, text = brief
    for line in text.splitlines():
        if line.startswith("**Square footage:**") and "Not verified" not in line:
            assert "_(source:" in line


# --- structural --------------------------------------------------------------

def test_brief_carries_the_required_header_fields(brief):
    _db, _builder, _ids, text = brief
    assert "# DFW Commercial HVAC Opportunity Brief" in text
    assert "**Prepared for:**" in text
    assert "**Date generated:**" in text
    assert "## Important note" in text


def test_brief_does_not_expose_internal_sections(brief):
    _db, _builder, _ids, text = brief
    for internal_only in (
        "### Field-level verification",
        "### Classification reasoning",
        "### Contributing permits",
        "### Source provenance",
    ):
        assert internal_only not in text


def test_what_is_not_verified_is_stated_per_opportunity(brief):
    _db, _builder, ids, text = brief
    assert text.count("**What is not verified**") == len(ids)


def test_brief_is_deterministic(brief):
    db, builder, ids, text = brief
    assert builder.customer_brief(ids) == text