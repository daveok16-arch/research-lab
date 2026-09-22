"""Procurement status.

Whether a project is currently out to bid is a claim about the *market*, not about the
permit record. No source in this market publishes bid status, so the platform cannot know
it, and must not imply it.

The three permitted values are deliberately narrow:

* ``CONFIRMED_OPEN``  — a source explicitly states the work is open for bid or award.
* ``EVIDENCE_FOUND``  — a source shows the project is active and moving, but says nothing
                        about how work will be procured. This is the honest label for
                        "permit issued, work under way": there may be an opportunity, but
                        no bid status is on record.
* ``NOT_VERIFIED``    — nothing supports any procurement statement.

A project is never described as an "open bid" unless a source says so, which today means
never. That is the correct outcome rather than a limitation to be worked around.
"""

from __future__ import annotations

import re

from .models import Project

CONFIRMED_OPEN = "Confirmed open"
EVIDENCE_FOUND = "Evidence found, status unclear"
NOT_VERIFIED = "Not verified"

PROCUREMENT_STATES = (CONFIRMED_OPEN, EVIDENCE_FOUND, NOT_VERIFIED)

#: Phrases a source would have to contain to justify a confirmed-open claim. None of the
#: currently configured sources publish these fields, so this list is intentionally thin and
#: exists so that a future source which does publish bid status is handled rather than
#: ignored.
BID_EVIDENCE_PHRASES = (
    "invitation to bid",
    "invitation for bid",
    "request for proposal",
    "request for quotation",
    "advertisement for bids",
    "bid opening",
    "bids due",
    "sealed bid",
    "pre-bid",
    "out for bid",
    "now bidding",
    "bid package",
)

#: Statuses indicating the project is proceeding, which supports "evidence found" but never
#: "confirmed open". These deliberately mirror the classifier's `active_status_keywords` so
#: that the two layers cannot contradict each other: a record the classifier credits as
#: active work must not simultaneously be reported as having no evidence of activity.
ACTIVE_PROCUREMENT_STATUS_PHRASES = (
    "issued",
    "inspection phase",
    "plan review",
    "in review",
    "under construction",
    "approved",
    "pending",
    "ready to issue",
    "incomplete submittal",
    "document received",
    "additional info required",
    "revisions required",
    "payment due",
    "application about to expire",
    "partial approval",
    "approved with conditions",
    "awaiting client reply",
)

#: Statuses indicating the work is finished, dead, or withdrawn. These cannot support even
#: "evidence found", because there is nothing left to procure.
#:
#: The completion phrases matter more than they look. A "Final CO Issued" record means the
#: building passed its certificate-of-occupancy inspection, so the mechanical work is
#: already installed and closed. Treating that as active work would put finished buildings
#: in front of a contractor as live opportunities, which is the fastest way to lose their
#: trust in the list. "issued" alone is an active signal, so the completion forms are tested
#: first and win.
CLOSED_PROCUREMENT_STATUS_PHRASES = (
    "closed",
    "finaled",
    "complete",
    "completed",
    "final co",
    "final c/o",
    "final certificate",
    "tco issued",
    "temporary certificate",
    "final inspection",
    "expired",
    "void",
    "denied",
    "withdrawn",
    "cancelled",
    "canceled",
)


def _contains_phrase(haystack: str, phrase: str) -> bool:
    """Whole-word phrase match.

    Word boundaries matter here for a specific reason: the closed-status phrase "complete"
    appears as a substring inside "Incomplete Submittal", which is an *active* status. A
    plain substring test therefore classified a live, not-yet-submitted project as finished
    — the exact error this module exists to prevent.
    """
    if not phrase:
        return False
    escaped = re.escape(phrase).replace(r"\ ", r"\s+")
    return re.search(r"(?<![a-z0-9])" + escaped + r"(?![a-z0-9])", haystack) is not None


def procurement_status(project: Project, *, raw_text: str | None = None) -> str:
    """Determine the procurement status a project's evidence actually supports.

    `raw_text` is the concatenated source text for the project, used only to look for an
    explicit bid advertisement. Absent that, the permit status decides between "evidence
    found" and "not verified".

    Completion is tested before activity, because a status can contain both: "Final CO
    Issued" carries the active word "issued" but means the building is finished.
    """
    haystack_parts = [
        raw_text or "",
        project.project_status or "",
        project.mechanical_hvac_evidence or "",
    ]
    haystack = " ".join(haystack_parts).lower()

    # A confirmed-open claim requires explicit bid language from a source.
    if any(_contains_phrase(haystack, phrase) for phrase in BID_EVIDENCE_PHRASES):
        return CONFIRMED_OPEN

    status = (project.project_status or "").lower()
    if not status.strip():
        return NOT_VERIFIED

    # Completion wins over activity, and word boundaries stop "incomplete" reading as
    # "complete".
    if any(_contains_phrase(status, phrase) for phrase in CLOSED_PROCUREMENT_STATUS_PHRASES):
        return NOT_VERIFIED
    if any(_contains_phrase(status, phrase) for phrase in ACTIVE_PROCUREMENT_STATUS_PHRASES):
        return EVIDENCE_FOUND
    return NOT_VERIFIED


def procurement_explanation(status: str, project: Project) -> str:
    """A sentence explaining the status, safe to print in a customer-facing report."""
    if status == CONFIRMED_OPEN:
        return (
            "A source explicitly advertises this work for bid or award. Confirm deadlines "
            "with the issuing body before relying on this."
        )
    if status == EVIDENCE_FOUND:
        shown = project.project_status or "an active status"
        return (
            f"The permit record shows active work ({shown}), which indicates the project is "
            "proceeding. No source states how the work will be procured, so this is not a "
            "confirmed open bid."
        )
    return (
        "No source states a procurement status, and the recorded status does not indicate "
        "active work. Treat procurement as unverified. A permit existing does not mean the "
        "mechanical scope is still available."
    )


def is_claimable(project: Project) -> bool:
    """True when a project is worth putting in front of a customer.

    A finished, expired or withdrawn project is not an opportunity, however strong its
    mechanical evidence was. Keeping it off the customer report is the difference between a
    list a contractor trusts and one they stop reading.
    """
    return procurement_status(project) != NOT_VERIFIED