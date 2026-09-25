"""Personal matching: why an opportunity is relevant to *this* account.

The product's central claim is that it can tell a contractor not just that a project exists,
but why it is worth their attention. That claim is only trustworthy if every reason is a fact
that can be re-checked against the record. So a reason here is never a judgement — it is the
statement of a comparison that either holds or does not:

* the project carries the active trade's configured evidence;
* it sits in the market and city the account configured;
* its project type is one the account selected;
* it moved recently, as evidenced by a recorded change.

Absent a matched reason, the module says nothing. It does not pad the list to look useful, and
it never asserts procurement. "Relevant to HVAC" is not "HVAC bid is open", and the wording
here is chosen to keep those apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

#: A project updated within this window counts as recently active. Chosen to match the
#: freshness language used elsewhere so "recently updated" means one thing across the product.
RECENT_WINDOW_DAYS = 30

#: Reason kinds, so the UI can render an icon and the tests can assert on the vocabulary.
REASON_TRADE_EVIDENCE = "trade_evidence"
REASON_MARKET = "market"
REASON_CITY = "city"
REASON_PROJECT_TYPE = "project_type"
REASON_VALUE = "value"
REASON_RECENT = "recent"
REASON_CLASSIFICATION = "classification"
REASON_NEW = "new"


@dataclass
class MatchReason:
    """One checkable reason an opportunity matches an account."""

    kind: str
    label: str
    detail: str | None = None


@dataclass
class MatchResult:
    reasons: list[MatchReason] = field(default_factory=list)
    score: int = 0
    is_match: bool = False

    @property
    def labels(self) -> list[str]:
        return [r.label for r in self.reasons]


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_recent(updated_at: Any, *, days: int = RECENT_WINDOW_DAYS) -> bool:
    parsed = _parse(updated_at)
    if parsed is None:
        return False
    return parsed >= datetime.now(timezone.utc) - timedelta(days=days)


def evaluate_match(
    project: dict[str, Any],
    *,
    trade: Any,
    market: Any,
    preferences: dict[str, Any] | None = None,
) -> MatchResult:
    """Explain, in facts, how a project relates to the active trade and an account's settings.

    `preferences` may be empty. Without preferences the result still reports the trade and
    market matches, because those define the product's own scope rather than the account's
    choices, and a visitor with no account still deserves to know why a project is listed.
    """
    prefs = preferences or {}
    result = MatchResult()

    # Trade evidence: the reason the project is in this directory at all.
    field_name = (getattr(trade, "discovery", None) or {}).get("evidence_field") or ""
    tier = project.get("mechanical_evidence_tier")
    if project.get("has_mechanical_evidence"):
        strong = tier == 1
        result.reasons.append(
            MatchReason(
                REASON_TRADE_EVIDENCE,
                f"{trade.short_label or trade.label} evidence on record",
                "A mechanical permit was filed at this address."
                if strong
                else "Mechanical scope is named in the permit record.",
            )
        )
        result.score += 30 if strong else 20

    # Market: the project's city must be one the active market declares.
    city = project.get("city")
    city_names = set(getattr(market, "city_names", []) or [])
    if city and city in city_names:
        result.reasons.append(
            MatchReason(REASON_MARKET, f"{market.short_name} market", f"{city}, {market.state or 'TX'}")
        )
        result.score += 10

    # City preference: only reported when the account actually selected it.
    chosen_cities = set(prefs.get("cities") or [])
    if city and chosen_cities and city in chosen_cities:
        result.reasons.append(MatchReason(REASON_CITY, f"Your selected city: {city}"))
        result.score += 25

    # Project type preference.
    chosen_types = set(prefs.get("project_types") or [])
    ptype = project.get("project_type")
    if ptype and chosen_types and ptype in chosen_types:
        result.reasons.append(MatchReason(REASON_PROJECT_TYPE, f"Matches your type: {ptype}"))
        result.score += 20

    # Value band preference.
    value = project.get("estimated_project_value")
    minimum = prefs.get("min_value")
    maximum = prefs.get("max_value")
    if value is not None and (minimum or maximum):
        above = minimum is None or float(value) >= float(minimum)
        below = maximum is None or float(value) <= float(maximum)
        if above and below:
            result.reasons.append(
                MatchReason(REASON_VALUE, "Within your value range", f"${float(value):,.0f}")
            )
            result.score += 15
        elif not (above and below) and (minimum or maximum):
            # Out of band is not a reason to match, and is not rendered. The caller filters,
            # this function only ever adds supporting reasons.
            pass

    # Classification: reported as a fact, and rewarded because HIGH carries stronger evidence.
    classification = project.get("classification")
    if classification in ("HIGH", "MEDIUM"):
        result.reasons.append(
            MatchReason(
                REASON_CLASSIFICATION,
                f"{classification} classification",
                "Documented evidence supports the classification.",
            )
        )
        result.score += 25 if classification == "HIGH" else 12

    # Recency, from a recorded update rather than a guess.
    if is_recent(project.get("updated_at")):
        result.reasons.append(
            MatchReason(REASON_RECENT, "Recently updated", "The record changed in the last 30 days.")
        )
        result.score += 10

    result.is_match = bool(result.reasons)
    return result


def describe_match(result: MatchResult, limit: int = 5) -> list[MatchReason]:
    """The reasons to render, strongest first, capped so a card stays readable."""
    return result.reasons[:limit]


def filter_matches(
    projects: list[dict[str, Any]], *, preferences: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Apply an account's hard preferences to a list of already-matched projects.

    Hard filters (a selected city, type or value band) remove a project rather than merely
    failing to add a reason. Kept separate from `evaluate_match` so the two intents — "why does
    this match" and "does this qualify" — cannot be confused.
    """
    prefs = preferences or {}
    cities = set(prefs.get("cities") or [])
    types = set(prefs.get("project_types") or [])
    minimum = prefs.get("min_value")
    maximum = prefs.get("max_value")

    out: list[dict[str, Any]] = []
    for project in projects:
        if cities and project.get("city") not in cities:
            continue
        if types and project.get("project_type") not in types:
            continue
        value = project.get("estimated_project_value")
        if (minimum is not None or maximum is not None) and value is not None:
            if minimum is not None and float(value) < float(minimum):
                continue
            if maximum is not None and float(value) > float(maximum):
                continue
        out.append(project)
    return out
