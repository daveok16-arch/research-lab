"""Tests for procurement status.

The rule under test: an opportunity is never described as an open bid unless a source says
so. No source in this market publishes bid status, so the expected outcome is that
``Confirmed open`` is effectively unreachable — and that is correct, not a gap to work
around.

The second rule: a finished or withdrawn project is not an opportunity. Permits for work
that is already installed must not appear in front of a contractor as live.
"""

from __future__ import annotations

import pytest

from oppintel.models import Project
from oppintel.procurement import (
    ACTIVE_PROCUREMENT_STATUS_PHRASES,
    CONFIRMED_OPEN,
    EVIDENCE_FOUND,
    NOT_VERIFIED,
    PROCUREMENT_STATES,
    is_claimable,
    procurement_explanation,
    procurement_status,
)

#: Real statuses observed from the live Dallas Accela and Fort Worth feeds.
OBSERVED_ACTIVE = (
    "Inspection Phase",
    "Pending",
    "Plan Review",
    "In Review",
    "Issued",
    "Document Received",
    "Additional Info Required",
    "Revisions Required",
    "Payment Due",
    "Incomplete Submittal",
    "Ready to Issue",
)

OBSERVED_CLOSED = (
    "Closed - Complete",
    "Closed - Denied",
    "Closed - Withdrawn",
    "Final CO Issued",
    "TCO Issued",
    "Finaled",
    "Expired",
    "Void",
)


def project(status=None, **overrides) -> Project:
    defaults = dict(project_key="x", project_status=status)
    defaults.update(overrides)
    return Project(**defaults)


# --- confirmed open is unreachable without evidence ----------------------------

def test_no_configured_source_can_claim_confirmed_open():
    """Nothing in this market publishes bid status."""
    for status in OBSERVED_ACTIVE:
        assert procurement_status(project(status)) != CONFIRMED_OPEN


def test_confirmed_open_requires_explicit_bid_language():
    p = project("Inspection Phase")
    assert procurement_status(p, raw_text="Plans available, invitation to bid issued") == CONFIRMED_OPEN


def test_ordinary_permit_text_never_reads_as_a_bid():
    p = project(
        "Issued",
        mechanical_hvac_evidence="Tier 1: Permit COM-MEC-26-001850 is filed as 'Commercial Mechanical Permit'",
    )
    assert procurement_status(p) == EVIDENCE_FOUND


def test_pre_bid_mention_is_recognised():
    p = project("Plan Review")
    assert procurement_status(p, raw_text="pre-bid conference scheduled") == CONFIRMED_OPEN


# --- active work ---------------------------------------------------------------

@pytest.mark.parametrize("status", OBSERVED_ACTIVE)
def test_active_statuses_give_evidence_found(status):
    assert procurement_status(project(status)) == EVIDENCE_FOUND


def test_active_status_can_never_be_confirmed_open():
    for status in OBSERVED_ACTIVE:
        assert procurement_status(project(status)) != CONFIRMED_OPEN


# --- completed and dead work ---------------------------------------------------

@pytest.mark.parametrize("status", OBSERVED_CLOSED)
def test_closed_statuses_are_not_verified(status):
    """Finished or withdrawn work supports no procurement claim at all."""
    assert procurement_status(project(status)) == NOT_VERIFIED


def test_final_co_issued_is_not_active():
    """Regression: 'Final CO Issued' contains 'issued' and was read as active work."""
    assert procurement_status(project("Final CO Issued")) == NOT_VERIFIED


def test_tco_issued_is_not_active():
    assert procurement_status(project("TCO Issued")) == NOT_VERIFIED


def test_closed_complete_is_not_claimable():
    assert not is_claimable(project("Closed - Complete"))


def test_active_project_is_claimable():
    assert is_claimable(project("Inspection Phase"))


def test_missing_status_is_not_verified():
    assert procurement_status(project(None)) == NOT_VERIFIED
    assert not is_claimable(project(None))


# --- consistency with the classifier -------------------------------------------

def test_active_phrases_cover_the_classifier_active_keywords():
    """A status the classifier credits as active must not read as 'no evidence of activity'.

    The two layers describe the same underlying fact, so a divergence would let the report
    contradict the classification.
    """
    from oppintel.config import active_trade

    trade = active_trade()
    for keyword in ("inspection phase", "plan review", "pending", "issued"):
        assert keyword in trade.active_status_keywords
        assert any(keyword in phrase for phrase in ACTIVE_PROCUREMENT_STATUS_PHRASES), (
            f"{keyword!r} is active for the classifier but not for procurement"
        )


# --- the three permitted states ------------------------------------------------

def test_only_the_three_permitted_states_are_returned():
    for status in OBSERVED_ACTIVE + OBSERVED_CLOSED + (None,):
        assert procurement_status(project(status)) in PROCUREMENT_STATES


def test_identical_wording_between_brief_and_pipeline():
    """The report reuses the helper, so both explain a status the same way."""
    p = project("Inspection Phase")
    explanation = procurement_explanation(EVIDENCE_FOUND, p)
    assert "not a confirmed open bid" in explanation


def test_explanation_for_confirmed_open_urges_confirmation():
    text = procurement_explanation(CONFIRMED_OPEN, project("Issued"))
    assert "confirm" in text.lower()


def test_explanation_for_unverified_states_the_limitation():
    text = procurement_explanation(NOT_VERIFIED, project("Closed - Complete"))
    assert "unverified" in text.lower()