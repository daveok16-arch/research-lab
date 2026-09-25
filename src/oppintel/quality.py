"""Data-quality detection.

The operations view needs to state what is wrong with the data, and the only honest way to do
that is to measure it. This module inspects stored permits and projects and records the
conditions it can demonstrate: a permit with no address, a date in the future, a project with
no mechanical evidence sitting in a mechanical directory, a source that returned nothing.

Nothing here repairs or imputes. A missing field is recorded as missing and stays missing in
the project record, which is what keeps "Not verified" meaningful rather than decorative.

The checks run at the end of an assembly pass and replace the previous pass's findings, so the
operations view describes the current dataset rather than accumulating stale complaints.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .db import Database

#: Issue severities, ordered worst first for display.
SEVERITY_HIGH = "HIGH"
SEVERITY_MEDIUM = "MEDIUM"
SEVERITY_LOW = "LOW"

#: A permit dated further into the future than this is almost certainly a data error rather
#: than a scheduled submission, and a contractor reading it would be misled.
FUTURE_DATE_TOLERANCE_DAYS = 365

#: Issue types, as a controlled vocabulary so the operations view and the tests agree.
MISSING_ADDRESS = "missing_address"
MISSING_PERMIT_DATE = "missing_permit_date"
FUTURE_PERMIT_DATE = "future_permit_date"
NON_POSITIVE_VALUE = "non_positive_value"
UNMATCHED_SOURCE = "unmatched_source"
PROJECT_WITHOUT_EVIDENCE = "project_without_evidence"
DISCOVERABLE_WITHOUT_EVIDENCE = "discoverable_without_evidence"


def detect_quality_issues(db: Database, permits: list[Any]) -> int:
    """Record data-quality issues for the current dataset. Returns the count written.

    The previous pass's issues are cleared first, because an issue that has been fixed should
    disappear from the report. The result is a snapshot of what is wrong now.
    """
    db.clear_quality_issues()
    count = 0
    today = date.today()
    horizon = today + timedelta(days=FUTURE_DATE_TOLERANCE_DAYS)

    for permit in permits:
        source_id = getattr(permit, "source_id", None)
        number = getattr(permit, "permit_number", None) or getattr(permit, "natural_key", "")
        if not getattr(permit, "address", None):
            db.record_quality_issue(
                MISSING_ADDRESS, SEVERITY_MEDIUM,
                f"Permit {number} has no address, so it cannot be placed in a market.",
                source_id=source_id,
            )
            count += 1
        if getattr(permit, "permit_date", None) is None:
            db.record_quality_issue(
                MISSING_PERMIT_DATE, SEVERITY_LOW,
                f"Permit {number} has no permit date. Freshness cannot be established for it.",
                source_id=source_id,
            )
            count += 1
        elif permit.permit_date > horizon:
            db.record_quality_issue(
                FUTURE_PERMIT_DATE, SEVERITY_MEDIUM,
                f"Permit {number} is dated {permit.permit_date}, which is beyond a plausible "
                f"submission window.",
                source_id=source_id,
            )
            count += 1
        value = getattr(permit, "job_value", None)
        if value is not None and value <= 0:
            db.record_quality_issue(
                NON_POSITIVE_VALUE, SEVERITY_MEDIUM,
                f"Permit {number} declares a value of {value}, which is not usable as a "
                f"project value.",
                source_id=source_id,
            )
            count += 1

    # Source-level checks, computed from the stored rows rather than the crawl counters.
    known = {r["id"] for r in db.conn.execute("SELECT id FROM source").fetchall()}
    for row in db.conn.execute(
        "SELECT DISTINCT source_id FROM permit WHERE source_id IS NOT NULL"
    ).fetchall():
        if row["source_id"] not in known:
            db.record_quality_issue(
                UNMATCHED_SOURCE, SEVERITY_HIGH,
                f"Permits reference source {row['source_id']!r}, which has no configuration "
                f"entry. Its provenance cannot be described.",
                source_id=row["source_id"],
            )
            count += 1

    # Project-level checks: a project with no evidence rows cannot support any claim.
    for row in db.conn.execute(
        """
        SELECT p.id, p.classification FROM project p
         WHERE NOT EXISTS (SELECT 1 FROM evidence e WHERE e.project_id = p.id)
        """
    ).fetchall():
        db.record_quality_issue(
            PROJECT_WITHOUT_EVIDENCE, SEVERITY_HIGH,
            f"Project {row['id']} holds no evidence rows, so none of its fields is citable.",
            project_id=int(row["id"]),
        )
        count += 1

    # The most serious product-level inconsistency: a project offered in a trade directory
    # without the evidence that directory requires. Detected here so it is visible rather
    # than silently trusted.
    from .config import active_trade

    trade = active_trade()
    field = (trade.discovery or {}).get("evidence_field")
    values = (trade.discovery or {}).get("evidence_values") or []
    if field and values and field.isidentifier():
        placeholders = ",".join("?" for _ in values)
        rows = db.conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM project
             WHERE classification IN ('HIGH', 'MEDIUM')
               AND procurement_status IN ('Confirmed open', 'Evidence found, status unclear')
               AND ({field} IS NULL OR {field} NOT IN ({placeholders}))
            """,
            list(values),
        ).fetchone()
        missing = int(rows["n"] or 0)
        if missing:
            db.record_quality_issue(
                DISCOVERABLE_WITHOUT_EVIDENCE, SEVERITY_HIGH,
                f"{missing} discoverable project(s) carry no evidence for the active trade's "
                f"configured evidence field. The directory filter should have excluded them.",
            )
            count += 1

    db.conn.commit()
    return count
