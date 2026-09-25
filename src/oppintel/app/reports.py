"""Published reports.

Only reports intended for customers are served here. The internal research report, the
validation report and the data quality report are deliberately excluded: they contain the
audit trail, internal scoring language and operational detail that should not be public.

Everything published is generated from the verified project records at request time, so a
report cannot drift from the data it describes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ..db import Database
from ..report_generator import ReportBuilder
from .config import AppConfig


def published_reports(db: Database, cfg: AppConfig) -> list[dict[str, Any]]:
    """The public report catalogue, with bodies generated on demand.

    Each entry carries a `slug`, a title, a one-line summary and the rendered body. Bodies are
    generated from the current database rather than cached to disk, so a report always reflects
    the data as it stands and there is no stale copy that could contradict the opportunity pages.
    """
    builder = ReportBuilder(db)
    market = _market_label(db)
    reports: list[dict[str, Any]] = []

    # 1. The customer opportunity brief from the strongest eligible opportunities.
    brief_ids = builder.select_opportunities(limit=5)
    reports.append(
        {
            "slug": "dfw-hvac-opportunity-brief",
            "title": f"{market} Commercial HVAC Opportunity Brief",
            "summary": (
                "The five strongest currently verified commercial HVAC opportunities, each with "
                "its mechanical evidence and source links."
            ),
            "kind": "Opportunity brief",
            "opportunity_count": len(brief_ids),
            "body": builder.customer_brief(
                brief_ids, market=f"{market}, TX", prepared_for="Commercial HVAC contractors"
            ),
        }
    )

    # 2. A market summary built from real counts.
    stats = _market_stats(db)
    reports.append(
        {
            "slug": "dfw-market-summary",
            "title": f"{market} Commercial Construction Market Summary",
            "summary": (
                "Current counts of commercial construction activity and mechanical evidence "
                "across the market."
            ),
            "kind": "Market summary",
            "opportunity_count": stats["mechanical"],
            "body": _market_summary_body(db, market, stats),
        }
    )

    return reports


def _market_label(db: Database) -> str:
    from ..config import active_market

    return active_market().short_name


def _market_stats(db: Database) -> dict[str, int]:
    """Headline counts for the public report catalogue.

    The evidence predicates come from the active trade profile, so these figures follow the
    configured trade rather than a column name written into this module.
    """
    from ..config import active_trade

    trade = active_trade()
    evidence_clause, evidence_params = trade.evidence_clause("")
    strong_clause, strong_params = trade.strong_evidence_clause("")
    # Strip the leading alias dot that the helper adds for a qualified column.
    evidence = (evidence_clause or "").lstrip(".")
    strong = (strong_clause or "").lstrip(".")

    def scalar(sql: str, params: tuple = ()) -> int:
        return int(db.conn.execute(sql, params).fetchone()[0] or 0)

    public = (
        "classification IN ('HIGH','MEDIUM') "
        "AND procurement_status IN ('Confirmed open','Evidence found, status unclear')"
    )
    mechanical = (
        scalar(f"SELECT COUNT(*) FROM project WHERE {public} AND {evidence}", tuple(evidence_params))
        if evidence else 0
    )
    tier1 = (
        scalar(f"SELECT COUNT(*) FROM project WHERE {public} AND {strong}", tuple(strong_params))
        if strong else 0
    )
    return {
        "total": scalar("SELECT COUNT(*) FROM project"),
        "public": scalar(f"SELECT COUNT(*) FROM project WHERE {public}"),
        "mechanical": mechanical,
        "tier1": tier1,
        "high": scalar(f"SELECT COUNT(*) FROM project WHERE {public} AND classification='HIGH'"),
        "cities": scalar(f"SELECT COUNT(DISTINCT city) FROM project WHERE {public}"),
        "permits": scalar("SELECT COUNT(*) FROM permit"),
    }


def _market_summary_body(db: Database, market: str, stats: dict[str, int]) -> str:
    from ..config import active_trade

    trade = active_trade()
    """A market summary assembled from counts the database actually holds.

    Written as a template rather than generated prose, so no figure can be introduced that the
    database did not produce.
    """
    generated = datetime.now(timezone.utc).strftime("%d %B %Y")
    out: list[str] = []
    out.append(f"# {market} Commercial Construction Market Summary")
    out.append("")
    out.append(f"Generated {generated} from public City permit records.")
    out.append("")
    out.append("## Headline figures")
    out.append("")
    out.append(f"- Commercial projects identified: **{stats['public']:,}**")
    out.append(
        f"- With mechanical or HVAC evidence: **{stats['mechanical']:,}** "
        f"({stats['tier1']:,} have a mechanical permit filed)"
    )
    out.append(f"- Classified HIGH: **{stats['high']:,}**")
    out.append(f"- Cities covered: **{stats['cities']:,}**")
    out.append(f"- Permit records processed: **{stats['permits']:,}**")
    out.append("")
    out.append("## How to read these numbers")
    out.append("")
    out.append(
        "These are counts of stored public records, not estimates. A project appears here when "
        "its permits assemble into a commercial project with documented construction activity."
    )
    out.append("")
    out.append(
        "A mechanical permit on file shows that mechanical work was filed with the City at that "
        "address. It does not confirm that an HVAC package is currently available for bid, and "
        "no source in this market publishes bid status."
    )
    out.append("")

    out.append("## By city")
    out.append("")
    out.append("| City | Projects | With mechanical evidence |")
    out.append("|---|---|---|")
    # The evidence predicate is interpolated from the trade profile, so the column is not
    # written into this query. Its values are inlined because they appear inside an aggregate
    # expression, where a bound parameter cannot be used; they are validated integers.
    evidence_predicate = (trade.evidence_clause("")[0] or "0").lstrip(".")
    for value in trade.evidence_values:
        evidence_predicate = evidence_predicate.replace("?", str(int(value)), 1)
    for row in db.conn.execute(
        f"""
        SELECT city, COUNT(*) AS n,
               SUM(CASE WHEN {evidence_predicate} THEN 1 ELSE 0 END) AS mech
          FROM project
         WHERE classification IN ('HIGH','MEDIUM')
           AND procurement_status IN ('Confirmed open','Evidence found, status unclear')
           AND city IS NOT NULL
         GROUP BY city ORDER BY n DESC
        """
    ):
        out.append(f"| {row['city']} | {row['n']:,} | {row['mech']:,} |")
    out.append("")

    out.append("## By project type")
    out.append("")
    out.append("| Project type | Projects |")
    out.append("|---|---|")
    for row in db.conn.execute(
        """
        SELECT project_type, COUNT(*) AS n
          FROM project
         WHERE classification IN ('HIGH','MEDIUM')
           AND procurement_status IN ('Confirmed open','Evidence found, status unclear')
           AND project_type IS NOT NULL
         GROUP BY project_type ORDER BY n DESC LIMIT 12
        """
    ):
        out.append(f"| {row['project_type']} | {row['n']:,} |")
    out.append("")

    out.append("## Coverage and limitations")
    out.append("")
    out.append(
        "Coverage is not complete for every jurisdiction in the market. Some source record "
        "types are capped by a pagination safety limit, so their totals are lower bounds rather "
        "than complete counts. Dallas publishes no declared project value or floor area, and no "
        "source publishes bid status or trade contractor."
    )
    out.append("")
    return "\n".join(out)