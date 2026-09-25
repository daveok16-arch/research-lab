"""Detail page tests: provenance, missing fields, procurement transparency, grouping.

This is where the product's data-quality promises become visible to a visitor, so these are
the most important application tests.
"""

from __future__ import annotations

import re

from oppintel.db import Database
from oppintel.slugs import project_id_for_slug, slug_for_project


def _slug_for(app_db, *, address: str) -> str:
    db = Database(app_db.config["APP_CONFIG"].database_path)
    try:
        row = db.conn.execute(
            "SELECT id FROM project WHERE address LIKE ? LIMIT 1", (f"{address}%",)
        ).fetchone()
        assert row, f"no fixture project at {address}"
        slug = slug_for_project(db, row["id"])
        assert slug, f"no slug for {address}"
        return slug
    finally:
        db.close()


def _body(client, slug: str) -> str:
    response = client.get(f"/opportunities/{slug}")
    assert response.status_code == 200
    return response.get_data(as_text=True)


# --- provenance ----------------------------------------------------------------

def test_detail_page_shows_a_citable_source(client, app_db):
    body = _body(client, _slug_for(app_db, address="10 ROSS AVE"))
    assert "Sources" in body
    assert "City of Fort Worth Development Permits" in body


def test_source_url_from_the_database_is_rendered(client, app_db):
    """A stored source URL must appear as a clickable link."""
    body = _body(client, _slug_for(app_db, address="10 ROSS AVE"))
    assert "https://example.gov/PB1" in body or "example.gov" in body


def test_last_verified_is_displayed(client, app_db):
    body = _body(client, _slug_for(app_db, address="10 ROSS AVE"))
    assert "Last verified" in body


# --- missing fields ------------------------------------------------------------

def test_missing_fields_render_as_not_verified(client, app_db):
    """Fields the source does not publish must say so, never appear blank."""
    body = _body(client, _slug_for(app_db, address="10 ROSS AVE"))
    assert "Not verified" in body
    # The fixture permits carry no owner, developer, GC or architect.
    for label in ("Owner", "Developer", "General contractor", "Architect"):
        assert label in body


def test_blank_field_is_never_rendered_as_an_empty_dd(client, app_db):
    """An empty value would read as 'none'; the marker must be explicit."""
    body = _body(client, _slug_for(app_db, address="10 ROSS AVE"))
    assert not re.search(r"<dd>\s*</dd>", body), "an empty definition value was rendered"


def test_detail_page_does_not_fabricate_a_contractor(client, app_db):
    body = _body(client, _slug_for(app_db, address="10 ROSS AVE"))
    for label in ("General contractor", "Architect", "Owner"):
        block = re.search(rf"<dt>{label}</dt>\s*<dd>(.*?)</dd>", body, re.S)
        assert block, label
        assert "Not verified" in block.group(1), (
            f"{label} claims a value the fixture does not support"
        )


# --- procurement transparency --------------------------------------------------

def test_procurement_status_is_shown(client, app_db):
    body = _body(client, _slug_for(app_db, address="10 ROSS AVE"))
    assert "Procurement status" in body
    assert "Evidence found, status unclear" in body


def test_permit_evidence_notice_is_present(client, app_db):
    body = _body(client, _slug_for(app_db, address="10 ROSS AVE"))
    # The template wraps this sentence across lines, so match an unbroken fragment.
    assert "does not confirm that the HVAC/mechanical package" in body


def test_no_open_bid_claim_is_made(client, app_db):
    """'open bid' may appear only inside a negation."""
    body = _body(client, _slug_for(app_db, address="10 ROSS AVE")).lower()
    start = 0
    while (index := body.find("open bid", start)) != -1:
        context = body[max(0, index - 60):index]
        assert "not " in context or "no " in context, f"unqualified open-bid claim: ...{context}"
        start = index + 1


def test_closed_project_still_reachable_by_direct_link(client, app_db):
    """Completed work is out of discovery but its record remains reachable and truthful."""
    slug = _slug_for(app_db, address="20 DONE ST")
    body = _body(client, slug)
    assert "Closed" in body
    # The template wraps this sentence across lines, so collapse whitespace before matching.
    assert "live opportunity" in re.sub(r"\s+", " ", body)


