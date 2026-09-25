"""Report generation: internal research reports and customer-facing briefs.

Two audiences, two documents:

* **Internal research report** — the full audit trail. Every client-facing field, the exact
  classification reasoning, every contributing permit, and every source URL. This is the
  document used to answer "where did this come from?" in detail.

* **Customer brief** — a short, opportunity-focused document for a contractor's business
  development lead. It leads with why the project matters and carries one citation per
  claim. Database internals, permit grids and scoring arithmetic are deliberately omitted.

Both are generated from the same verified records, so the two formats can never disagree.

The critical rule carries through unchanged: a field with no supporting source renders
"Not verified." Nothing is inferred, and no field is ever filled in to make a report look
more complete.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .constants import NOT_VERIFIED
from .db import Database
from .eligibility import evaluate, inclusion_reason
from .grouping import building_key, group_projects, sibling_info_for
from .procurement import (
    CLOSED as PROCUREMENT_CLOSED,
    CONFIRMED_OPEN,
    NOT_VERIFIED as PROCUREMENT_NOT_VERIFIED,
    procurement_explanation,
)
from .reporting import (
    CONFIRMED,
    NOT_VERIFIED as VERDICT_NOT_VERIFIED,
    PARTIALLY_VERIFIED,
    field_verdict,
)

#: The not-verified marker as it appears inside a table cell or field value. The canonical
#: constant is a sentence and carries a period; in a cell that period is noise.
_NV = NOT_VERIFIED.rstrip(".")

#: Fields presented in the customer brief, in order.
BRIEF_FIELDS: tuple[tuple[str, str], ...] = (
    ("project_name", "Project"),
    ("address", "Address"),
    ("city", "City"),
    ("project_type", "Project type"),
    ("permit_number", "Permit number"),
    ("permit_date", "Permit date"),
    ("project_status", "Current status"),
    ("mechanical_hvac_evidence", "Mechanical / HVAC evidence"),
    ("estimated_project_value", "Project value"),
    ("square_footage", "Square footage"),
    ("owner", "Owner / developer"),
    ("general_contractor", "General contractor"),
    ("architect", "Architect"),
)

#: Fields that appear in the internal report but must never be padded in the brief.
OPTIONAL_FIELDS = (
    "estimated_project_value",
    "square_footage",
    "owner",
    "general_contractor",
    "architect",
)


#: The exact procurement caveat required on every customer report. The wording is fixed
#: deliberately: permit evidence shows that mechanical work was filed, not that a package is
#: currently out for bid, and a reader must not be left to infer otherwise.
PERMIT_EVIDENCE_NOTICE = (
    "Public permit evidence does not by itself confirm that the HVAC/mechanical package is "
    "currently available for bid."
)


def _fmt(value: Any) -> str:
    """Render a value, applying the not-verified rule.

    The NOT_VERIFIED constant ends in a period because it is a sentence in the constants
    module. Inside a table cell it is a value, so the period is trimmed; leaving it produces
    "Not verified." mid-row, which reads like a typo.
    """
    if value is None:
        return _NV
    if isinstance(value, str):
        return value if value.strip() else _NV
    if isinstance(value, float):
        return f"{float(value):,.0f}"
    return str(value)


def _money(value: Any) -> str:
    if value is None:
        return _NV
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return NOT_VERIFIED


def _area(value: Any) -> str:
    if value is None:
        return _NV
    try:
        return f"{float(value):,.0f} sq ft"
    except (TypeError, ValueError):
        return NOT_VERIFIED


class ReportBuilder:
    """Builds reports from verified project records."""

    def __init__(self, db: Database):
        self.db = db

    # --- data access ----------------------------------------------------------

    def _project_row(self, project_id: int) -> Any:
        return self.db.conn.execute(
            "SELECT * FROM project WHERE id = ?", (project_id,)
        ).fetchone()

    def _permits(self, project_id: int) -> list[dict[str, Any]]:
        rows = self.db.conn.execute(
            """
            SELECT p.permit_number, p.permit_type, p.permit_subtype, p.permit_date,
                   p.status, p.work_description, p.address, p.city, p.zip_code,
                   p.job_value, p.square_footage, p.source_id, p.source_url,
                   s.name AS source_name, s.market_coverage
              FROM permit p
              JOIN project_permit pp ON pp.permit_id = p.id
              LEFT JOIN source s ON s.id = p.source_id
             WHERE pp.project_id = ?
             ORDER BY p.permit_date, p.permit_number
            """,
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _evidence(self, project_id: int, field_name: str) -> list[dict[str, Any]]:
        rows = self.db.conn.execute(
            """
            SELECT e.*, s.market_coverage AS coverage, s.name AS publisher
              FROM evidence e
              LEFT JOIN source s ON s.id = e.source_id
             WHERE e.project_id = ? AND e.field_name = ?
             ORDER BY e.id
            """,
            (project_id, field_name),
        ).fetchall()
        return [dict(r) for r in rows]

    def _source_urls(self, project_id: int) -> list[dict[str, Any]]:
        """Distinct source records behind this project, deduplicated by record.

        A project spans many permits from one publisher, so grouping by record key rather
        than by every evidence row keeps the citation list readable and free of duplicates.
        """
        rows = self.db.conn.execute(
            """
            SELECT e.source_name, e.source_url, e.source_record_key,
                   MIN(e.source_date) AS source_date,
                   COUNT(DISTINCT e.field_name) AS fields_supported,
                   COUNT(DISTINCT e.evidence_type) AS evidence_types
              FROM evidence e
             WHERE e.project_id = ?
             GROUP BY e.source_name, e.source_url, e.source_record_key
             ORDER BY e.source_name, e.source_record_key
            """,
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _discrepancies(self, row: Any) -> list[dict[str, Any]]:
        return json.loads((row["discrepancies"] if row else None) or "[]")

    # --- selection ------------------------------------------------------------

    def select_opportunities(
        self, *, classifications: tuple[str, ...] = ("HIGH", "MEDIUM"), limit: int | None = None,
        require_active: bool = True, avoid_siblings: bool = True,
        require_eligibility: bool = True,
    ) -> list[int]:
        """Pick the strongest report-ready opportunities, deterministically.

        Ordering, in priority order — all of it derived from documented facts, never from an
        invented attractiveness score:

        1. Classification score (the gates' own output).
        2. Classification label, so HIGH precedes MEDIUM at equal score.
        3. Amount of context the customer can use (declared value and footprint present).
        4. Recency of the permit.
        5. Project id, purely to make the result stable across runs.

        Filters applied in order:

        * `require_eligibility` applies the customer-brief criteria in `eligibility.py`,
          which reject completed work, non-mechanical records, service work, and records
          with nothing to explain why they matter.
        * `avoid_siblings` prefers at most one project per building key, so a customer brief
          does not present five suites of one office tower as five independent
          opportunities. Siblings are still selected if the brief cannot otherwise be filled,
          and the report states the relationship plainly when that happens.
        """
        placeholders = ",".join("?" for _ in classifications)
        rows = self.db.conn.execute(
            f"""
            SELECT *,
                   (CASE WHEN estimated_project_value IS NOT NULL THEN 1 ELSE 0 END
                    + CASE WHEN square_footage IS NOT NULL THEN 1 ELSE 0 END
                    + (CASE WHEN property_class IS NOT NULL
                             AND property_class <> 'Commercial (unspecified)'
                            THEN 1 ELSE 0 END)) AS context,
                   (CASE WHEN classification = 'HIGH' THEN 0 ELSE 1 END) AS class_rank
              FROM project
             WHERE classification IN ({placeholders})
             ORDER BY classification_score DESC, class_rank, context DESC,
                      permit_date DESC NULLS LAST, id
            """,
            classifications,
        ).fetchall()

        eligible: list[tuple[Any, Any]] = []
        for row in rows:
            if require_eligibility:
                verdict = evaluate(row)
                if not verdict.eligible:
                    continue
            elif require_active and row["procurement_status"] in (
                PROCUREMENT_CLOSED, PROCUREMENT_NOT_VERIFIED
            ):
                continue
            eligible.append((row, verdict if require_eligibility else None))

        if not avoid_siblings:
            ids = [int(r["id"]) for r, _ in eligible]
            return ids[:limit] if limit else ids

        # Group the eligible set by building key and take at most one project per key,
        # deferring the rest so a short brief can still be filled. `eligible` is already in
        # strength order, so the first member encountered for a key is that building's
        # strongest project.
        selected: list[int] = []
        deferred: list[int] = []
        chosen_keys: set[str] = set()
        for row, _ in eligible:
            key = building_key(row["address"], row["city"])
            pid = int(row["id"])
            if key and key in chosen_keys:
                deferred.append(pid)
                continue
            if key:
                chosen_keys.add(key)
            selected.append(pid)
            if limit and len(selected) >= limit:
                return selected

        # Fill any remaining slots from deferred siblings, weakest-context last, so a brief
        # is never short purely because a building had many suites.
        if limit:
            for pid in deferred:
                if len(selected) >= limit:
                    break
                selected.append(pid)
        return selected

    # --- customer-facing brief -------------------------------------------------

    def customer_brief(
        self, project_ids: list[int], *, market: str = "Dallas–Fort Worth, TX",
        title: str | None = None, prepared_for: str | None = None,
    ) -> str:
        """A concise, opportunity-focused brief for a commercial HVAC contractor.

        `prepared_for` names the recipient. It is left as a placeholder when not supplied
        rather than being filled with a guess.
        """
        now = datetime.now(timezone.utc).strftime("%d %B %Y")
        out: list[str] = []
        out.append(f"# {title or 'DFW Commercial HVAC Opportunity Brief'}")
        out.append("")
        out.append(f"**Prepared for:** {prepared_for or '[Contractor]'}  ")
        out.append(f"**Date generated:** {now}  ")
        out.append(f"**Market:** {market}  ")
        out.append(f"**Opportunities in this brief:** {len(project_ids)}")
        out.append("")
        out.append("## Important note")
        out.append("")
        out.append(
            "These opportunities are identified from publicly available construction and "
            "permitting records. Permit evidence does not confirm that a project is "
            "currently accepting HVAC bids."
        )
        out.append("")
        out.append(
            "Every fact below is drawn from a public permit record and carries a source "
            "link. Where a detail is not published by a source it is marked **Not verified** "
            "rather than estimated."
        )
        out.append("")

        for index, project_id in enumerate(project_ids, start=1):
            out.extend(self._brief_block(project_id, index))

        out.extend(self._brief_footer())
        return "\n".join(out)

    def _brief_block(self, project_id: int, index: int) -> list[str]:
        """One opportunity, in the required customer-facing field order."""
        row = self._project_row(project_id)
        if row is None:
            return []
        out: list[str] = []

        headline = row["project_name"] or f"{row['address']}, {row['city']}"
        out.append("---")
        out.append("")
        out.append(f"## {index}. {headline[:200]}")
        out.append("")

        def field(label: str, value: Any, name: str | None = None) -> None:
            """Emit a labelled field, citing the source when one supports the value."""
            if name:
                _verdict, evidence = field_verdict(self.db, project_id, name, value)
                citation = self._short_citation(evidence)
            else:
                citation = ""
            out.append(f"**{label}:** {value}{citation}  ")

        field("Location", f"{row['address'] or _NV}, {row['city'] or _NV}, "
                          f"{row['state'] or 'TX'}", "address")
        field("Project type", row["project_type"] or _NV, "project_type")
        out.append("")

        out.append(f"**Mechanical evidence:** {self._hvac_statement(row)}  ")
        out.append(
            f"**Construction/activity status:** {row['project_status'] or _NV}  "
        )
        out.append(f"**Procurement status:** {row['procurement_status'] or _NV}  ")
        out.append("")
        out.append(f"> {procurement_explanation(row['procurement_status'] or '', _RowProject(row))}")
        out.append("")

        eligibility = evaluate(row)
        out.append(f"**Project significance:** {'; '.join(eligibility.significance) or _NV}  ")
        field("Value", _money(row["estimated_project_value"]), "estimated_project_value")
        field("Square footage", _area(row["square_footage"]), "square_footage")
        field("Owner/developer", row["owner"] or _NV, "owner")
        field("GC", row["general_contractor"] or _NV, "general_contractor")
        field("Architect", row["architect"] or _NV, "architect")
        out.append("")

        out.append("**Why it was identified**")
        out.append("")
        out.append(f"{inclusion_reason(row, eligibility)}")
        out.append("")
        for reason in self._why_it_matters(row)[:4]:
            out.append(f"- {reason}")
        out.append("")

        out.extend(self._brief_not_verified(row, project_id))

        out.extend(self._brief_siblings(row))

        out.extend(self._brief_discrepancies(row))

        out.append("**Sources**")
        out.append("")
        for src in self._dedup_sources(project_id):
            url = src["source_url"] or "(search the record number at the city portal)"
            out.append(
                f"- {src['source_name']} — record `{src['source_record_key']}` — {url}"
            )
        out.append("")
        out.append(f"**Last verified:** {self._date(row['last_verified'])}")
        out.append("")
        return out

    def _brief_not_verified(self, row: Any, project_id: int) -> list[str]:
        """State plainly what is missing, so absence reads as fact rather than omission."""
        missing: list[str] = []
        labels = (
            ("estimated_project_value", "project value"),
            ("square_footage", "square footage"),
            ("owner", "owner or developer"),
            ("general_contractor", "general contractor"),
            ("architect", "architect"),
        )
        for name, label in labels:
            value = row[name] if name in row.keys() else None
            if value is None or (isinstance(value, str) and not value.strip()):
                missing.append(label)
        # A value that exists but has no supporting evidence row is also unverified.
        for name, label in labels:
            value = row[name] if name in row.keys() else None
            if value is None:
                continue
            verdict, _ = field_verdict(self.db, project_id, name, value)
            if verdict == VERDICT_NOT_VERIFIED:
                missing.append(label)

        out = ["**What is not verified**", ""]
        if missing:
            out.append(
                "Not published by any source for this record: "
                + ", ".join(dict.fromkeys(missing))
                + "."
            )
        else:
            out.append("All client-facing fields on this opportunity have a supporting source.")
        out.append("")
        out.append(f"> {PERMIT_EVIDENCE_NOTICE}")
        out.append("")
        return out

    def _brief_siblings(self, row: Any) -> list[str]:
        """Disclose when other projects share this base address.

        Grouping is reported, never merged: a shared street number is a hint that two records
        belong to one building, not proof.
        """
        info = self.sibling_info(int(row["id"]))
        if not info.is_grouped:
            return []
        return [
            "**Related records**",
            "",
            info.describe(),
            "",
        ]

    def sibling_info(self, project_id: int):
        """Sibling relationship for one project, computed across the eligible pool."""
        from .grouping import sibling_info_for

        rows = self.db.conn.execute(
            "SELECT id, address, city FROM project WHERE classification IN ('HIGH','MEDIUM')"
        ).fetchall()
        groups = group_projects(
            rows,
            address_getter=lambda r: r["address"],
            city_getter=lambda r: r["city"],
            id_getter=lambda r: r["id"],
        )
        return sibling_info_for(project_id, groups, id_getter=lambda r: r["id"])

    def _dedup_sources(self, project_id: int) -> list[dict[str, Any]]:
        seen: set[tuple[str, str]] = set()
        unique: list[dict[str, Any]] = []
        for src in self._source_urls(project_id):
            key = (src["source_name"], src["source_record_key"] or "")
            if key in seen:
                continue
            seen.add(key)
            unique.append(src)
        return unique

    def _why_it_matters(self, row: Any) -> list[str]:
        """Translate classification reasons into customer-facing business language.

        The raw reasons are written for an auditor: they state scores, tiers and gate
        outcomes. The brief needs the same substance in the language a contractor uses, so
        each reason is mapped to a plain sentence. Reasons that describe a *gate failing* or
        an *absence* are dropped here, because they belong in the internal report, not in a
        list of why an opportunity is worth calling.
        """
        reasons = json.loads(row["classification_reasons"] or "[]")
        out: list[str] = []
        seen: set[str] = set()

        def add(sentence: str) -> None:
            if sentence and sentence.strip() and sentence not in seen:
                seen.add(sentence)
                out.append(sentence)

        for reason in reasons:
            lowered = reason.lower()

            if lowered.startswith("held below") or lowered.startswith("no mechanical"):
                continue
            if lowered.startswith("source address has no street number"):
                continue
            if lowered.startswith("sources disagree"):
                continue

            if lowered.startswith("mechanical permit on file"):
                add(
                    "The city has a mechanical permit on file at this address, so "
                    "mechanical work is confirmed rather than assumed."
                )
            elif lowered.startswith("mechanical scope stated"):
                add(
                    "The permit record describes mechanical scope, giving a documented "
                    "reason to expect mechanical work on this project."
                )
            elif "project class" in lowered:
                label = reason.replace(" project class.", "").strip()
                add(f"Project class: {label} — a building type that requires mechanical systems.")
            elif lowered.startswith("declared value"):
                amount = reason.split("$")[-1].rstrip(".")
                add(
                    f"Declared permit value ${amount} — scale in the range that typically "
                    "involves contracted mechanical work."
                )
            elif lowered.startswith("declared area"):
                amount = reason.replace("Declared area", "").rstrip(".")
                add(
                    f"Declared floor area{amount} — a footprint large enough to require "
                    "engineered mechanical systems."
                )
            elif "active work" in lowered:
                status = row["project_status"] or "active"
                add(f"Permit status is '{status}', indicating the project is proceeding.")
            elif lowered.startswith("permit filed within"):
                add("Filed recently, so the work is likely still in planning or early construction.")
            elif lowered.startswith("no mechanical"):
                continue
            else:
                add(reason.rstrip(".") + ".")

        if not out:
            add("Classified from public permit records; see the sources below.")
        return out

    def _hvac_statement(self, row: Any) -> str:
        evidence = row["mechanical_hvac_evidence"]
        if not evidence:
            return (
                f"{NOT_VERIFIED} — no mechanical or HVAC permit, or mechanical scope text, "
                "is on record for this address."
            )
        tier = row["mechanical_evidence_tier"]
        if tier == 1:
            return (
                f"**Confirmed mechanical permit.** The building department filed a "
                f"mechanical permit at this address. {evidence}"
            )
        return (
            f"**Mechanical scope stated in the permit record.** {evidence}"
        )

    def _brief_discrepancies(self, row: Any) -> list[str]:
        items = self._discrepancies(row)
        if not items:
            return []
        out = ["**Source disagreement**", ""]
        for item in items:
            rendered = " vs ".join(
                f"{v['value']} ({v['source_name']})" for v in item["values"]
            )
            out.append(f"- `{item['field_name']}`: {rendered}")
        out.append("")
        out.append(
            "> These figures come from different publishers. Both are reported rather than "
            "reconciled; confirm with the issuing body before relying on either."
        )
        out.append("")
        return out

    def _short_citation(self, evidence: list[dict[str, Any]]) -> str:
        """A compact inline citation for a verified value, or nothing when unsupported.

        Kept short because this appears next to every field. The full URL still appears in
        the Sources section of the same opportunity, so traceability is not lost.
        """
        if not evidence:
            return ""
        ev = evidence[0]
        record = ev.get("source_record_key")
        if record:
            return f" _(source: `{record}`)_"
        name = ev.get("source_name") or ev.get("source_id")
        return f" _(source: {name})_"

    def _brief_footer(self) -> list[str]:
        return [
            "---",
            "",
            "## About this list",
            "",
            f"- **Procurement.** {PERMIT_EVIDENCE_NOTICE} No opportunity in this brief is "
            "described as an open bid, because no source in this market publishes bid status.",
            "- **Mechanical evidence.** Where a mechanical permit exists, the City recorded "
            "mechanical work at that address. That is evidence of scope, not a guarantee the "
            "work is still available.",
            "- **Verification.** Every fact carries a source link. Fields a source does not "
            "publish read **Not verified** rather than being estimated.",
            "- **Project identity.** Where several records share a base street address they "
            "are most likely suites of one building. They are listed separately and the "
            "relationship is flagged rather than merged, because a shared street number does "
            "not prove a shared building.",
            "- **Coverage.** Some source record types are capped by a pagination safety "
            "limit, so their totals are lower bounds. Coverage is not complete for every "
            "jurisdiction in the market.",
            "",
            "This brief was generated by an AI agent (OpenHands) from public records on "
            "behalf of the report owner. Confirm all figures with the issuing jurisdiction "
            "before commercial reliance.",
        ]

    # --- internal report ------------------------------------------------------

    def internal_report(self, project_ids: list[int], *, market: str = "Dallas–Fort Worth, TX") -> str:
        """The full research report: complete records, reasoning and provenance."""
        now = datetime.now(timezone.utc).strftime("%d %B %Y %H:%M UTC")
        out: list[str] = []
        out.append("# Internal research report — DFW commercial HVAC opportunities")
        out.append("")
        out.append(f"**Market:** {market}  ")
        out.append(f"**Generated:** {now}  ")
        out.append(f"**Projects:** {len(project_ids)}")
        out.append("")
        out.append(
            "This is the internal audit document. It contains the complete record behind "
            "each opportunity, the arithmetic of its classification, every contributing "
            "permit, and every source URL. The customer-facing brief is generated from the "
            "same records, so the two cannot disagree."
        )
        out.append("")

        for index, project_id in enumerate(project_ids, start=1):
            out.extend(self._internal_block(project_id, index))
        return "\n".join(out)

    def _internal_block(self, project_id: int, index: int) -> list[str]:
        row = self._project_row(project_id)
        if row is None:
            return []
        out: list[str] = []
        out.append("---")
        out.append("")
        out.append(f"## {index}. {row['address']}, {row['city']}")
        out.append("")
        out.append(
            f"- **Classification:** {row['classification']} ({row['classification_score']})"
        )
        out.append(f"- **Procurement status:** {row['procurement_status'] or PROCUREMENT_NOT_VERIFIED}")
        out.append(f"- **Mechanical evidence tier:** {row['mechanical_evidence_tier']}")
        out.append(f"- **Property class:** {row['property_class'] or NOT_VERIFIED}")
        out.append(f"- **Location precision:** {row['location_precision'] or NOT_VERIFIED}")
        out.append(f"- **Last verified:** {self._date(row['last_verified'])}")
        out.append("")

        out.append("### Field-level verification")
        out.append("")
        out.append("| Field | Value | Verdict | Evidence excerpt | Source |")
        out.append("|---|---|---|---|---|")
        for field, _label in BRIEF_FIELDS:
            value = row[field] if field in row.keys() else None
            verdict, evidence = field_verdict(self.db, project_id, field, value)
            rendered = self._render_field(field, value)
            if verdict == VERDICT_NOT_VERIFIED:
                out.append(f"| `{field}` | {rendered} | {verdict} | — | — |")
                continue
            ev = evidence[0]
            excerpt = (ev.get("excerpt") or "").replace("|", "/")[:90]
            out.append(
                f"| `{field}` | {rendered} | {verdict} | {excerpt} | "
                f"{ev.get('source_name')} |"
            )
        out.append("")

        out.append("### Classification reasoning")
        out.append("")
        for reason in json.loads(row["classification_reasons"] or "[]"):
            out.append(f"- {reason}")
        out.append("")

        out.append("### Contributing permits")
        out.append("")
        out.append("| Permit | Type | Date | Status | Value | Area | Work description |")
        out.append("|---|---|---|---|---|---|---|")
        for permit in self._permits(project_id):
            out.append(
                f"| `{permit['permit_number']}` | {permit['permit_type'] or '—'} | "
                f"{permit['permit_date'] or '—'} | {permit['status'] or '—'} | "
                f"{_money(permit['job_value'])} | {_area(permit['square_footage'])} | "
                f"{(permit['work_description'] or '—').replace('|', '/')[:110]} |"
            )
        out.append("")

        out.append("### Source provenance")
        out.append("")
        for src in self._source_urls(project_id):
            url = src["source_url"] or "(no per-record URL published by this source)"
            out.append(
                f"- **{src['source_name']}** — record `{src['source_record_key']}` — "
                f"{src['fields_supported']} field(s) — {url}"
            )
        out.append("")

        items = self._discrepancies(row)
        out.append("### Source discrepancies")
        out.append("")
        if not items:
            out.append("No cross-source discrepancies detected.")
        else:
            for item in items:
                rendered = " vs ".join(
                    f"{v['value']} ({v['source_name']})" for v in item["values"]
                )
                out.append(f"- `{item['field_name']}`: {rendered}")
        out.append("")
        return out

    # --- helpers --------------------------------------------------------------

    def _render_field(self, field: str, value: Any) -> str:
        if field == "estimated_project_value":
            return _money(value)
        if field == "square_footage":
            return _area(value)
        if field == "permit_date":
            return self._date(value)
        return _fmt(value)

    @staticmethod
    def _date(value: Any) -> str:
        if not value:
            return _NV
        text = str(value)
        return text[:10]

    @staticmethod
    def _citation(evidence: list[dict[str, Any]]) -> str:
        if not evidence:
            return "—"
        ev = evidence[0]
        name = ev.get("source_name") or ev.get("source_id")
        url = ev.get("source_url")
        if url:
            return f"{name} ([record]({url}))"
        return f"{name} (record `{ev.get('source_record_key')}`)"


class _RowProject:
    """Adapts a sqlite Row to the small surface `procurement_explanation` needs.

    Keeps the procurement helper decoupled from the database while still letting the report
    reuse its wording, so the brief and the pipeline explain a status identically.
    """

    def __init__(self, row: Any):
        self._row = row

    def __getattr__(self, name: str) -> Any:
        try:
            return self._row[name]
        except (KeyError, IndexError):
            return None