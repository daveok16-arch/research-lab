"""Discrepancy detection across sources.

When two public sources state different values for the same project field, the platform must
not silently pick one. A silently chosen value is indistinguishable from an invented one,
and it destroys the answer to "where did you get this?" the moment a customer checks a
second source.

Instead, both values are retained as separate sourced facts, and the disagreement is
recorded explicitly. The project field is then marked as disputed so that the dashboard and
the client report can present both figures rather than one merged number.

Example this exists for:

    TDLR value:        $250,000,000
    City permit value: $264,000,000

Both are real. The correct output is two facts and a flag, not an average and not the newer
one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Evidence, Project

#: Relative tolerance below which two numeric values are treated as the same fact.
#: Sources round differently (a CAD record may report $4,300,000 where the permit says
#: $4,300,000.00), so exact equality would produce spurious disputes.
DEFAULT_RELATIVE_TOLERANCE = 0.005

#: Fields where a disagreement is materially important to the customer.
MATERIAL_FIELDS = (
    "estimated_project_value",
    "square_footage",
    "address",
    "city",
    "project_status",
    "owner",
    "general_contractor",
    "architect",
    "project_type",
    "permit_date",
)


@dataclass
class Discrepancy:
    """A disagreement between two sources about one project field."""

    field_name: str
    values: list[dict[str, Any]] = field(default_factory=list)

    def describe(self) -> str:
        parts = [
            f"{v['value']} (per {v['source_name']})" for v in self.values
        ]
        return f"{self.field_name} differs between sources: " + "; ".join(parts)


def _numeric(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").replace("$", "").strip())
        except ValueError:
            return None
    return None


def values_agree(a: Any, b: Any, relative_tolerance: float = DEFAULT_RELATIVE_TOLERANCE) -> bool:
    """Decide whether two source values represent the same fact.

    Numeric values are compared with a relative tolerance so that rounding differences are
    not reported as disputes. Everything else is compared case-insensitively after
    whitespace normalization.
    """
    na, nb = _numeric(a), _numeric(b)
    if na is not None and nb is not None:
        if na == nb:
            return True
        scale = max(abs(na), abs(nb))
        if scale == 0:
            return True
        return abs(na - nb) / scale <= relative_tolerance
    sa = " ".join(str(a).split()).lower() if a is not None else ""
    sb = " ".join(str(b).split()).lower() if b is not None else ""
    return sa == sb


def find_discrepancies(
    project: Project,
    *,
    fields: tuple[str, ...] = MATERIAL_FIELDS,
    relative_tolerance: float = DEFAULT_RELATIVE_TOLERANCE,
) -> list[Discrepancy]:
    """Find fields where the evidence rows disagree.

    Operates on the recorded evidence, not on the project's resolved field values, so a
    disagreement that the assembler resolved by precedence is still surfaced.
    """
    found: list[Discrepancy] = []
    for name in fields:
        rows = [e for e in project.evidence if e.field_name == name and e.value is not None]
        if len(rows) < 2:
            continue

        distinct: list[Evidence] = []
        for row in rows:
            if not any(values_agree(row.value, other.value, relative_tolerance) for other in distinct):
                distinct.append(row)
        if len(distinct) < 2:
            continue

        found.append(
            Discrepancy(
                field_name=name,
                values=[
                    {
                        "value": row.value,
                        "source_id": row.source_id,
                        "source_name": row.source_name,
                        "source_url": row.source_url,
                        "source_record_key": row.source_record_key,
                        "source_date": row.source_date.isoformat() if row.source_date else None,
                        "excerpt": row.excerpt,
                    }
                    for row in distinct
                ],
            )
        )
    return found


def mark_disputed(project: Project, discrepancies: list[Discrepancy]) -> None:
    """Record the disputed fields on the project and append an explanatory reason.

    The resolved field value is deliberately left as-is. Precedence already decided which
    value the record displays; the discrepancy list is what stops that decision from being
    invisible.
    """
    if not discrepancies:
        return
    project.discrepancies = [
        {"field_name": d.field_name, "values": d.values} for d in discrepancies
    ]
    project.disputed_fields = [d.field_name for d in discrepancies]
    for d in discrepancies:
        rendered = ", ".join(f"{v['value']} ({v['source_name']})" for v in d.values)
        project.classification_reasons.append(
            f"Sources disagree on {d.field_name}: {rendered}. Both values are retained and "
            "the discrepancy is unresolved."
        )


def discrepancy_summary(project: Project) -> str:
    """One-line summary for reports, or an explicit statement that there is none."""
    if not project.discrepancies:
        return "No source discrepancies detected."
    parts = []
    for item in project.discrepancies:
        rendered = " vs ".join(
            f"{v['value']} ({v['source_name']})" for v in item["values"]
        )
        parts.append(f"{item['field_name']}: {rendered}")
    return " | ".join(parts)