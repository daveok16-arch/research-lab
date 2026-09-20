"""Tests for project assembly, clustering, and the persistent pipeline.

These exercise the real assembly code and a real SQLite database. No mocks.
"""

from __future__ import annotations

from datetime import date

from oppintel.assemble import assemble_project, cluster_permits, project_key_for
from oppintel.classify import classify
from oppintel.db import Database
from oppintel.models import normalize_address
from oppintel.pipeline import Pipeline

from conftest import make_permit

SOURCE_NAMES = {"fort_worth_permits": "City of Fort Worth Development Permits"}


# --- clustering ---------------------------------------------------------------

def test_permits_at_one_address_cluster_together(trade):
    permits = [
        make_permit(permit_number="PB1", permit_type="Commercial Building Permit"),
        make_permit(
            permit_number="PM1", permit_type="Mechanical", natural_key="PM1::Mechanical",
            permit_date=date(2026, 8, 1),
        ),
    ]
    clusters = cluster_permits(permits, trade)
    assert len(clusters) == 1
    assert len(next(iter(clusters.values()))) == 2


def test_permits_at_different_addresses_do_not_cluster(trade):
    permits = [
        make_permit(permit_number="PB1", address="100 MAIN ST"),
        make_permit(permit_number="PB2", address="200 OAK AVE", natural_key="PB2"),
    ]
    clusters = cluster_permits(permits, trade)
    assert len(clusters) == 2


def test_permits_far_apart_in_time_do_not_cluster(trade):
    permits = [
        make_permit(permit_number="PB1", permit_date=date(2020, 1, 1)),
        make_permit(permit_number="PB2", permit_date=date(2026, 1, 1), natural_key="PB2"),
    ]
    clusters = cluster_permits(permits, trade, window_days=540)
    # The largest single window wins; the two permits are years apart.
    assert len(next(iter(clusters.values()))) == 1


def test_residential_permits_never_form_projects(trade):
    permits = [
        make_permit(
            permit_type="Plumbing (PL) Single Family Alteration",
            land_use="SINGLE FAMILY DWELLING",
            is_commercial=None,
            work_description=None,
        ),
    ]
    clusters = cluster_permits(permits, trade)
    assert clusters == {}


def test_permits_without_addresses_are_dropped(trade):
    permits = [make_permit(address=None)]
    assert cluster_permits(permits, trade) == {}


def test_clustering_is_deterministic(trade):
    permits = [
        make_permit(permit_number="PB1"),
        make_permit(permit_number="PM1", permit_type="Mechanical", natural_key="PM1"),
    ]
    first = cluster_permits(permits, trade)
    second = cluster_permits(list(reversed(permits)), trade)
    assert list(first) == list(second)


def test_project_key_is_stable():
    key = project_key_for("100 main street", "commercial_hvac")
    assert key == project_key_for("100 main street", "commercial_hvac")
    assert len(key) == 20


# --- assembly -----------------------------------------------------------------

def test_assembly_attaches_mechanical_evidence_from_a_sibling_permit(trade):
    building = make_permit(permit_number="PB1", permit_type="Commercial Building Permit")
    mechanical = make_permit(
        permit_number="PM1", permit_type="Mechanical", natural_key="PM1::Mechanical",
        work_description=None, job_value=None, square_footage=None,
    )
    address_key = normalize_address("100 MAIN ST")
    project = assemble_project(address_key, [building, mechanical], trade, SOURCE_NAMES)

    assert project.mechanical_evidence_tier == 1
    assert "Mechanical" in project.mechanical_hvac_evidence
    assert project.permit_number == "PB1"
    assert project.estimated_project_value == 5_000_000.0


def test_building_permit_outranks_mechanical_for_primary_fields(trade):
    """The building permit carries value and area; the trade permit must not displace it."""
    mechanical = make_permit(
        permit_number="PM1", permit_type="Mechanical", natural_key="PM1::Mechanical",
        work_description=None, square_footage=12.0, job_value=95_000.0,
        permit_date=date(2026, 9, 10),
    )
    building = make_permit(
        permit_number="PB1", permit_type="Commercial Building Permit",
        square_footage=40_000.0, job_value=5_000_000.0,
    )
    address_key = normalize_address("100 MAIN ST")
    project = assemble_project(address_key, [mechanical, building], trade, SOURCE_NAMES)

    assert project.permit_number == "PB1"
    assert project.estimated_project_value == 5_000_000.0
    assert project.square_footage == 40_000.0


def test_owner_is_attached_as_a_party_with_evidence(trade):
    permit = make_permit(owner="TARRANT COUNTY COLLEGE DISTRICT")
    address_key = normalize_address("100 MAIN ST")
    project = assemble_project(address_key, [permit], trade, SOURCE_NAMES)

    assert project.owner == "TARRANT COUNTY COLLEGE DISTRICT"
    roles = {p.role for p in project.parties}
    assert "owner" in roles
    assert any(e.field_name == "owner" for e in project.evidence)


