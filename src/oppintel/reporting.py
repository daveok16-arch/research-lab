"""Validation and coverage reports.

The validation report exists to answer the question a paying customer will actually ask:
"where did you get this?" Every field is labelled with how strongly it is supported:

* CONFIRMED          a value exists and at least one current source substantiates it
* PARTIALLY VERIFIED a value exists but its only support is a historical source, or the
                     source is identified without a directly resolvable record URL
* NOT VERIFIED       no value could be substantiated, and none is asserted

Nothing is inferred. A blank field is reported as NOT VERIFIED rather than filled in.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .constants import NOT_VERIFIED, REQUIRED_PROJECT_FIELDS
from .db import Database
from .discrepancy import discrepancy_summary

CONFIRMED = "CONFIRMED"
PARTIALLY_VERIFIED = "PARTIALLY VERIFIED"
NOT_VERIFIED = "NOT VERIFIED"

#: The 11 fields the client report must present, in the order the customer asked for.
REPORT_FIELDS: tuple[tuple[str, str], ...] = (
    ("project_name", "PROJECT"),
    ("address", "LOCATION"),
    ("estimated_project_value", "VALUE"),
    ("square_footage", "SIZE"),
    ("project_type", "PROJECT TYPE"),
    ("owner", "OWNER"),
    ("general_contractor", "GENERAL CONTRACTOR"),
    ("architect", "ARCHITECT"),
    ("mechanical_hvac_evidence", "HVAC/MECHANICAL EVIDENCE"),
    ("project_status", "STATUS"),
)


def _fmt_row(row: Any, column: str) -> str:
    value = row[column] if column in row.keys() else None
    if value is None or (isinstance(value, str) and not value.strip()):
        return NOT_VERIFIED
    if column in ("estimated_project_value", "square_footage"):
        try:
            return f"{float(value):,.0f}"
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def field_verdict(db: Database, project_id: int, field_name: str, value: Any) -> tuple[str, list[dict]]:
    """Classify one field and return its supporting evidence rows.

    The evidence rows are checked against the value actually recorded on the project. A
    value with no matching evidence row is reported as NOT VERIFIED rather than trusted,
    because an unsupported value is exactly the failure mode this platform exists to
    prevent. Numeric fields are compared with the same relative tolerance used for
    discrepancy detection, since evidence stores the value as text.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return NOT_VERIFIED, []

    evidence = db.conn.execute(
        """
        SELECT e.*, s.market_coverage AS coverage
          FROM evidence e
          LEFT JOIN source s ON s.id = e.source_id
         WHERE e.project_id = ? AND e.field_name = ?
        """,
        (project_id, field_name),
    ).fetchall()
    if not evidence:
        return NOT_VERIFIED, []

    rows = [dict(e) for e in evidence]

    def matches(candidate: Any) -> bool:
        from .discrepancy import values_agree

        return values_agree(candidate, value)

    supporting = [r for r in rows if matches(r.get("value"))]
    if not supporting:
        # The field holds a value no evidence row supports.
        return NOT_VERIFIED, []

    current = [r for r in supporting if (r.get("coverage") or "") == "current"]
    if current:
        return CONFIRMED, supporting
    return PARTIALLY_VERIFIED, supporting


