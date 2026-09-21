"""Dallas-specific false-positive regression tests.

A live Dallas run on 2026-09-21 exposed two defects that these tests lock down:

1. The Accela portal appends a disclaimer to every trade permit stating that the permit is
   *not* construction work. That disclaimer contains the word "Construction", so it was
   itself satisfying the construction-scope gate and promoting maintenance work to HIGH.
2. Descriptions contain HTML entities ("Remove &amp; replace"), which were being stored and
   displayed as literal entity text.

The three classification gates are deliberately not weakened here. These tests assert that
the gates reject work they should reject.
"""

from __future__ import annotations

from datetime import date

from oppintel.config import active_trade
from oppintel.models import Permit, clean_text
from oppintel.normalize import (
    commercial_candidate,
    detect_mechanical_signal,
    has_construction_scope,
    is_trade_service_work,
    strip_boilerplate,
)

#: The exact disclaimer the Dallas portal appends to trade permits.
DALLAS_TRADE_DISCLAIMER = (
    "**This permit authorizes work only for the approved trade. Construction, erection, or "
    "alteration of any structure will require a separate permit and may require additional "
    "trade permits.**"
)


def dallas_permit(**overrides) -> Permit:
    defaults = dict(
        source_id="dallas_accela_permits",
        permit_number="COM-MEC-26-002405",
        natural_key="COM-MEC-26-002405",
        permit_type="Commercial Mechanical Permit",
        permit_subtype="commercial_mechanical",
        permit_date=date(2026, 9, 16),
        status="Inspection Phase",
        address="12050 E NORTHWEST HWY",
        city="Dallas",
        state="TX",
        work_description=None,
        is_commercial=True,
    )
    defaults.update(overrides)
    return Permit(**defaults)


# --- the disclaimer bug -------------------------------------------------------

def test_disclaimer_is_stripped_from_scope_text():
    text = strip_boilerplate(f"install commercial RTU {DALLAS_TRADE_DISCLAIMER}")
    assert "authorizes work only" not in text
    assert "require a separate permit" not in text
    assert text.strip() == "install commercial RTU"


def test_disclaimer_does_not_create_construction_scope(trade):
    """Regression: the 'not construction work' disclaimer was satisfying the gate."""
    permit = dallas_permit(
        work_description=f"installing commercial RTU replacing existing unit {DALLAS_TRADE_DISCLAIMER}"
    )
    assert not has_construction_scope(permit, trade)
    assert not commercial_candidate(permit, trade)


# --- mechanical used as ordinary language -------------------------------------

def test_like_for_like_rtu_replacement_is_not_a_construction_project(trade):
    permit = dallas_permit(
        work_description=f"Remove & replace (5) RTU package units & (1) split system like for like. {DALLAS_TRADE_DISCLAIMER}"
    )
    assert is_trade_service_work(permit)
    assert not has_construction_scope(permit, trade)


def test_changing_out_rooftop_units_is_service_work(trade):
    permit = dallas_permit(
        work_description="Changing out TWO AAON Roof Top Units - LIKE FOR LIKE (1 - 25 ton, 1 - 30 ton)"
    )
    assert is_trade_service_work(permit)
    assert not has_construction_scope(permit, trade)


def test_mechanical_remodel_of_a_suite_is_construction(trade):
    """A genuine remodel is construction and must still qualify."""
    permit = dallas_permit(work_description="Mech remodel of spec suite")
    assert not is_trade_service_work(permit)
    assert has_construction_scope(permit, trade)


def test_roofing_adverb_still_does_not_imply_mechanical_scope(trade):
    permit = dallas_permit(
        permit_type="Commercial Roofing Permit",
        permit_subtype="commercial_roofing",
        work_description="Mechanically attach 4.5 inch Polyiso Rigid Insulation Boards to metal deck.",
    )
    # The permit type is roofing, so no Tier-1 signal; and "mechanically" is an adverb.
    signal = detect_mechanical_signal(permit, trade)
    assert signal is None or signal.tier != 2


# --- plumbing-only and electrical-only ----------------------------------------

def test_plumbing_only_permit_alone_is_not_a_project(trade):
    permit = dallas_permit(
        permit_type="Commercial Plumbing Permit",
        permit_subtype="commercial_plumbing",
        work_description=None,
    )
    assert not has_construction_scope(permit, trade)
    assert not commercial_candidate(permit, trade)