def test_unavailable_parties_stay_unverified(trade):
    """No free source in this market publishes the architect. It must not be invented."""
    permit = make_permit()
    address_key = normalize_address("100 MAIN ST")
    project = assemble_project(address_key, [permit], trade, SOURCE_NAMES)

    assert project.architect is None
    assert project.developer is None
    assert project.display("architect") == "Not verified."
    assert project.display("developer") == "Not verified."


def test_project_type_is_recorded_as_derived_not_sourced(trade):
    permit = make_permit(land_use="OFFICE BUILDING", work_description="New office building")
    address_key = normalize_address("100 MAIN ST")
    project = assemble_project(address_key, [permit], trade, SOURCE_NAMES)

    assert project.project_type == "Office"
    type_evidence = [e for e in project.evidence if e.field_name == "project_type"]
    assert type_evidence and type_evidence[0].evidence_type == "derived"


def test_no_placeholder_parties_are_created(trade):
    """A placeholder name would invent an entity. Only real source values are recorded."""
    mechanical = make_permit(permit_number="PM1", permit_type="Mechanical",
                             natural_key="PM1::Mechanical")
    address_key = normalize_address("100 MAIN ST")
    project = assemble_project(address_key, [mechanical], trade, SOURCE_NAMES)
    for party in project.parties:
        assert not party.name.startswith("("), f"placeholder party: {party.name}"


# --- end-to-end database ------------------------------------------------------

def test_pipeline_persists_projects_with_evidence(tmp_path):
    db = Database(tmp_path / "test.db")
    db.init_schema()
    pipeline = Pipeline(db)
    pipeline._register_sources()

    # Insert permits directly rather than over the network, so the test is offline and
    # still exercises the real persistence path.
    from oppintel.models import Permit

    permits = [
        Permit(
            source_id="fort_worth_permits", permit_number="PB26-13010",
            natural_key="PB26-13010::Commercial Building Permit",
            permit_type="Commercial Building Permit", permit_subtype="New",
            permit_date=date(2026, 9, 16), status="Pending",
            address="300 TRINITY CAMPUS CIR", city="Fort Worth", state="TX",
            work_description=(
                "Renovation to existing data center which includes replacing interior "
                "partition walls and associated mechanical, electrical, and plumbing work."
            ),
            land_use="Office", job_value=2_000_000.0,
            owner="TARRANT COUNTY COLLEGE DISTRICT", is_commercial=True,
            source_url="https://example.gov/PB26-13010", source_date=date(2026, 9, 16),
        ),
    ]
    for permit in permits:
        db.upsert_permit(permit, normalize_address(permit.address))
    db.commit()

    report = pipeline.assemble_and_classify()
    assert report.projects_written == 1

    row = db.conn.execute("SELECT * FROM project").fetchone()
    assert row["address"] == "300 TRINITY CAMPUS CIR"
    assert row["owner"] == "TARRANT COUNTY COLLEGE DISTRICT"
    assert row["architect"] is None
    assert row["classification"] == "HIGH"
    assert row["mechanical_evidence_tier"] == 2

    evidence = db.conn.execute(
        "SELECT field_name, source_url FROM evidence WHERE project_id = ?",
        (row["id"],),
    ).fetchall()
    fields = {e["field_name"] for e in evidence}
    assert {"owner", "address", "project_status", "mechanical_hvac_evidence"} <= fields
    # Every evidence row points back at a source.
    assert all(e["source_url"] for e in evidence)

    # A classification history row is written for the transition.
    history = db.conn.execute("SELECT * FROM project_classification").fetchall()
    assert len(history) == 1
    assert history[0]["classification"] == "HIGH"

    db.close()


def test_reassembly_is_idempotent(tmp_path):
    """Running assembly twice must not duplicate projects or evidence."""
    db = Database(tmp_path / "test.db")
    db.init_schema()
    pipeline = Pipeline(db)
    pipeline._register_sources()

    from oppintel.models import Permit

    permit = Permit(
        source_id="fort_worth_permits", permit_number="PB1",
        natural_key="PB1", permit_type="Commercial Building Permit",
        permit_subtype="New", permit_date=date(2026, 9, 1), status="Issued",
        address="100 MAIN ST", city="Fort Worth", state="TX",
        work_description="New construction of office building",
        owner="ACME LLC", job_value=1_000_000.0, square_footage=10_000.0,
        is_commercial=True, source_url="https://example.gov/PB1",
    )
    db.upsert_permit(permit, normalize_address(permit.address))
    db.commit()

    pipeline.assemble_and_classify()
    first_count = db.conn.execute("SELECT COUNT(*) FROM project").fetchone()[0]
    first_evidence = db.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]

    pipeline.assemble_and_classify()
    assert db.conn.execute("SELECT COUNT(*) FROM project").fetchone()[0] == first_count
    assert db.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == first_evidence

    db.close()


def test_run_fails_loudly_for_an_unknown_source(tmp_path):
    db = Database(tmp_path / "test.db")
    db.init_schema()
    pipeline = Pipeline(db)
    try:
        pipeline.run(["no_such_source"])
    except ValueError as exc:
        assert "no_such_source" in str(exc)
    else:
        raise AssertionError("expected ValueError for an unknown source")
    finally:
        db.close()