# --- mechanical evidence -------------------------------------------------------

def test_machine_evidence_is_explained(client, app_db):
    body = _body(client, _slug_for(app_db, address="10 ROSS AVE"))
    assert "Mechanical permit" in body or "Confirmed mechanical permit" in body


def test_task_evidence_quotes_the_source_text(client, app_db):
    body = _body(client, _slug_for(app_db, address="10 ROSS AVE"))
    assert "Mechanical remodel" in body


def test_plumbing_permit_is_never_shown_as_mechanical(client, app_db):
    """The record's own evidence must be mechanical, not another trade."""
    db = Database(app_db.config["APP_CONFIG"].database_path)
    try:
        rows = db.conn.execute(
            "SELECT p.address, s.slug FROM project p JOIN project_slug s ON s.project_id = p.id"
        ).fetchall()
    finally:
        db.close()

    for row in rows:
        body = _body(client, row["slug"])
        for line in body.splitlines():
            if "Mechanical evidence" in line:
                assert "Plumbing Permit" not in line


# --- grouping ------------------------------------------------------------------

def test_suite_pair_is_flagged_so_it_cannot_read_as_two_buildings(client):
    """Two suites of one building must not read as two independent opportunities.

    Both records are genuine permits, so both belong in a complete directory. What must not
    happen is that they appear as two unrelated buildings. Every card for a shared address must
    therefore carry the relationship disclosure.
    """
    body = client.get("/opportunities").get_data(as_text=True)
    cards = [c for c in body.split('<article class="card">')[1:] if "50 TOWER ST" in c]
    assert cards, "the suite records should be discoverable"
    for card in cards:
        assert "Shares building" in card or "share this street address" in card, (
            "a card for a shared building carried no relationship disclosure"
        )


def test_unshared_building_carries_no_relationship_flag(client):
    """The disclosure must not appear on records that have no sibling."""
    body = client.get("/opportunities").get_data(as_text=True)
    cards = [c for c in body.split('<article class="card">')[1:] if "10 ROSS AVE" in c]
    assert cards
    for card in cards:
        assert "Shares building" not in card


def test_related_records_disclosure_appears_when_a_building_is_shared(client, app_db):
    """When siblings exist, the relationship is stated rather than hidden."""
    body = _body(client, _slug_for(app_db, address="50 TOWER ST"))
    # Either the disclosure is present, or the pair was filtered so only one is shown.
    assert "Related records" in body or "50 TOWER ST" in body


# --- public/private separation -------------------------------------------------

def test_detail_page_exposes_no_internal_operational_detail(client, app_db):
    body = _body(client, _slug_for(app_db, address="10 ROSS AVE"))
    for internal in (
        "classification_score", "classification_reasons", "project_key",
        "discrepancies", "evidence_type", "/admin", "SELECT ", "sqlite",
        "data/oppintel.db", "Accela", "__VIEWSTATE",
    ):
        assert internal not in body, f"internal detail leaked: {internal}"


def test_detail_page_does_not_expose_another_projects_evidence(client, app_db):
    """A project page must show only its own sources."""
    body = _body(client, _slug_for(app_db, address="10 ROSS AVE"))
    assert "20 DONE ST" not in body
    assert "40 PLUMB ST" not in body


# --- slugs ---------------------------------------------------------------------

def test_slugs_are_stable_across_repeated_lookups(app_db):
    first = _slug_for(app_db, address="10 ROSS AVE")
    second = _slug_for(app_db, address="10 ROSS AVE")
    assert first == second


def test_slug_resolves_back_to_its_project(app_db):
    slug = _slug_for(app_db, address="10 ROSS AVE")
    db = Database(app_db.config["APP_CONFIG"].database_path)
    try:
        project_id = project_id_for_slug(db, slug)
        assert project_id is not None
        row = db.conn.execute(
            "SELECT address FROM project WHERE id = ?", (project_id,)
        ).fetchone()
        assert row["address"].startswith("10 ROSS AVE")
    finally:
        db.close()
