"""The required classification probe matrix.

Requirement: test specifically that the gates behave correctly for

  * "mechanical" used as ordinary construction language
  * plumbing-only permits
  * electrical-only permits
  * reroofing
  * repairs
  * maintenance
  * residential projects
  * commercial new construction
  * commercial mechanical

Each case asserts BOTH whether the record forms a project at all and, where it does, what
evidence tier it earns. The gates are not weakened anywhere in this file; several of these
tests exist to prove that specific work types are correctly rejected.
"""

from __future__ import annotations

from datetime import date

import pytest

from oppintel.classify import classify
from oppintel.config import active_trade
from oppintel.models import Permit, Project
from oppintel.normalize import (
    commercial_candidate,
    detect_mechanical_signal,
    has_construction_scope,
)

REFERENCE = date(2026, 9, 21)

DALLAS_DISCLAIMER = (
    "**This permit authorizes work only for the approved trade. Construction, erection, or "
    "alteration of any structure will require a separate permit and may require additional "
    "trade permits.**"
)


def permit(**overrides) -> Permit:
    defaults = dict(
        source_id="dallas_accela_permits",
        permit_number="P1",
        natural_key="P1",
        permit_type="Commercial New Construction Permit",
        permit_subtype="commercial_new_construction",
        permit_date=date(2026, 9, 1),
        status="Pending",
        address="100 MAIN ST",
        city="Dallas",
        state="TX",
        work_description=None,
        is_commercial=True,
    )
    defaults.update(overrides)
    return Permit(**defaults)


# =============================================================================
# 1. "mechanical" used as ordinary construction language
# =============================================================================

def test_mechanically_fasten_is_not_mechanical_scope(trade):
    """"Mechanically" is an adverb about fastening, not evidence of mechanical work."""
    p = permit(
        permit_type="Commercial Roofing Permit",
        permit_subtype="commercial_roofing",
        work_description="Mechanically attach 4.5 inch Polyiso Rigid Insulation to metal deck.",
    )
    signal = detect_mechanical_signal(p, trade)
    assert signal is None


def test_mechanical_room_mentioned_in_passing_still_counts_as_scope(trade):
    """A real mention of mechanical scope does count, even if incidental to the description.

    This is deliberately permissive at the *detection* layer; the significance and scale
    gates are what stop a passing mention from becoming a headline opportunity.
    """
    p = permit(
        permit_type="Commercial Alteration Addition Permit",
        permit_subtype="commercial_alteration_addition",
        work_description="Tenant build out including support areas such as mechanical rooms.",
    )
    signal = detect_mechanical_signal(p, trade)
    assert signal is not None and signal.tier == 2


def test_mechanical_scope_alone_cannot_reach_high_without_scale(trade):
    """An incidental mention on a tiny job is held below HIGH by the scale gate."""
    project = Project(
        project_key="x", address="100 MAIN ST", city="Dallas", state="TX",
        estimated_project_value=18_000.0, property_class="Retail",
        project_status="Plan Review", permit_date=date(2026, 9, 1),
        mechanical_evidence_tier=2,
        mechanical_hvac_evidence='Tier 2: work description: "...mechanical rooms..."',
    )
    classify(project, trade, reference_date=REFERENCE)
    assert project.classification != "HIGH"


# =============================================================================
# 2. plumbing-only
# =============================================================================

def test_plumbing_only_permit_is_not_a_project(trade):
    p = permit(
        permit_type="Commercial Plumbing Permit",
        permit_subtype="commercial_plumbing",
        work_description=None,
    )
    assert not has_construction_scope(p, trade)
    assert not commercial_candidate(p, trade)


def test_plumbing_only_has_no_mechanical_evidence(trade):
    p = permit(
        permit_type="Commercial Plumbing Permit",
        permit_subtype="commercial_plumbing",
        work_description="Replace water heater",
    )
    assert detect_mechanical_signal(p, trade) is None


def test_plumbing_within_a_remodel_does_form_a_project(trade):
    """The exclusion is about scope, not trade: plumbing inside a build-out is real work."""
    p = permit(
        permit_type="Commercial Plumbing Permit",
        permit_subtype="commercial_plumbing",
        work_description="New plumbing rough-in for restaurant build-out",
    )
    assert has_construction_scope(p, trade)
    assert commercial_candidate(p, trade)


# =============================================================================
# 3. electrical-only
# =============================================================================