def validation_report(db: Database, project_ids: list[int]) -> str:
    """Full validation report for the given projects, in Markdown."""
    out: list[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out.append("# Opportunity validation report")
    out.append("")
    out.append(f"Generated {now}")
    out.append("")
    out.append(
        "Every field is labelled by the strength of its support. "
        f"`{CONFIRMED}` means a current public source states it. "
        f"`{PARTIALLY_VERIFIED}` means a real source states it but that source is historical. "
        f"`{NOT_VERIFIED}` means no public source could substantiate it, and no value is "
        "asserted in its place."
    )
    out.append("")

    for project_id in project_ids:
        row = db.conn.execute("SELECT * FROM project WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            continue
        out.extend(_validation_block(db, row))

    return "\n".join(out)


def _validation_block(db: Database, row: Any) -> list[str]:
    project_id = row["id"]
    out: list[str] = []
    out.append("---")
    out.append("")
    out.append(f"## {_fmt_row(row, 'project_name')}")
    out.append("")
    out.append(f"- **Classification**: {_fmt_row(row, 'classification')} "
               f"(score {_fmt_row(row, 'classification_score')})")
    out.append(f"- **Location**: {_fmt_row(row, 'address')}, {_fmt_row(row, 'city')}, "
               f"{_fmt_row(row, 'state')}")
    out.append(f"- **Permit number**: {_fmt_row(row, 'permit_number')}")
    out.append(f"- **Permit date**: {_fmt_row(row, 'permit_date')}")
    out.append(f"- **Last verified**: {_fmt_row(row, 'last_verified')}")
    out.append("")

    out.append("### Fields")
    out.append("")
    out.append("| Field | Value | Status | Source | Source date |")
    out.append("|---|---|---|---|---|")
    for field_name, _label in REPORT_FIELDS:
        value = row[field_name] if field_name in row.keys() else None
        verdict, evidence = field_verdict(db, project_id, field_name, value)
        if verdict == NOT_VERIFIED:
            out.append(f"| `{field_name}` | {NOT_VERIFIED} | {verdict} | — | — |")
            continue
        primary = evidence[0]
        url = primary.get("source_url") or "no direct URL (search by record number)"
        out.append(
            f"| `{field_name}` | {_fmt_row(row, field_name)} | {verdict} | "
            f"{primary.get('source_name')} | {primary.get('source_date') or '—'} |"
        )
    out.append("")

    out.append("### Classification reasons")
    out.append("")
    reasons = json.loads(row["classification_reasons"] or "[]")
    for reason in reasons:
        out.append(f"- {reason}")
    out.append("")

    out.append("### Source URLs")
    out.append("")
    evidence = db.conn.execute(
        """
        SELECT DISTINCT field_name, source_name, source_url, source_record_key,
               source_date, evidence_type, tier, excerpt
          FROM evidence WHERE project_id = ?
         ORDER BY field_name
        """,
        (project_id,),
    ).fetchall()
    seen_urls: set[str] = set()
    for e in evidence:
        url = e["source_url"] or "(no per-record URL published by this source)"
        if url in seen_urls:
            continue
        seen_urls.add(url)
        out.append(f"- {e['source_name']} — {url}")
    out.append("")

    out.append("### Source discrepancies")
    out.append("")
    out.append(discrepancy_summary_from_row(row))
    out.append("")

    out.append("### Verdict summary")
    out.append("")
    counts = {CONFIRMED: 0, PARTIALLY_VERIFIED: 0, NOT_VERIFIED: 0}
    for field_name, _label in REPORT_FIELDS:
        value = row[field_name] if field_name in row.keys() else None
        verdict, _ = field_verdict(db, project_id, field_name, value)
        counts[verdict] += 1
    out.append(
        f"- {CONFIRMED}: {counts[CONFIRMED]} of {len(REPORT_FIELDS)} client-report fields"
    )
    out.append(
        f"- {PARTIALLY_VERIFIED}: {counts[PARTIALLY_VERIFIED]} of {len(REPORT_FIELDS)} fields"
    )
    out.append(
        f"- {NOT_VERIFIED}: {counts[NOT_VERIFIED]} of {len(REPORT_FIELDS)} fields"
    )
    out.append("")
    return out


def discrepancy_summary_from_row(row: Any) -> str:
    raw = row["discrepancies"] if "discrepancies" in row.keys() else None
    items = json.loads(raw or "[]")
    if not items:
        return "No source discrepancies detected."
    lines = []
    for item in items:
        rendered = " vs ".join(
            f"{v['value']} ({v['source_name']})" for v in item["values"]
        )
        lines.append(f"- `{item['field_name']}`: {rendered}")
    return "\n".join(lines)


def coverage_report(db: Database) -> str:
    """Coverage table across every configured source: dates, volume, and evidence yield."""
    out: list[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out.append("# Source coverage report")
    out.append("")
    out.append(f"Generated {now}")
    out.append("")
    out.append(
        "| City | Source | Earliest Date | Latest Date | Retrieval Date | Records "
        "| Commercial Records | Mechanical Evidence | Pages | Notes |"
    )
    out.append("|---|---|---|---|---|---|---|---|---|---|")

    rows = db.conn.execute(
        """
        SELECT s.id, s.name, s.jurisdiction_city, s.market_coverage,
               c.earliest_date, c.latest_date, c.retrieval_date, c.record_count,
               c.commercial_count, c.mechanical_count, c.pagination_pages,
               c.pagination_notes, s.coverage_note
          FROM source s
          LEFT JOIN source_coverage c ON c.source_id = s.id
         ORDER BY COALESCE(c.record_count, 0) DESC, s.name
        """
    ).fetchall()

    for r in rows:
        city = r["jurisdiction_city"] or "Multi-city"
        notes = " ".join((r["pagination_notes"] or r["coverage_note"] or "").split())
        if len(notes) > 220:
            notes = notes[:217] + "..."
        out.append(
            f"| {city} | {r['name']} | {r['earliest_date'] or '—'} "
            f"| {r['latest_date'] or '—'} | {r['retrieval_date'] or '—'} "
            f"| {r['record_count'] or 0} | {r['commercial_count'] or 0} "
            f"| {r['mechanical_count'] or 0} | {r['pagination_pages'] or '—'} | {notes} |"
        )
    out.append("")

    out.append("## Projects by city")
    out.append("")
    out.append("| City | Projects | HIGH | MEDIUM | NEEDS_VERIFICATION | With mechanical evidence |")
    out.append("|---|---|---|---|---|---|")
    cities = db.conn.execute(
        """
        SELECT COALESCE(city,'(unknown)') AS city,
               COUNT(*) AS projects,
               SUM(CASE WHEN classification='HIGH' THEN 1 ELSE 0 END) AS high,
               SUM(CASE WHEN classification='MEDIUM' THEN 1 ELSE 0 END) AS medium,
               SUM(CASE WHEN classification='NEEDS_VERIFICATION' THEN 1 ELSE 0 END) AS needs,
               SUM(CASE WHEN mechanical_evidence_tier IS NOT NULL THEN 1 ELSE 0 END) AS mech
          FROM project GROUP BY city ORDER BY projects DESC
        """
    ).fetchall()
    for r in cities:
        out.append(
            f"| {r['city']} | {r['projects']} | {r['high']} | {r['medium']} | {r['needs']} "
            f"| {r['mech']} |"
        )
    out.append("")
    return "\n".join(out)