def test_electrical_only_permit_alone_is_not_a_project(trade):
    permit = dallas_permit(
        permit_type="Commercial Electrical Permit",
        permit_subtype="commercial_electrical",
        work_description=None,
    )
    assert not has_construction_scope(permit, trade)
    assert not commercial_candidate(permit, trade)


def test_plumbing_permit_with_real_construction_scope_does_qualify(trade):
    """The exclusion is about scope, not about the trade, so genuine work still counts."""
    permit = dallas_permit(
        permit_type="Commercial Plumbing Permit",
        permit_subtype="commercial_plumbing",
        work_description="Interior remodel including new plumbing rough-in for a restaurant build-out",
    )
    assert has_construction_scope(permit, trade)


# --- reroofing, repairs, maintenance ------------------------------------------

def test_reroofing_is_not_a_mechanical_opportunity(trade):
    permit = dallas_permit(
        permit_type="Commercial Roofing Permit",
        permit_subtype="commercial_roofing",
        work_description="Remove existing rubber roof and install 60 MIL TPO membrane roof system",
    )
    assert detect_mechanical_signal(permit, trade) is None


def test_foundation_repair_is_not_a_project(trade):
    permit = dallas_permit(
        permit_type="Commercial Foundation Repair",
        permit_subtype="commercial_foundation_repair",
        work_description="Foundation repair",
    )
    assert not has_construction_scope(permit, trade)


def test_preventive_maintenance_is_service_work(trade):
    permit = dallas_permit(
        work_description="Annual preventive maintenance on rooftop units",
    )
    assert is_trade_service_work(permit)


# --- residential ---------------------------------------------------------------

def test_residential_mechanical_permit_is_excluded(trade):
    permit = dallas_permit(
        permit_type="Residential Mechanical Permit",
        permit_subtype="residential_mechanical",
        work_description="Replace residential air handler",
        is_commercial=False,
    )
    assert not commercial_candidate(permit, trade)


# --- HTML entities -------------------------------------------------------------

def test_html_entities_are_decoded_in_source_text():
    """Regression: Dallas returns 'Remove &amp; replace' verbatim."""
    assert clean_text("Remove &amp; replace (5) RTU package units") == \
        "Remove & replace (5) RTU package units"


def test_entity_decoding_does_not_break_mechanical_detection(trade):
    """A decoded ampersand must not prevent Tier-2 scope detection.

    Uses a non-mechanical permit type so the assertion isolates Tier 2. On a mechanical
    permit type, Tier 1 correctly takes precedence over scope text.
    """
    permit = dallas_permit(
        permit_type="Commercial Alteration Addition Permit",
        permit_subtype="commercial_alteration_addition",
        work_description="Install HVAC &amp; exhaust systems for a new tenant build-out",
    )
    signal = detect_mechanical_signal(permit, trade)
    assert signal is not None and signal.tier == 2


def test_entities_are_decoded_in_the_stored_description(trade):
    from oppintel.connectors.dallas_accela_permits import DallasAccelaConnector
    from oppintel.config import CONFIG_DIR, load_sources
    from oppintel.models import RawPermit

    import yaml

    defaults = yaml.safe_load((CONFIG_DIR / "sources.yaml").read_text())["defaults"]
    connector = DallasAccelaConnector(load_sources()["dallas_accela_permits"], defaults)
    permit = connector.normalize(
        RawPermit(
            source_id="dallas_accela_permits",
            natural_key="COM-MEC-26-002373",
            payload={
                "record_date": "09/14/2026",
                "record_number": "COM-MEC-26-002373",
                "record_type": "Commercial Mechanical Permit",
                "address": "6166 RETAIL RD, Dallas TX 75230",
                "description": "Remove &amp; replace (5) RTU package units",
                "status": "Payment Due",
            },
        )
    )
    assert permit.work_description == "Remove & replace (5) RTU package units"


# --- the gates still hold ------------------------------------------------------

def test_dallas_mechanical_permit_still_yields_tier_1_evidence(trade):
    """The quality fixes must not suppress genuine Tier-1 mechanical evidence."""
    permit = dallas_permit(work_description="Mech remodel of spec suite")
    signal = detect_mechanical_signal(permit, trade)
    assert signal is not None
    assert signal.tier == 1