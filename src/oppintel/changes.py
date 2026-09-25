"""Change detection.

Monitoring is only credible if the product can state what actually changed and when. This
module compares a project as it exists now against the snapshot taken on the previous
assembly pass, and emits only the differences it can see. Nothing is inferred: if two passes
agree, there is no event, and no event means no alert and no timeline entry.

This lives in the intelligence layer rather than the application layer because it is a
statement about the collected data, not about how the website displays it. The web tier reads
the change rows; it never decides what changed.

Deliberately narrow: a change is recorded only for a field the product makes a claim about.
`updated_at` moving because the pipeline re-ran is not a change — that would turn every ingest
into a notification storm and make the timeline worthless.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

#: Fields whose value is compared between passes. Chosen because each is a field the product
#: displays and draws a conclusion from, so a change to it is meaningful to a contractor.
TRACKED_FIELDS: tuple[tuple[str, str], ...] = (
    ("classification", "classification"),
    ("classification_score", "classification score"),
    ("procurement_status", "procurement status"),
    ("project_status", "project status"),
    ("mechanical_evidence_tier", "mechanical evidence"),
    ("estimated_project_value", "estimated value"),
    ("square_footage", "square footage"),
    ("project_type", "project type"),
    ("project_name", "project name"),
    ("owner", "owner"),
    ("developer", "developer"),
    ("general_contractor", "general contractor"),
    ("architect", "architect"),
    ("permit_number", "permit number"),
    ("permit_date", "permit date"),
    ("mechanical_hvac_evidence", "mechanical scope"),
)

#: Change kinds. A controlled vocabulary so the UI, the alerts and the timeline all classify
#: an event the same way instead of each inventing its own labels.
NEW_PROJECT = "new_project"
NEW_PERMIT = "new_permit"
MECHANICAL_ADDED = "mechanical_evidence_added"
STATUS_CHANGED = "status_changed"
PROCUREMENT_CHANGED = "procurement_changed"
CLASSIFICATION_CHANGED = "classification_changed"
VALUE_CHANGED = "value_changed"
EVIDENCE_UPDATED = "evidence_updated"
CLOSED = "project_closed"

#: Kinds that a contractor would want an alert about, as opposed to a routine correction.
#: A record correction is still shown on the timeline; it just does not raise a notification.
NOTIFIABLE_KINDS = frozenset(
    {NEW_PROJECT, NEW_PERMIT, MECHANICAL_ADDED, STATUS_CHANGED, PROCUREMENT_CHANGED,
     CLASSIFICATION_CHANGED, CLOSED}
)


@dataclass
class DetectedChange:
    """One difference between the previous and current state of a project."""

    project_id: int
    field_name: str
    change_kind: str
    summary: str
    previous_value: Any = None
    current_value: Any = None
    source_id: str | None = None
    source_name: str | None = None
    source_url: str | None = None

    @property
    def is_notifiable(self) -> bool:
        return self.change_kind in NOTIFIABLE_KINDS


def _normalise(value: Any) -> Any:
    """A comparable form of a stored value.

    Empty strings are folded to None because a source that starts publishing an empty field
    has not changed a fact, and 5.0 and "5" must compare equal so a serialisation difference
    in the database does not manufacture an event.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _display(value: Any) -> str:
    if value is None:
        return "Not verified"
    if isinstance(value, float) and value.is_integer():
        return f"{int(value):,}"
    if isinstance(value, (int,)):
        return f"{value:,}"
    return str(value)