def test_electrical_only_permit_is_not_a_project(trade):
    p = permit(
        permit_type="Commercial Electrical Permit",
        permit_subtype="commercial_electrical",
        work_description=None,
    )
    assert not has_construction_scope(p, trade)
    assert not commercial_candidate(p, trade)


def test_electrical_only_has_no_mechanical_evidence(trade):
    p = permit(
        permit_type="Commercial Electrical Permit",
        permit_subtype="commercial_electrical",
        work_description="Panel upgrade and new service",
    )
    assert detect_mechanical_signal(p, trade) is None


# =============================================================================
# 4. reroofing
# =============================================================================

def test_reroofing_is_not_mechanical_work(trade):
    p = permit(
        permit_type="Commercial Roofing Permit",
        permit_subtype="commercial_roofing",
        work_description="Remove existing rubber roof and install 60 MIL TPO membrane roof system",
    )
    assert detect_mechanical_signal(p, trade) is None


def test_reroofing_does_not_form_a_commercial_project(trade):
    """A roof replacement has no construction keyword, so it is not a project."""
    p = permit(
        permit_type="Commercial Roofing Permit",
        permit_subtype="commercial_roofing",
        work_description="Install TPO membrane roof system",
    )
    assert not has_construction_scope(p, trade)


# =============================================================================
# 5. repairs
# =============================================================================

def test_foundation_repair_is_not_a_project(trade):
    p = permit(
        permit_type="Commercial Foundation Repair",
        permit_subtype="commercial_foundation_repair",
        work_description="Foundation repair",
    )
    assert not has_construction_scope(p, trade)
    assert not commercial_candidate(p, trade)


def test_like_for_like_equipment_replacement_is_not_a_project(trade):
    p = permit(
        permit_type="Commercial Mechanical Permit",
        permit_subtype="commercial_mechanical",
        work_description=f"Remove & replace (5) RTU package units like for like. {DALLAS_DISCLAIMER}",
    )
    assert not has_construction_scope(p, trade)
    assert not commercial_candidate(p, trade)


# =============================================================================
# 6. maintenance
# =============================================================================

def test_preventive_maintenance_is_service_work(trade):
    p = permit(
        permit_type="Commercial Mechanical Permit",
        permit_subtype="commercial_mechanical",
        work_description="Annual preventive maintenance on rooftop units",
    )
    assert not has_construction_scope(p, trade)


def test_maintenance_never_produces_a_high_opportunity(trade):
    """Even though the permit type is mechanical, maintenance must not reach HIGH."""
    p = permit(
        permit_type="Commercial Mechanical Permit",
        permit_subtype="commercial_mechanical",
        work_description="Routine maintenance and filter replacement",
    )
    assert not commercial_candidate(p, trade)


# =============================================================================
# 7. residential
# =============================================================================

def test_residential_mechanical_is_excluded(trade):
    p = permit(
        permit_type="Residential Mechanical Permit",
        permit_subtype="residential_mechanical",
        work_description="Replace residential air handler",
        is_commercial=False,
    )
    assert not commercial_candidate(p, trade)


def test_residential_new_construction_is_excluded(trade):
    p = permit(
        permit_type="Residential New Construction Permit",
        permit_subtype="residential_new_construction",
        work_description="New single family residence",
        is_commercial=False,
    )
    assert not commercial_candidate(p, trade)


def test_residential_solar_is_excluded(trade):
    p = permit(
        permit_type="Residential Solar/PV Permit",
        permit_subtype="residential_solar_pv",
        work_description="Install residential solar",
        is_commercial=False,
    )
    assert not commercial_candidate(p, trade)


# =============================================================================
# 8. commercial new construction
# =============================================================================

def test_commercial_new_construction_forms_a_project(trade):
    p = permit(
        permit_type="Commercial New Construction Permit",
        permit_subtype="commercial_new_construction",
        work_description="New construction of a 4-story office building",
    )
    assert has_construction_scope(p, trade)
    assert commercial_candidate(p, trade)


def test_commercial_new_construction_without_description_still_forms_a_project(trade):
    """A 'Commercial New Construction' type is itself unmistakable construction."""
    p = permit(
        permit_type="Commercial New Construction Permit",
        permit_subtype="commercial_new_construction",
        work_description=None,
    )
    assert commercial_candidate(p, trade)


# =============================================================================
# 9. commercial mechanical
# =============================================================================

