"""Tests for customer-brief eligibility.

Requirement 11 defines exactly when an opportunity may be shown to a customer. These tests
fix that behaviour so a later change cannot quietly widen it — for example by letting a
completed building, a non-mechanical record, or a record with nothing to say about
significance into the brief.

Confirmed open bidding is deliberately NOT required, because no configured source can
establish it. That absence is asserted here too, so nobody "fixes" it by demanding evidence
that cannot exist.
"""

from __future__ import annotations

from oppintel.eligibility import (
    ELIGIBLE_CLASSIFICATIONS,
    SIGNIFICANCE_FIELDS,
    evaluate,
    inclusion_reason,
)


def row(**overrides):
    """A project row with every eligibility criterion satisfied."""
    base = {
        "id": 1,
        "classification": "HIGH",
        "classification_score": 95,
        "procurement_status": "Evidence found, status unclear",
        "mechanical_evidence_tier": 1,
        "mechanical_hvac_evidence": "Tier 1: Permit M1 is filed as 'Commercial Mechanical Permit'",
        "project_name": "Mechanical remodel of spec suite",
        "property_class": "Office",
        "estimated_project_value": None,
        "square_footage": None,
        "disputed_fields": "[]",
    }
    base.update(overrides)
    return base


# --- the happy path -----------------------------------------------------------

def test_an_active_mechanical_project_with_a_building_class_is_eligible():
    result = evaluate(row())
    assert result.eligible
    assert result.significance == ["building class 'Office'"]


def test_inclusion_reason_is_recorded_and_explainable():
    """Requirement 14: the reason an opportunity was included must be stored."""
    r = row()
    text = inclusion_reason(r, evaluate(r))
    assert "Tier-1 mechanical permit" in text
    assert "Office" in text
    assert "HIGH" in text
    assert "Evidence found" in text


def test_inclusion_reason_marks_tier_two_differently():
    r = row(mechanical_evidence_tier=2)
    assert "Tier-2" in inclusion_reason(r, evaluate(r))


# --- completed work -----------------------------------------------------------

def test_completed_project_is_not_eligible():
    result = evaluate(row(procurement_status="Closed"))
    assert not result.eligible
    assert any("finished" in e for e in result.exclusions)


def test_unverified_procurement_is_not_eligible():
    result = evaluate(row(procurement_status="Not verified"))
    assert not result.eligible


def test_evidence_found_is_eligible_without_being_an_open_bid():
    """The whole point: active work qualifies, and confirmed bidding is not required."""
    result = evaluate(row(procurement_status="Evidence found, status unclear"))
    assert result.eligible


def test_confirmed_open_is_not_required():
    """No source can establish it, so requiring it would empty the brief."""
    result = evaluate(row(procurement_status="Evidence found, status unclear"))
    assert result.eligible


# --- mechanical evidence ------------------------------------------------------

def test_record_without_mechanical_evidence_is_not_eligible():
    result = evaluate(row(mechanical_evidence_tier=None, mechanical_hvac_evidence=None))
    assert not result.eligible
    assert any("mechanical" in e.lower() for e in result.exclusions)


def test_tier_three_implication_is_not_sufficient():
    """Trade implication alone is not mechanical evidence."""
    result = evaluate(row(mechanical_evidence_tier=3))
    assert not result.eligible


# --- classification gates -----------------------------------------------------

def test_needs_verification_is_not_eligible():
    result = evaluate(row(classification="NEEDS_VERIFICATION"))
    assert not result.eligible


def test_only_high_and_medium_are_eligible():
    assert set(ELIGIBLE_CLASSIFICATIONS) == {"HIGH", "MEDIUM"}


def test_medium_with_context_is_eligible():
    """MEDIUM is a legitimate customer opportunity when significance is established."""
    result = evaluate(row(classification="MEDIUM", property_class="Retail"))
    assert result.eligible


# --- significance -------------------------------------------------------------

def test_no_significance_fact_is_not_eligible():
    """Nothing to explain why it matters means nothing to put in front of a customer."""
    result = evaluate(row(property_class=None))
    assert not result.eligible
    assert any("why the project matters" in e for e in result.exclusions)


def test_unspecified_commercial_class_is_not_significance():
    result = evaluate(row(property_class="Commercial (unspecified)"))
    assert not result.eligible


def test_declared_value_is_significance():
    result = evaluate(row(property_class=None, estimated_project_value=5_000_000.0))
    assert result.eligible
    assert any("declared value" in s for s in result.significance)


def test_declared_area_is_significance():
    result = evaluate(row(property_class=None, square_footage=40_000.0))
    assert result.eligible
    assert any("declared area" in s for s in result.significance)


def test_significance_fields_are_facts_not_scores():
    assert set(SIGNIFICANCE_FIELDS) == {
        "property_class", "estimated_project_value", "square_footage",
    }


# --- service work -------------------------------------------------------------

def test_like_for_like_replacement_is_not_eligible():
    result = evaluate(
        row(project_name="Remove & replace (5) RTU package units like for like")
    )
    assert not result.eligible
    assert any("service" in e.lower() for e in result.exclusions)


def test_preventive_maintenance_is_not_eligible():
    result = evaluate(row(project_name="Annual preventive maintenance on rooftop units"))
    assert not result.eligible


def test_service_check_runs_even_though_the_assembler_already_excludes_it():
    """A defence in depth: a future upstream change cannot leak maintenance work."""
    result = evaluate(row(mechanical_hvac_evidence="like for like replacement of existing unit"))
    assert not result.eligible


# --- explanation --------------------------------------------------------------

def test_eligible_explanation_states_the_significance():
    text = evaluate(row()).explain()
    assert "Eligible" in text
    assert "Office" in text


def test_ineligible_explanation_states_the_reason():
    text = evaluate(row(classification="NEEDS_VERIFICATION")).explain()
    assert "Not eligible" in text
    assert "NEEDS_VERIFICATION" in text


def test_eligibility_is_deterministic():
    r = row()
    assert evaluate(r).eligible == evaluate(r).eligible
    assert evaluate(r).significance == evaluate(r).significance