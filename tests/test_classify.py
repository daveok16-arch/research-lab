"""Tests for opportunity classification.

The gates in this module are what stop the platform from over-claiming. Each is tested
both in the direction where it should fire and where it should not, so that a change to the
weights cannot silently disable a gate.
"""

from __future__ import annotations

from datetime import date

import pytest

from oppintel.classify import classify
from oppintel.constants import HIGH, MEDIUM, NEEDS_VERIFICATION
from oppintel.models import Project

REFERENCE = date(2026, 9, 20)


def build_project(**overrides) -> Project:
    defaults = dict(
        project_key="test",
        address="100 MAIN ST",
        city="Fort Worth",
        state="TX",
        project_type="Healthcare",
        project_status="Issued",
        permit_date=date(2026, 9, 1),
    )
    defaults.update(overrides)
    return Project(**defaults)


def test_healthcare_with_mechanical_permit_is_high(trade):
    """The worked example from the design document."""
    project = build_project(
        estimated_project_value=12_000_000.0,
        square_footage=90_000.0,
        mechanical_evidence_tier=1,
        mechanical_hvac_evidence="Tier 1: Permit PM26-09104 is filed as 'Mechanical'",
        property_class="Healthcare",
    )
    classify(project, trade, reference_date=REFERENCE)
    assert project.classification == HIGH
    assert project.classification_score >= 70
    assert any("Mechanical permit on file" in r for r in project.classification_reasons)


def test_large_warehouse_without_mechanical_evidence_cannot_be_high(trade):
    """The mechanical-evidence gate. This is the most important rule in the platform."""
    project = build_project(
        estimated_project_value=25_000_000.0,
        square_footage=300_000.0,
        property_class="Industrial",
        project_status="Issued",
        mechanical_evidence_tier=None,
    )
    classify(project, trade, reference_date=REFERENCE)
    assert project.classification != HIGH
    assert project.classification == MEDIUM
    assert any("Held below HIGH" in r for r in project.classification_reasons)


def test_small_roofing_job_with_incidental_hvac_mention_is_not_high(trade):
    """Regression: live data produced an $18,000 roof replacement labelled HIGH."""
    project = build_project(
        estimated_project_value=18_000.0,
        square_footage=None,
        property_class="Retail",
        mechanical_evidence_tier=2,
        mechanical_hvac_evidence='Tier 2: work description: "...HVAC curbs..."',
    )
    classify(project, trade, reference_date=REFERENCE)
    assert project.classification != HIGH
    assert any("Held below HIGH" in r for r in project.classification_reasons)


def test_small_but_explicit_mechanical_permit_with_context_can_be_high(trade):
    """A Tier-1 mechanical permit is direct evidence and is exempt from the *scale* floor.

    The significance gate is separate from the scale gate: a project still needs a building
    class, a declared value, or a footprint before HIGH is warranted.
    """
    project = build_project(
        estimated_project_value=300_000.0,
        square_footage=None,
        property_class=None,
        project_status="Issued",
        mechanical_evidence_tier=1,
        mechanical_hvac_evidence="Tier 1: Permit PM26-09014 is filed as 'Mechanical'",
    )
    classify(project, trade, reference_date=REFERENCE)
    assert project.classification == HIGH


def test_bare_mechanical_permit_with_no_context_is_not_high(trade):
    """HIGH asserts significance, not merely that mechanical work exists.

    A standalone Dallas trade permit confirms mechanical scope but carries no building class,
    declared value, or footprint. Earlier live runs labelled 27 of these HIGH, which
    overstated what the evidence supports; they are now held at MEDIUM with a stated reason.
    """
    project = build_project(
        estimated_project_value=None,
        square_footage=None,
        property_class=None,
        project_type="Commercial (unspecified)",
        project_status="Inspection Phase",
        mechanical_evidence_tier=1,
        mechanical_hvac_evidence="Tier 1: Permit COM-MEC-26-002396 is filed as 'Commercial Mechanical Permit'",
    )
    classify(project, trade, reference_date=REFERENCE)
    assert project.classification == MEDIUM
    assert any("significance is not established" in r for r in project.classification_reasons)


def test_data_center_with_mechanical_scope_is_high(trade):
    project = build_project(
        estimated_project_value=2_000_000.0,
        project_type="Data Center",
        property_class="Data Center",
        project_status="Pending",
        mechanical_evidence_tier=2,
        mechanical_hvac_evidence='Tier 2: work description: "...associated mechanical..."',
    )
    classify(project, trade, reference_date=REFERENCE)
    assert project.classification == HIGH


