"""Building-level project identity.

The problem this solves: a single building can hold many suite-level records, and each one
assembles into its own project. Measured on the live database, 240 base street addresses have
more than one suite-level project, and one address (8687 N CENTRAL EXPY, Dallas) has 21.

Presented naively, a customer brief could show five "independent" opportunities that are
actually five suites in one office tower. That is the single fastest way to make the list
look inflated, and it is exactly the false positive the product exists to avoid.

The approach is deliberately conservative, because the opposite error is worse:

* A ``building_key`` groups projects that share a **normalised base address** — street number
  and street name, with any suite or unit designator removed. This is a grouping *hint*, not
  a merge.
* Projects are **never merged**. Each suite keeps its own record, its own permits and its own
  evidence. Only a relationship is recorded.
* The relationship is reported as *uncertain*, because a base address alone cannot prove two
  suites belong to one development. A shopping centre and a strip of separate buildings can
  share a street number.
* Customer-facing selection prefers to avoid presenting several siblings as independent
  projects, and says so plainly when it does.

Nothing here changes a classification, a gate, or an evidence record.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

#: Suite / unit designators that carry no building identity. Matched only when they appear
#: as a trailing comma- or hash-delimited segment, so a street named "Suite Street" is safe.
_SUITE_SEGMENT = re.compile(
    r"^\s*(?:"
    r"ste|suite|unit|apt|apartment|bldg|building|floor|fl|rm|room|no|num|number|"
    r"bu|su|#"
    r")\b\.?\s*[-#:]?\s*[\w\-]*\s*$",
    re.IGNORECASE,
)

#: A bare alphanumeric suite code such as "1010", "BU15", "140", "12B" appearing as a
#: trailing segment. Only applied to a segment that follows a street address.
_BARE_SUITE_CODE = re.compile(r"^\s*[A-Z]{0,3}[-#]?\d{1,5}[A-Z]?\s*$", re.IGNORECASE)


def building_key(address: str | None, city: str | None) -> str | None:
    """Derive a building-identity key from an address.

    The key is the street number plus street name, city, with suite designators removed:
    ``"8687 N CENTRAL EXPY, 1010"`` and ``"8687 N CENTRAL EXPY, 1028"`` both yield
    ``"8687 n central expy|dallas"``.

    Returns None when the address has no street number, because a corridor name such as
    "BLUE RIDGE TRL" cannot identify a single building and grouping on it would be wrong.
    """
    if not address or not address.strip():
        return None

    parts = [p.strip() for p in address.split(",") if p.strip()]
    if not parts:
        return None

    base = parts[0]
    # A suite designator can also be the *second* segment ("..., Suite 400") or appear in the
    # base itself ("100 MAIN ST STE 5"). Strip trailing designator segments from the base.
    while len(parts) > 1 and (
        _SUITE_SEGMENT.match(parts[1]) or _BARE_SUITE_CODE.match(parts[1])
    ):
        parts.pop(1)

    base = re.sub(
        r"\b(?:ste|suite|unit|apt|bldg|building|floor|fl)\b\.?\s*[-#:]?\s*[\w\-]+$",
        "",
        base,
        flags=re.IGNORECASE,
    ).strip()
    base = re.sub(r"#\s*[\w\-]+$", "", base).strip()

    if not re.match(r"^\s*\d+\s+\S", base):
        # No street number: not enough to identify a building.
        return None

    normalised = re.sub(r"[^a-z0-9 ]+", " ", base.lower())
    normalised = re.sub(r"\s+", " ", normalised).strip()
    city_part = re.sub(r"[^a-z0-9 ]+", " ", (city or "").lower()).strip()
    return f"{normalised}|{city_part}" if normalised else None


@dataclass
class ProjectGroup:
    """Projects that share a building key, and therefore may share a building."""

    building_key: str
    members: list[Any] = field(default_factory=list)

    @property
    def is_multi(self) -> bool:
        return len(self.members) > 1

    @property
    def size(self) -> int:
        return len(self.members)


@dataclass
class SiblingInfo:
    """How a single project relates to others sharing its base address."""

    building_key: str | None
    sibling_count: int = 0          # siblings excluding this project
    sibling_ids: list[int] = field(default_factory=list)

    @property
    def is_grouped(self) -> bool:
        return self.sibling_count > 0

    def describe(self) -> str:
        if not self.is_grouped:
            return "No other project shares this base address."
        noun = "project" if self.sibling_count == 1 else "projects"
        return (
            f"{self.sibling_count} other {noun} share this base street address. They are "
            "most likely separate suites or units in one building or development. This "
            "relationship is uncertain: a shared street number does not prove a shared "
            "building. Records are kept separate rather than merged."
        )


def group_projects(
    rows: Iterable[Any],
    *,
    address_getter=lambda r: r["address"],
    city_getter=lambda r: r["city"],
    id_getter=lambda r: r["id"],
) -> dict[str, ProjectGroup]:
    """Group project rows by building key.

    Rows without a usable building key are excluded, so they never participate in grouping.
    """
    groups: dict[str, ProjectGroup] = {}
    for row in rows:
        key = building_key(address_getter(row), city_getter(row))
        if not key:
            continue
        group = groups.setdefault(key, ProjectGroup(building_key=key))
        group.members.append(row)
    return groups


def sibling_info_for(
    project_id: int, groups: dict[str, ProjectGroup], *, id_getter=lambda r: r["id"]
) -> SiblingInfo:
    """Describe how one project relates to others sharing its building key."""
    for key, group in groups.items():
        ids = [id_getter(m) for m in group.members]
        if project_id in ids:
            others = [i for i in ids if i != project_id]
            return SiblingInfo(
                building_key=key, sibling_count=len(others), sibling_ids=others
            )
    return SiblingInfo(building_key=None)


def duplicate_statistics(groups: dict[str, ProjectGroup]) -> dict[str, int]:
    """Summarise grouping across the whole database, for the data quality report."""
    multi = [g for g in groups.values() if g.is_multi]
    grouped_projects = sum(g.size for g in multi)
    return {
        "building_keys": len(groups),
        "multi_project_keys": len(multi),
        "projects_in_multi_keys": grouped_projects,
        "largest_group": max((g.size for g in multi), default=0),
        # The number of customer-facing slots that grouping could free, if every multi-key
        # group were collapsed to one project. Reported as a bound, not a target: nothing is
        # actually merged.
        "potential_reduction": grouped_projects - len(multi),
    }