def _hash(values: dict[str, Any]) -> str:
    payload = json.dumps(values, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def snapshot_values(project: dict[str, Any], permits: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The subset of a project that monitoring compares, in a stable serialisable form.

    Permit identities are included so that a newly filed permit at an existing address is
    detected even when every project-level field is unchanged. That is the common case for a
    building that is already under way, and it is exactly the signal a contractor wants.
    """
    values: dict[str, Any] = {name: _normalise(project.get(name)) for name, _ in TRACKED_FIELDS}
    if permits is not None:
        values["_permit_keys"] = sorted(
            str(p.get("natural_key") or p.get("permit_number") or "") for p in permits
        )
    return values


def state_hash(values: dict[str, Any]) -> str:
    return _hash(values)


def _permits_added(previous: list[str], current: list[str]) -> list[str]:
    return [key for key in current if key not in set(previous)]


def diff_project(
    project: dict[str, Any],
    previous_values: dict[str, Any] | None,
    permits: list[dict[str, Any]] | None = None,
) -> list[DetectedChange]:
    """Differences between a project's current state and its previous snapshot.

    `previous_values is None` means the project has not been seen before, which is itself an
    event: it is the moment the opportunity entered the system.
    """
    current = snapshot_values(project, permits)
    project_id = int(project["id"])

    if previous_values is None:
        return [
            DetectedChange(
                project_id=project_id,
                field_name="__project__",
                change_kind=NEW_PROJECT,
                summary="Project first entered the system.",
                current_value=project.get("project_name") or project.get("address"),
                source_name=project.get("source_name"),
                source_url=project.get("source_url"),
            )
        ]

    changes: list[DetectedChange] = []

    def emit(field_name: str, kind: str, summary: str, previous: Any, current_value: Any) -> None:
        changes.append(
            DetectedChange(
                project_id=project_id,
                field_name=field_name,
                change_kind=kind,
                summary=summary,
                previous_value=previous,
                current_value=current_value,
                source_name=project.get("source_name"),
                source_url=project.get("source_url"),
            )
        )

    for name, label in TRACKED_FIELDS:
        before = _normalise(previous_values.get(name))
        after = current.get(name)
        if before == after:
            continue

        if name == "mechanical_evidence_tier":
            gained = after is not None and (before is None or int(after) < int(before))
            if gained:
                emit(
                    name, MECHANICAL_ADDED,
                    f"Mechanical evidence added. Previous: {_display(before)}. "
                    f"Current: {_display(after)}.",
                    before, after,
                )
            else:
                emit(
                    name, EVIDENCE_UPDATED,
                    f"Mechanical evidence changed. Previous: {_display(before)}. "
                    f"Current: {_display(after)}.",
                    before, after,
                )
        elif name == "procurement_status":
            kind = CLOSED if after == "Closed" else PROCUREMENT_CHANGED
            emit(
                name, kind,
                f"Procurement status changed. Previous: {_display(before)}. "
                f"Current: {_display(after)}.",
                before, after,
            )
        elif name == "project_status":
            emit(
                name, STATUS_CHANGED,
                f"Project status changed. Previous: {_display(before)}. "
                f"Current: {_display(after)}.",
                before, after,
            )
        elif name == "classification":
            emit(
                name, CLASSIFICATION_CHANGED,
                f"Classification changed. Previous: {_display(before)}. "
                f"Current: {_display(after)}.",
                before, after,
            )
        elif name in ("estimated_project_value", "square_footage"):
            emit(
                name, VALUE_CHANGED,
                f"{label.capitalize()} changed. Previous: {_display(before)}. "
                f"Current: {_display(after)}.",
                before, after,
            )
        else:
            emit(
                name, EVIDENCE_UPDATED,
                f"{label.capitalize()} changed. Previous: {_display(before)}. "
                f"Current: {_display(after)}.",
                before, after,
            )

    if permits is not None:
        added = _permits_added(previous_values.get("_permit_keys") or [], current.get("_permit_keys") or [])
        for key in added:
            record = next(
                (p for p in permits
                 if str(p.get("natural_key") or p.get("permit_number") or "") == key),
                {},
            )
            changes.append(
                DetectedChange(
                    project_id=project_id,
                    field_name="__permits__",
                    change_kind=NEW_PERMIT,
                    summary=f"New permit recorded: {record.get('permit_number') or key}.",
                    current_value=record.get("permit_number") or key,
                    source_id=record.get("source_id"),
                    source_name=record.get("source_display_name") or project.get("source_name"),
                    source_url=record.get("source_url"),
                )
            )

    return changes


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
