"""Opportunity classification.

Labels a project HIGH, MEDIUM, or NEEDS_VERIFICATION from documented evidence only.

The single most important rule in this module is the mechanical-evidence gate: a project
cannot be labelled HIGH without Tier-1 or Tier-2 mechanical evidence, however large its
value or attractive its property class. Under-claiming is the correct failure mode for a
tool whose usefulness depends on the customer trusting it.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from .config import TradeConfig
from .constants import HIGH, MEDIUM, NEEDS_VERIFICATION
from .models import Project, utcnow


def has_precise_location(address: str | None) -> bool:
    """True when an address begins with a street number, e.g. '12361 MAVERICK RANCH RD'.

    Some public records carry a corridor or street name with no number. The value is still
    real and sourced, but it does not identify a single property.
    """
    if not address:
        return False
    return re.match(r"^\s*\d+\s+\S", address) is not None


def classify(
    project: Project,
    trade: TradeConfig,
    *,
    reference_date: date | None = None,
) -> Project:
    """Score and label a project, populating classification fields in place.

    Every point awarded appends a human-readable reason to `classification_reasons`, so a
    reviewer can always reconstruct why a project received its label.
    """
    reasons: list[str] = []
    score = 0
    scoring = trade.scoring
    thresholds = trade.thresholds
    today = reference_date or utcnow().date()

    commercial_class = project.property_class not in (None, "Commercial (unspecified)")

    # --- Mechanical evidence (dominant factor) ---------------------------------
    has_mechanical_evidence = project.mechanical_evidence_tier in (1, 2)
    if project.mechanical_evidence_tier == 1:
        score += int(scoring.get("mechanical_permit_points", 45))
        reasons.append(
            f"Mechanical permit on file at this address. {project.mechanical_hvac_evidence}"
        )
    elif project.mechanical_evidence_tier == 2:
        score += int(scoring.get("mechanical_scope_points", 30))
        reasons.append(
            f"Mechanical scope stated in permit text. {project.mechanical_hvac_evidence}"
        )

    # --- Property class --------------------------------------------------------
    if commercial_class:
        klass = next(
            (k for k in trade.property_classes if k.get("label") == project.property_class),
            None,
        )
        if klass:
            points = int(klass.get("points", 0))
            if points:
                score += points
                reasons.append(f"{klass.get('label')} project class.")

    # --- Scale -----------------------------------------------------------------
    if project.estimated_project_value is not None:
        for tier in scoring.get("value_tiers", []):
            if project.estimated_project_value >= tier["min"]:
                score += int(tier["points"])
                reasons.append(
                    f"Declared value ${project.estimated_project_value:,.0f}."
                )
                break

    if project.square_footage is not None:
        for tier in scoring.get("size_tiers", []):
            if project.square_footage >= tier["min"]:
                score += int(tier["points"])
                reasons.append(f"Declared area {project.square_footage:,.0f} sq ft.")
                break

    # --- Construction phase ----------------------------------------------------
    if _is_active_status(project.project_status, trade):
        score += int(scoring.get("active_status_points", 15))
        reasons.append(f"Permit status indicates active work ({project.project_status}).")

    if project.permit_date:
        window = int(scoring.get("recent_permit_days", 180))
        if project.permit_date >= today - timedelta(days=window):
            score += int(scoring.get("recent_permit_points", 10))
            reasons.append(f"Permit filed within the last {window} days.")

    # --- Label thresholds, then the gates --------------------------------------
    high_min = int(thresholds.get("high_min_score", 70))
    medium_min = int(thresholds.get("medium_min_score", 40))

    if score >= high_min:
        label = HIGH
    elif score >= medium_min:
        label = MEDIUM
    else:
        label = NEEDS_VERIFICATION

    gate_enabled = bool(thresholds.get("requires_mechanical_evidence_for_high", True))
    if label == HIGH and gate_enabled and not has_mechanical_evidence:
        label = MEDIUM
        reasons.append(
            "Held below HIGH: no mechanical/HVAC permit or scope text is on record for "
            "this address. Mechanical scope must be confirmed independently."
        )

    # A scale floor stops small jobs from being sold as major opportunities. A $18,000
    # roof replacement that happens to mention "HVAC curbs" in passing is not an HVAC
    # opportunity. Tier-1 mechanical permits are exempt, because a mechanical permit is
    # direct evidence of mechanical work regardless of the declared job value.
    min_value = thresholds.get("high_min_value")
    min_sqft = thresholds.get("high_min_sqft")
    if label == HIGH and project.mechanical_evidence_tier != 1:
        meets_scale = False
        if min_value is not None and project.estimated_project_value is not None:
            meets_scale = project.estimated_project_value >= float(min_value)
        if not meets_scale and min_sqft is not None and project.square_footage is not None:
            meets_scale = project.square_footage >= float(min_sqft)
        if not meets_scale:
            label = MEDIUM
            reasons.append(
                "Held below HIGH: the record shows no value or floor area large enough to "
                "indicate a commercial-scale mechanical opportunity. Confirm scale with the "
                "issuing jurisdiction."
            )

    if label == MEDIUM and thresholds.get("medium_requires_commercial_class"):
        if not commercial_class and not has_mechanical_evidence:
            label = NEEDS_VERIFICATION
            reasons.append(
                "Held below MEDIUM: property class is not a recognised commercial class "
                "and no mechanical evidence is on record."
            )

    # An opportunity a contractor cannot locate is not yet actionable. Some sources publish
    # an address with no street number (for example a CAD record for "BLUE RIDGE TRL,
    # PLANO"). The value is real and the source is cited, but the location must be resolved
    # before anyone can act on it.
    if has_precise_location(project.address):
        project.location_precision = "precise"
    else:
        project.location_precision = "approximate"
        if label in (HIGH, MEDIUM):
            label = NEEDS_VERIFICATION
            reasons.append(
                "Held at NEEDS_VERIFICATION: the source address has no street number, so "
                "the exact property must be resolved before this can be acted upon."
            )
        else:
            reasons.append(
                "Source address has no street number; the exact property must be resolved."
            )

    if not has_mechanical_evidence:
        reasons.append(
            "No mechanical/HVAC evidence found in the public record. Treat scope as "
            "unconfirmed."
        )

    project.classification = label
    project.classification_score = score
    project.classification_reasons = reasons
    return project


def _is_active_status(status: str | None, trade: TradeConfig) -> bool:
    """True when the permit status indicates work is proceeding or under review.

    Inactive statuses (finaled, void, denied, expired) take precedence, so a status that
    contains both an active and an inactive keyword is treated as inactive.
    """
    if not status:
        return False
    lowered = status.lower()
    if any(k in lowered for k in trade.inactive_status_keywords):
        return False
    return any(k in lowered for k in trade.active_status_keywords)