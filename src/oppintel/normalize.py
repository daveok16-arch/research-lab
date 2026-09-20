"""Normalization helpers: commercial filtering and mechanical-evidence detection.

Everything here operates on text that came from a public record. Nothing in this module
writes to a project field; it only produces facts and classifications that the assembler
then attaches with evidence via `provenance.assert_field`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import TradeConfig
from .models import Permit


@dataclass
class MechanicalSignal:
    """A detected mechanical/HVAC signal on a permit."""

    tier: int
    matched_keyword: str
    excerpt: str
    permit: Permit


def _contains_keyword(haystack: str, keyword: str) -> bool:
    """Word-boundary match for every keyword.

    Word boundaries matter more than they look. A bare substring test makes roofing text
    such as "mechanically fasten ... coverboard" register as mechanical scope, which would
    attach an HVAC claim to a roof replacement. Requiring a whole word keeps
    "mechanical, electrical and plumbing work" while rejecting the adverb.
    """
    keyword = keyword.lower().strip()
    if not keyword:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", haystack) is not None


#: Permit subtypes that denote construction work when no work description is supplied.
#: For example a Fort Worth "Commercial Building Permit" with subtype "New" and an empty
#: description is still unmistakably a new building.
CONSTRUCTION_SUBTYPES = {
    "new",
    "new construction",
    "remodel",
    "remodeling",
    "renovation",
    "addition",
    "alteration",
    "shell",
    "core and shell",
    "finish out",
    "finish-out",
    "build-out",
    "buildout",
    "tenant improvement",
    "interior remodel",
}


#: Permit types that denote a single trade rather than a building project. A subtype of
#: "New" on one of these means a new installation of that trade, not a construction project.
PURE_TRADE_TYPE_KEYWORDS = (
    "mechanical",
    "electrical",
    "plumbing",
    "refrigeration",
)


def has_construction_scope(permit: Permit, trade: TradeConfig) -> bool:
    """True when the permit describes construction work rather than a trade service call.

    A plumbing or mechanical permit alone is a service call, typically residential, and
    does not evidence a construction project. A project is only formed when at least one
    permit at the address describes building work.

    A construction subtype such as "New" is not sufficient on a pure trade permit: a
    "Mechanical / New" row means a new mechanical installation, so those rows must supply
    actual construction keywords in their description to count.
    """
    permit_type = (permit.permit_type or "").lower()
    subtype = (permit.permit_subtype or "").strip().lower()
    is_pure_trade = any(k in permit_type for k in PURE_TRADE_TYPE_KEYWORDS)

    if subtype and subtype in CONSTRUCTION_SUBTYPES and not is_pure_trade:
        return True

    text = permit.combined_text
    if not text:
        return False
    return any(k in text for k in trade.construction_activity_keywords)


def is_mechanical_permit(permit: Permit, trade: TradeConfig) -> bool:
    """True when the permit *type* itself denotes mechanical scope (Tier 1).

    This is the strongest possible evidence: the building department recorded mechanical
    work as the permit's own category.
    """
    permit_type = (permit.permit_type or "").lower()
    if not permit_type:
        return False
    return any(k in permit_type for k in trade.mechanical_permit_type_keywords)


def detect_mechanical_signal(permit: Permit, trade: TradeConfig) -> MechanicalSignal | None:
    """Detect the strongest mechanical/HVAC signal on a single permit.

    Tier 1 (permit type) outranks Tier 2 (scope text). Returns None when nothing is found,
    in which case no mechanical claim is made about that permit.
    """
    if is_mechanical_permit(permit, trade):
        permit_type = (permit.permit_type or "").lower()
        matched = next(
            (k for k in trade.mechanical_permit_type_keywords if k in permit_type),
            "mechanical",
        )
        return MechanicalSignal(
            tier=1,
            matched_keyword=matched,
            excerpt=f"Permit {permit.permit_number} is filed as '{permit.permit_type}'",
            permit=permit,
        )

    text = permit.combined_text
    if not text:
        return None
    for keyword in trade.mechanical_scope_keywords:
        if _contains_keyword(text, keyword):
            return MechanicalSignal(
                tier=2,
                matched_keyword=keyword,
                excerpt=_excerpt_for(permit, keyword),
                permit=permit,
            )
    return None


def _excerpt_for(permit: Permit, keyword: str) -> str:
    """Build a literal excerpt showing where the keyword was found."""
    for label, value in (
        ("work description", permit.work_description),
        ("permit type", permit.permit_type),
        ("land use", permit.land_use),
        ("specific use", permit.specific_use),
    ):
        if value and keyword in value.lower():
            return f"{label}: \"{value.strip()}\""
    return f"Keyword '{keyword}' matched permit {permit.permit_number}"


def is_excluded_residential(permit: Permit, trade: TradeConfig) -> bool:
    """True when a permit is clearly residential and should not form a project.

    The explicit commercial flag from the source is authoritative when present. Keyword
    matching is only consulted when the flag is absent.
    """
    if permit.is_commercial is True:
        return False
    if permit.is_commercial is False:
        return True

    haystack = " ".join(
        p for p in (
            permit.permit_type, permit.land_use, permit.specific_use, permit.work_description
        ) if p
    ).lower()
    if not haystack:
        return False
    return any(k in haystack for k in trade.residential_exclusion_keywords)


def commercial_candidate(permit: Permit, trade: TradeConfig) -> bool:
    """Decide whether a permit belongs to a commercial construction project.

    Three tests, each rejecting a distinct class of false opportunity:

    1. Not residential work.
    2. Has an address, because a project without one cannot be clustered or acted upon.
    3. Describes construction scope. A standalone plumbing or mechanical permit is a
       service call and would otherwise produce thousands of residential-flavoured
       "opportunities" that a commercial HVAC contractor cannot bid on.
    """
    if is_excluded_residential(permit, trade):
        return False
    if not permit.address:
        return False
    return has_construction_scope(permit, trade)


def derive_project_type(permits: list[Permit], trade: TradeConfig) -> tuple[str | None, dict | None]:
    """Derive a project type label and its matching property class from the evidence.

    Returns (project_type_label, property_class_dict). The label is descriptive only and
    is recorded as a derived value, never as a sourced fact.
    """
    combined = " ".join(p.combined_text for p in permits if p.combined_text)
    if not combined:
        return None, None

    klass = trade.property_class_for(combined)
    if klass:
        return str(klass.get("label")), klass

    # Fall back to the dominant permit activity where no property class matches.
    activity = " ".join(filter(None, (p.permit_subtype or p.permit_type for p in permits)))
    activity_lower = activity.lower()
    for keyword in trade.construction_activity_keywords:
        if keyword in activity_lower:
            return f"Commercial ({keyword})", None
    return "Commercial", None


def primary_permit(permits: list[Permit], trade: TradeConfig) -> Permit:
    """Pick the permit that best describes the project.

    A permit describing construction work outranks a trade permit, because it carries the
    project's value, area, and description. Ties break on newest issue date, then on
    permit number, so the choice is deterministic across runs.
    """
    def sort_key(permit: Permit) -> tuple:
        permit_type = (permit.permit_type or "").lower()
        is_pure_trade = any(k in permit_type for k in PURE_TRADE_TYPE_KEYWORDS)
        is_building = ("building" in permit_type or "commercial" in permit_type) and not is_pure_trade
        construction = has_construction_scope(permit, trade)

        if is_building:
            rank = 0
        elif construction:
            rank = 1
        elif is_pure_trade:
            rank = 3
        else:
            rank = 2
        return (
            rank,
            -(permit.permit_date.toordinal() if permit.permit_date else 0),
            str(permit.permit_number),
        )

    return sorted(permits, key=sort_key)[0]