def test_inactive_status_earns_no_phase_points(trade):
    active = build_project(property_class="Office", project_status="Issued")
    inactive = build_project(property_class="Office", project_status="Finaled")
    classify(active, trade, reference_date=REFERENCE)
    classify(inactive, trade, reference_date=REFERENCE)
    assert active.classification_score > inactive.classification_score
    assert not any("active work" in r for r in inactive.classification_reasons)


def test_status_with_both_active_and_inactive_keywords_is_inactive(trade):
    """'Closed' must win over 'Approved' in a compound status such as 'Closed By Rule'."""
    project = build_project(property_class="Office", project_status="Closed By Rule")
    classify(project, trade, reference_date=REFERENCE)
    assert not any("active work" in r for r in project.classification_reasons)


def test_recent_permit_earns_recency_points(trade):
    recent = build_project(property_class="Office", permit_date=date(2026, 9, 1))
    old = build_project(property_class="Office", permit_date=date(2023, 1, 1))
    classify(recent, trade, reference_date=REFERENCE)
    classify(old, trade, reference_date=REFERENCE)
    assert any("within the last" in r for r in recent.classification_reasons)
    assert not any("within the last" in r for r in old.classification_reasons)


def test_every_classification_records_its_reasons(trade):
    """A label with no reasons is a black box; the classifier must always explain itself."""
    for tier, value in ((None, 50_000.0), (2, 2_000_000.0), (1, None)):
        project = build_project(
            estimated_project_value=value,
            property_class="Office",
            mechanical_evidence_tier=tier,
        )
        classify(project, trade, reference_date=REFERENCE)
        assert project.classification in (HIGH, MEDIUM, NEEDS_VERIFICATION)
        assert project.classification_reasons, f"no reasons for tier={tier}"
        assert project.classification_score is not None


def test_address_without_street_number_cannot_be_high_or_medium(trade):
    """An opportunity nobody can locate is not yet actionable."""
    project = build_project(
        address="BLUE RIDGE TRL",
        estimated_project_value=3_665_421.0,
        property_class="Office",
        mechanical_evidence_tier=1,
        mechanical_hvac_evidence="Tier 1: Permit PM1 is filed as 'Mechanical'",
    )
    classify(project, trade, reference_date=REFERENCE)
    assert project.classification == NEEDS_VERIFICATION
    assert project.location_precision == "approximate"
    assert any("no street number" in r for r in project.classification_reasons)


def test_address_with_street_number_is_precise(trade):
    project = build_project(address="100 MAIN ST", property_class="Office")
    classify(project, trade, reference_date=REFERENCE)
    assert project.location_precision == "precise"


def test_thin_record_is_needs_verification(trade):
    project = build_project(
        estimated_project_value=None,
        square_footage=None,
        property_class=None,
        project_type=None,
        project_status=None,
        permit_date=None,
    )
    classify(project, trade, reference_date=REFERENCE)
    assert project.classification == NEEDS_VERIFICATION


def test_missing_mechanical_evidence_is_always_stated(trade):
    """The report must never let a reader assume mechanical scope was confirmed."""
    project = build_project(property_class="Office", mechanical_evidence_tier=None)
    classify(project, trade, reference_date=REFERENCE)
    assert any("No mechanical/HVAC evidence" in r for r in project.classification_reasons)


def test_hmo_class_property_classes_are_recognised(trade):
    """The customer's priority classes must map to real classes, not fall through."""
    for text, expected in (
        ("AMBULATORY SURGICAL CENTER", "Healthcare"),
        ("Data Center", "Data Center"),
        ("MANUFACTURING PLANT", "Manufacturing"),
        ("WAREHOUSE DISTRIBUTION", "Industrial"),
        ("OFFICE BUILDING", "Office"),
        ("SHOPPING CENTER RETAIL", "Retail"),
        ("HOTEL CONFERENCE CENTER", "Hospitality"),
        ("PLANO ISD ELEMENTARY SCHOOL", "Education"),
        ("CITY OF FORT WORTH LIBRARY", "Public / Institutional"),
    ):
        klass = trade.property_class_for(text)
        assert klass is not None, f"no class for {text!r}"
        assert klass["label"] == expected, f"{text!r} -> {klass['label']}"