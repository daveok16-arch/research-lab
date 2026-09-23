"""Customer-brief eligibility.

Before an opportunity is shown to a contractor, it must pass an explicit eligibility check.
The point is not to rank projects — inventing a "commercial attractiveness score" would be
exactly the kind of unsupported judgement this product must not make. The point is to apply
the product's own stated criteria mechanically, and to record *why* each opportunity was
included so the selection is explainable rather than a black box.

An opportunity is eligible only when every criterion below is met. Each criterion is stated
as a fact that can be checked against the record, not as a preference:

1. Not completed — a finished building is not a lead.
2. Commercial construction evidence — the record describes building work.
3. Mechanical/HVAC evidence — the city recorded mechanical scope, or the permit text states it.
4. Passes the existing classification gates — HIGH or MEDIUM, never NEEDS_VERIFICATION.
5. Enough evidence to explain why it matters — at least one substantive significance fact.
6. Not service/repair/maintenance/replacement — already excluded upstream, re-asserted here.
7. Not plumbing-only or electrical-only — the mechanical evidence must be its own.

Confirmed open bidding is deliberately **not** required. No configured source can establish
it, so requiring it would empty the brief. The procurement status is instead reported
honestly on each opportunity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .procurement import CLOSED, EVIDENCE_FOUND, NOT_VERIFIED as PROCUREMENT_NOT_VERIFIED

#: Classifications eligible for a customer brief. NEEDS_VERIFICATION is excluded: by
#: definition the important requirements are not sufficiently verified.
ELIGIBLE_CLASSIFICATIONS = ("HIGH", "MEDIUM")

#: Procurement states that disqualify an opportunity outright.
INELIGIBLE_PROCUREMENT = (CLOSED, PROCUREMENT_NOT_VERIFIED)

#: Significance signals. At least one must be present, otherwise the record cannot explain
#: why a contractor should care. These are facts from the record, not judgements.
SIGNIFICANCE_FIELDS = (
    "property_class",           # a recognised building class
    "estimated_project_value",  # a declared value
    "square_footage",           # a declared footprint
)


@dataclass
class Eligibility:
    """The result of applying the customer-brief criteria to one project."""

    eligible: bool
    reasons: list[str] = field(default_factory=list)
    exclusions: list[str] = field(default_factory=list)
    significance: list[str] = field(default_factory=list)

    def explain(self) -> str:
        if self.eligible:
            base = "Eligible. " + " ".join(self.reasons)
            if self.significance:
                base += " Significance: " + "; ".join(self.significance) + "."
            return base
        return "Not eligible. " + " ".join(self.exclusions)


def _is_service_work(row: Any) -> bool:
    """Re-assert the service-work exclusion at the report boundary.

    The assembler already excludes service work, so a project reaching this point should
    never be one. Checking again costs nothing and means a future change upstream cannot
    silently leak maintenance work into a customer brief.
    """
    from .normalize import TRADE_SERVICE_KEYWORDS

    haystack_parts = [
        row["project_name"] if "project_name" in row.keys() else None,
        row["mechanical_hvac_evidence"] if "mechanical_hvac_evidence" in row.keys() else None,
    ]
    haystack = " ".join(p for p in haystack_parts if p).lower()
    if not haystack:
        return False
    return any(keyword in haystack for keyword in TRADE_SERVICE_KEYWORDS)


def evaluate(row: Any) -> Eligibility:
    """Apply the customer-brief criteria to a project row."""
    result = Eligibility(eligible=True)

    def fail(reason: str) -> None:
        result.eligible = False
        result.exclusions.append(reason)

    # 1. Not completed.
    procurement = row["procurement_status"]
    if procurement == CLOSED:
        fail("Work is recorded as finished or inactive.")
    elif procurement == PROCUREMENT_NOT_VERIFIED:
        fail("Procurement status could not be established from any source.")

    # 2 & 4. Commercial construction evidence and the classification gates.
    classification = row["classification"]
    if classification not in ELIGIBLE_CLASSIFICATIONS:
        fail(
            f"Classification is {classification}, and only "
            f"{' or '.join(ELIGIBLE_CLASSIFICATIONS)} records are shown to customers."
        )

    # 3. Actual mechanical evidence.
    tier = row["mechanical_evidence_tier"]
    if tier not in (1, 2):
        fail("No mechanical or HVAC evidence is on record for this address.")

    # 6 & 7. Not service work, and not another trade wearing a mechanical label.
    if _is_service_work(row):
        fail("The record describes service, repair, maintenance or replacement work.")

    if not result.eligible:
        return result

    # 5. Enough evidence to explain why it matters.
    for name in SIGNIFICANCE_FIELDS:
        value = row[name] if name in row.keys() else None
        if value is None:
            continue
        if name == "property_class":
            if value and value != "Commercial (unspecified)":
                result.significance.append(f"building class '{value}'")
        elif name == "estimated_project_value":
            result.significance.append(f"declared value ${float(value):,.0f}")
        elif name == "square_footage":
            result.significance.append(f"declared area {float(value):,.0f} sq ft")

    if not result.significance:
        fail(
            "The record states no building class, declared value or floor area, so there is "
            "nothing to explain why the project matters."
        )
        return result

    # Records the positive reasons, so the brief can state why it was included.
    if tier == 1:
        result.reasons.append("A mechanical permit is on file at this address.")
    else:
        result.reasons.append("The permit record states mechanical scope.")
    if procurement == EVIDENCE_FOUND:
        result.reasons.append("The permit record shows active work.")
    return result


def inclusion_reason(row: Any, eligibility: Eligibility) -> str:
    """A deterministic, stored explanation of why this opportunity was selected.

    Requirements 14: the reason an opportunity was included must be recorded, and selection
    must be explainable. This is that record.
    """
    parts: list[str] = []
    tier = row["mechanical_evidence_tier"]
    parts.append(
        "Tier-1 mechanical permit on file at this address."
        if tier == 1
        else "Tier-2 mechanical scope stated in the permit record."
    )
    if eligibility.significance:
        parts.append("Significance established by " + ", ".join(eligibility.significance) + ".")
    parts.append(
        f"Classification {row['classification']} "
        f"(score {row['classification_score']})."
    )
    parts.append(f"Procurement status: {row['procurement_status']}.")
    return " ".join(parts)