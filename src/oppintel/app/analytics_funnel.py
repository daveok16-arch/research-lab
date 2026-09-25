"""Organic acquisition funnel measurement.

The SEO phase is only assessable if the funnel it feeds is measurable. This module records the
stages from a landing page through to a pipeline action, using the existing `analytics_event`
table — no new storage, and no new category of data.

Privacy is a hard constraint, inherited from `record_analytics`:

* No IP address, no user agent, no referrer URL, no query string, no free-text payload.
* No link to a user account, so the table cannot become a record of who searched for what.
* A page is identified by a *kind* (category, market, city, trade, guide, project), not by the
  URL a visitor arrived on, because the URL can carry a search term.

The funnel stages mirror the product's actual path, so a report can answer "how many organic
visitors reached the directory and then created an account" from stored rows alone.
"""

from __future__ import annotations

from typing import Any

from ..db import Database
from .accounts import record_analytics

#: Landing page kinds. A small closed vocabulary so reporting is a group-by rather than a
#: parse of arbitrary strings.
LANDING_CATEGORY = "landing_category"
LANDING_MARKET = "landing_market"
LANDING_CITY = "landing_city"
LANDING_TRADE = "landing_trade"
LANDING_CITY_TRADE = "landing_city_trade"
LANDING_PROJECT_TYPE = "landing_project_type"
LANDING_GUIDE = "landing_guide"
LANDING_DIRECTORY = "landing_directory"

LANDING_KINDS = (
    LANDING_CATEGORY,
    LANDING_MARKET,
    LANDING_CITY,
    LANDING_TRADE,
    LANDING_CITY_TRADE,
    LANDING_PROJECT_TYPE,
    LANDING_GUIDE,
    LANDING_DIRECTORY,
)

#: The conversion funnel, in order. Each name is an event already recorded somewhere in the
#: application, so the funnel is a reading of existing data rather than a parallel pipeline.
FUNNEL_STAGES: tuple[tuple[str, str], ...] = (
    (LANDING_DIRECTORY, "Landed on the opportunity directory"),
    ("opportunity_viewed", "Opened an opportunity"),
    ("signup", "Created an account"),
    ("opportunity_saved", "Saved an opportunity"),
    ("opportunity_watched", "Started watching an opportunity"),
    ("pipeline_updated", "Moved an opportunity in the pipeline"),
)


def record_landing(
    db: Database, kind: str, *, market_id: str | None = None, trade_id: str | None = None
) -> None:
    """Record that a visitor viewed a landing page of a given kind.

    Deliberately does not record which page beyond its kind. A city page and a guide are
    distinguishable in aggregate, which is what reporting needs, without recording the exact
    URL a person arrived on.
    """
    if kind not in LANDING_KINDS:
        return
    record_analytics(db, kind, market_id=market_id, trade_id=trade_id)


def funnel_counts(db: Database, *, market_id: str | None = None) -> list[dict[str, Any]]:
    """Counts per funnel stage, for the internal SEO report.

    Scoped to the market when one is given, so a report describes the market it is about.
    """
    out: list[dict[str, Any]] = []
    for event_name, label in FUNNEL_STAGES:
        if market_id:
            row = db.conn.execute(
                "SELECT COUNT(*) AS n FROM analytics_event WHERE event_name = ? AND market_id = ?",
                (event_name, market_id),
            ).fetchone()
        else:
            row = db.conn.execute(
                "SELECT COUNT(*) AS n FROM analytics_event WHERE event_name = ?",
                (event_name,),
            ).fetchone()
        out.append({"event": event_name, "label": label, "count": int(row["n"] or 0)})
    return out


def landing_counts(db: Database) -> list[dict[str, Any]]:
    """Counts per landing page kind, so coverage of the funnel entry points is visible."""
    rows = db.conn.execute(
        f"""
        SELECT event_name, COUNT(*) AS n FROM analytics_event
         WHERE event_name IN ({','.join('?' for _ in LANDING_KINDS)})
         GROUP BY event_name ORDER BY n DESC
        """,
        list(LANDING_KINDS),
    ).fetchall()
    return [{"kind": r["event_name"], "count": int(r["n"] or 0)} for r in rows]
