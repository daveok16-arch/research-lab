"""Tests for normalization and mechanical-evidence detection.

Several of these cover false positives found in a live Phase 1 run against Fort Worth data.
They exist so that a future keyword or mapping change cannot quietly reintroduce them.
"""

from __future__ import annotations

from datetime import date

from oppintel.models import (
    Permit,
    clean_text,
    normalize_address,
    parse_date,
    parse_money,
    parse_sqft,
)
from oppintel.normalize import (
    commercial_candidate,
    detect_mechanical_signal,
    has_construction_scope,
    is_excluded_residential,
    is_mechanical_permit,
)

from conftest import make_permit


# --- parsers -----------------------------------------------------------------

def test_parse_date_handles_the_real_source_formats():
    # Socrata ISO with fractional seconds.
    assert parse_date("2026-04-01T00:00:00.000") == date(2026, 4, 1)
    # Dallas two-digit year.
    assert parse_date("03/13/20") == date(2020, 3, 13)
    # Dallas GIS hyphenated.
    assert parse_date("12-29-2023") == date(2023, 12, 29)
    # ArcGIS epoch milliseconds.
    assert parse_date(1789689600000) == date(2026, 9, 18)


def test_parse_date_returns_none_rather_than_guessing():
    for bad in (None, "", "not a date", "NULL", "0000-00-00", "13/45/99"):
        assert parse_date(bad) is None, f"unexpected parse for {bad!r}"


def test_parse_money_treats_source_sentinels_as_absent():
    assert parse_money("1800") == 1800.0
    assert parse_money(150000) == 150000.0
    assert parse_money("$1,250.50") == 1250.5
    for absent in (None, "", "NULL", "N/A", "-", 0):
        assert parse_money(absent) in (None, 0.0) or parse_money(absent) == 0.0


def test_parse_money_rejects_stacked_values():
    """A pipe-delimited cell reports several permits, not one project value."""
    assert parse_money("100|200") is None


def test_parse_sqft_handles_text_and_units():
    assert parse_sqft("9236") == 9236.0
    assert parse_sqft("15,000 SF") == 15000.0
    assert parse_sqft(0) is None
    assert parse_sqft("NULL") is None


def test_clean_text_strips_source_sentinels():
    assert clean_text("  Fort Worth  ") == "Fort Worth"
    for sentinel in (None, "", "NULL", "n/a", "NA", "None"):
        assert clean_text(sentinel) is None


# --- address normalization ----------------------------------------------------

def test_address_normalization_collapses_variants_of_one_building():
    variants = [
        "12801 N CENTRAL EXPY Ste:1710",
        "12801 N Central Expressway",
        "12801 N. CENTRAL EXPY",
    ]
    keys = {normalize_address(v) for v in variants}
    assert len(keys) == 1, f"variants did not collapse: {keys}"


def test_address_normalization_keeps_distinct_buildings_apart():
    a = normalize_address("100 MAIN ST")
    b = normalize_address("200 MAIN ST")
    assert a != b


def test_address_normalization_returns_none_for_blanks():
    assert normalize_address(None) is None
    assert normalize_address("") is None
    assert normalize_address("   ") is None


# --- mechanical evidence ------------------------------------------------------

def test_mechanical_permit_type_is_tier_1(trade):
    permit = make_permit(permit_type="Mechanical", permit_subtype="Standalone")
    assert is_mechanical_permit(permit, trade)
    signal = detect_mechanical_signal(permit, trade)
    assert signal is not None and signal.tier == 1


def test_mechanical_scope_text_is_tier_2(trade):
    permit = make_permit(
        permit_type="Commercial Building Permit",
        work_description="Interior finish-out including HVAC, electrical and plumbing.",
    )
    signal = detect_mechanical_signal(permit, trade)
    assert signal is not None and signal.tier == 2


def test_roofing_adverb_does_not_register_as_mechanical_scope(trade):
    """Regression: live Fort Worth data matched 'mechanically' in a roof replacement."""
    permit = make_permit(
        permit_type="Commercial Building Permit",
        work_description=(
            "Roof recover-15,000 SF- prepare Existing roof for recover application, "
            "mechanically fasten 1/2 HD ISO coverboard, install TPO membrane."
        ),
    )
    assert detect_mechanical_signal(permit, trade) is None


def test_short_acronym_does_not_match_inside_a_word(trade):
    """'vav' must not match inside unrelated text."""
    permit = make_permit(work_description="Renovation of the pavavilion structure")
    assert detect_mechanical_signal(permit, trade) is None


def test_genuine_mep_scope_still_matches_after_word_boundary_fix(trade):
    permit = make_permit(
        work_description=(
            "Renovation to existing data center which includes replacing interior "
            "partition walls and associated mechanical, electrical, and plumbing work."
        ),
    )
    signal = detect_mechanical_signal(permit, trade)
    assert signal is not None and signal.tier == 2


def test_no_mechanical_signal_for_plain_building_work(trade):
    permit = make_permit(
        permit_type="Commercial Building Permit",
        work_description="New construction of a private amenity restaurant building",
        land_use="Assembly",
        specific_use="Restaurant",
    )
    assert detect_mechanical_signal(permit, trade) is None


# --- candidate filtering ------------------------------------------------------

def test_residential_rows_are_excluded(trade):
    permit = make_permit(
        permit_type="Plumbing (PL) Single Family  Alteration",
        land_use="SINGLE FAMILY DWELLING",
        is_commercial=None,
    )
    assert is_excluded_residential(permit, trade)
    assert not commercial_candidate(permit, trade)


def test_explicit_commercial_flag_overrides_keyword_heuristics(trade):
    """The source's own commercial flag is authoritative."""
    permit = make_permit(
        permit_type="Building (BU) Commercial Renovation",
        land_use="MULTI-FAMILY DWELLING",
        is_commercial=True,
    )
    assert not is_excluded_residential(permit, trade)


def test_standalone_mechanical_permit_has_no_construction_scope(trade):
    """A lone mechanical permit is a service call, not a construction project."""
    permit = make_permit(
        permit_type="Mechanical",
        permit_subtype="Standalone",
        work_description=None,
        land_use="General",
    )
    assert not has_construction_scope(permit, trade)
    assert not commercial_candidate(permit, trade)


def test_construction_subtype_supplies_scope_when_description_is_empty(trade):
    """A 'Commercial Building Permit / New' with no description is still a new building."""
    permit = make_permit(
        permit_type="Commercial Building Permit",
        permit_subtype="New",
        work_description=None,
    )
    assert has_construction_scope(permit, trade)
    assert commercial_candidate(permit, trade)


def test_permit_without_address_is_not_a_candidate(trade):
    permit = make_permit(address=None)
    assert not commercial_candidate(permit, trade)