"""Provenance: the enforcement point for the platform's critical data rule.

The rule is: never invent, infer, or hallucinate missing project information. If a value
cannot be verified from a public source, it is reported as "Not verified."

This module is what makes that rule structural rather than aspirational. A project field
is only ever populated through `assert_field`, which simultaneously records the evidence
that licenses the value. It is not possible to set a factual project field without
producing an Evidence row, so an unsourced fact cannot be created by accident.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .constants import NOT_VERIFIED
from .models import Evidence, Permit, Project


class ProvenanceError(RuntimeError):
    """Raised when a caller attempts to assert a fact with no supporting source."""


def assert_field(
    project: Project,
    field_name: str,
    value: Any,
    *,
    source_id: str,
    source_name: str,
    source_url: str | None,
    evidence_type: str = "permit",
    source_record_key: str | None = None,
    source_date: date | None = None,
    tier: int | None = None,
    excerpt: str | None = None,
    observed_at: datetime | None = None,
    overwrite: bool = True,
    evidence_only: bool = False,
) -> bool:
    """Set a project field and record the evidence that licenses it.

    Returns True if the field was set, False if there was nothing to assert.

    A blank or sentinel value is never written. This is the guard that stops upstream
    noise such as the literal string "NULL" from becoming a published fact.

    `overwrite=False` implements first-writer-wins precedence for the *displayed value*, which
    the assembler uses so that the primary permit's value is not clobbered by a later trade
    permit at the same address. It does not suppress the evidence row: if a second source
    states a different value, that fact is still recorded so the disagreement can be
    detected and surfaced rather than silently resolved by precedence.

    `evidence_only=True` records the evidence without touching the field. Used for values a
    project legitimately has several of, such as the permit numbers of every contributing
    permit, where the displayed value stays the primary permit's number.
    """
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    if isinstance(value, str) and value.strip().upper() in {"NULL", "N/A", "NA", "NONE", "-"}:
        return False
    if not source_id or not source_name:
        raise ProvenanceError(
            f"Refusing to assert {field_name!r} without an identified source."
        )

    value_was_set = False
    if not evidence_only:
        currently = getattr(project, field_name, None)
        if overwrite or currently is None:
            setattr(project, field_name, value)
            value_was_set = True

    # Identical facts from the same source are recorded once. Different values from
    # different sources are both recorded, which is what makes a contradiction visible
    # rather than silently resolved by precedence.
    for existing in project.evidence:
        if (
            existing.field_name == field_name
            and existing.source_id == source_id
            and existing.value == str(value)
        ):
            return value_was_set

    evidence = Evidence(
        field_name=field_name,
        value=str(value),
        source_id=source_id,
        source_name=source_name,
        source_url=source_url,
        source_record_key=source_record_key,
        source_date=source_date,
        evidence_type=evidence_type,
        tier=tier,
        excerpt=excerpt,
    )
    if observed_at is not None:
        evidence.observed_at = observed_at
    project.evidence.append(evidence)
    return True


def record_permit_evidence(
    project: Project,
    permit: Permit,
    source_name: str,
    *,
    observed_at: datetime | None = None,
    overwrite: bool = False,
) -> None:
    """Record the directly-observed permit facts that are always present.

    These are attributes of the permit record itself: the permit exists, it has a number,
    a date, an address, and a status. They are the substrate every project is built on.

    With `overwrite=False` (the default) the first permit recorded for a project wins, so
    the primary building permit's value, area, and description are not replaced by a later
    trade permit at the same address.
    """
    common = dict(
        source_id=permit.source_id,
        source_name=source_name,
        source_url=permit.source_url,
        source_record_key=permit.natural_key,
        source_date=permit.source_date,
        observed_at=observed_at,
        overwrite=overwrite,
    )

    # Permit numbers of every contributing permit are kept, because a project legitimately
    # has several. The primary permit's number is what the record displays.
    if permit.permit_number and not any(
        e.field_name == "permit_number" and e.value == permit.permit_number
        for e in project.evidence
    ):
        assert_field(
            project, "permit_number", permit.permit_number,
            evidence_type="permit", excerpt=f"Permit number {permit.permit_number}",
            evidence_only=project.permit_number is not None,
            **common,
        )
    if permit.permit_date:
        assert_field(
            project, "permit_date", permit.permit_date,
            evidence_type="permit",
            excerpt=f"Permit filed {permit.permit_date.isoformat()}",
            **common,
        )
    if permit.address:
        assert_field(
            project, "address", permit.address,
            evidence_type="permit", excerpt=f"Situs address {permit.address}", **common,
        )
    if permit.city:
        assert_field(
            project, "city", permit.city,
            evidence_type="permit", excerpt=f"Jurisdiction {permit.city}", **common,
        )
    if permit.state:
        assert_field(
            project, "state", permit.state,
            evidence_type="permit", excerpt=f"State {permit.state}", **common,
        )
    if permit.status:
        assert_field(
            project, "project_status", permit.status,
            evidence_type="permit", excerpt=f"Permit status {permit.status}", **common,
        )
    if permit.job_value is not None:
        assert_field(
            project, "estimated_project_value", permit.job_value,
            evidence_type="permit",
            excerpt=f"Declared job value {permit.job_value:,.0f} on permit {permit.permit_number}",
            **common,
        )
    if permit.square_footage is not None:
        assert_field(
            project, "square_footage", permit.square_footage,
            evidence_type="permit",
            excerpt=f"Declared area {permit.square_footage:,.0f} sq ft on permit {permit.permit_number}",
            **common,
        )
    if permit.work_description:
        # The work description is the source of the project name only when it is a genuine
        # descriptive string. Short fragments such as "RENOVATION" are not project names.
        text = permit.work_description.strip()
        if len(text) >= 12:
            assert_field(
                project, "project_name", text,
                evidence_type="permit",
                excerpt=f"Work description: {text}",
                **common,
            )


def unverified_fields(project: Project) -> list[str]:
    """Return the required project fields that remain unverified for this project."""
    from .constants import REQUIRED_PROJECT_FIELDS

    missing = []
    for name in REQUIRED_PROJECT_FIELDS:
        value = getattr(project, name, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(name)
    return missing


def evidence_for(project: Project, field_name: str) -> list[Evidence]:
    return [e for e in project.evidence if e.field_name == field_name]


def render(value: Any) -> str:
    """Render any value for display, applying the not-verified rule."""
    if value is None:
        return NOT_VERIFIED
    if isinstance(value, str):
        return value if value.strip() else NOT_VERIFIED
    if isinstance(value, float):
        return f"{value:,.0f}"
    if isinstance(value, date):
        return value.isoformat()
    return str(value)