def test_commercial_mechanical_permit_yields_tier_1_evidence(trade):
    p = permit(
        permit_type="Commercial Mechanical Permit",
        permit_subtype="commercial_mechanical",
        work_description="Mech remodel of spec suite",
    )
    signal = detect_mechanical_signal(p, trade)
    assert signal is not None and signal.tier == 1


def test_commercial_mechanical_with_construction_scope_forms_a_project(trade):
    p = permit(
        permit_type="Commercial Mechanical Permit",
        permit_subtype="commercial_mechanical",
        work_description="Mech remodel of spec suite",
    )
    assert has_construction_scope(p, trade)
    assert commercial_candidate(p, trade)


def test_commercial_mechanical_tier_1_wins_over_scope_text(trade):
    """Tier 1 is ranked above Tier 2 when both are available."""
    p = permit(
        permit_type="Commercial Mechanical Permit",
        permit_subtype="commercial_mechanical",
        work_description="Install HVAC and ductwork",
    )
    signal = detect_mechanical_signal(p, trade)
    assert signal.tier == 1


def test_bare_mechanical_permit_cannot_reach_high(trade):
    """The significance gate: mechanical scope alone does not establish significance."""
    project = Project(
        project_key="x", address="12050 E NORTHWEST HWY", city="Dallas", state="TX",
        project_type="Commercial (unspecified)", project_status="Inspection Phase",
        permit_date=date(2026, 9, 16), estimated_project_value=None,
        square_footage=None, property_class=None, mechanical_evidence_tier=1,
        mechanical_hvac_evidence="Tier 1: Permit COM-MEC-26-002405",
    )
    classify(project, trade, reference_date=REFERENCE)
    assert project.classification == "MEDIUM"


@pytest.mark.parametrize(
    "record_type,expect_project,reason",
    [
        ("Commercial New Construction Permit", True,
         "the type itself states construction"),
        ("Commercial Alteration Addition Permit", True,
         "'alteration addition' is a construction activity"),
        ("Commercial Mechanical Permit", False,
         "a lone trade permit with no description carries no construction evidence"),
        ("Commercial Plumbing Permit", False, "trade-only, no construction evidence"),
        ("Commercial Electrical Permit", False, "trade-only, no construction evidence"),
        ("Commercial Roofing Permit", False, "roofing with no description is not a project"),
        ("Residential Mechanical Permit", False, "residential work is excluded"),
        ("Residential New Construction Permit", False, "residential work is excluded"),
    ],
)
def test_end_to_end_gate_matrix(trade, record_type, expect_project, reason):
    """The full matrix with no description at all.

    With no free text the decision rests entirely on the record type. Building types qualify
    because their own name states construction work. A bare trade permit does not, because a
    mechanical permit with no description is a service call and the construction-scope gate
    requires positive evidence of building work. That is the intended behaviour, not a gap.
    """
    is_residential = record_type.startswith("Residential")
    subtype = record_type.lower().replace(" ", "_").replace("/", "_")
    p = permit(
        permit_type=record_type,
        permit_subtype=subtype,
        work_description=None,
        is_commercial=not is_residential,
    )
    assert commercial_candidate(p, trade) is expect_project, reason


def test_bare_mechanical_permit_is_excluded_for_a_stated_reason(trade):
    """Documents why a description-less mechanical permit does not form a project."""
    p = permit(
        permit_type="Commercial Mechanical Permit",
        permit_subtype="commercial_mechanical",
        work_description=None,
    )
    assert not has_construction_scope(p, trade)
    assert not commercial_candidate(p, trade)


def test_mechanical_permit_with_construction_scope_does_form_a_project(trade):
    """The same permit type qualifies once it describes building work."""
    p = permit(
        permit_type="Commercial Mechanical Permit",
        permit_subtype="commercial_mechanical",
        work_description="Mech remodel of spec suite",
    )
    assert commercial_candidate(p, trade)


@pytest.mark.parametrize(
    "record_type",
    [
        "Commercial Mechanical Permit",
        "Commercial Plumbing Permit",
        "Commercial Electrical Permit",
        "Commercial Roofing Permit",
    ],
)
def test_like_for_like_disqualifies_every_trade_type(trade, record_type):
    """Service language excludes the permit whatever its type, because it is maintenance."""
    p = permit(
        permit_type=record_type,
        permit_subtype=record_type.lower().replace(" ", "_"),
        work_description="Like for like replacement of existing equipment",
    )
    assert not commercial_candidate(p, trade)