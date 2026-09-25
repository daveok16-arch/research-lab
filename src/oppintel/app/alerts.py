"""Alerts, generated from recorded events.

The rule this module exists to enforce is that an alert must have an underlying event. There
is no code path that creates an alert from a guess, a schedule, or a template: every alert
points at either a `project_change` row (a detected difference in the data) or at the moment a
project first matched a user's saved criteria.

That gives the alerts list a property a customer can rely on: if the product says something
changed, the change is in the database with a before value, an after value and a source. If
nothing changed, there is nothing to alert about, and an empty inbox is the honest result.

Delivery is in-app only. Email is modelled in the schema (`email_sent_at`) and gated behind an
entitlement and a per-user preference, so a future mailer reads the same event rows rather than
needing a parallel notification system. Nothing here sends mail, and nothing claims to.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..db import Database

#: Alert kinds. Kept aligned with the change kinds the detector emits, plus the first-match
#: event, which is the only alert that does not come from a diff.
ALERT_NEW_MATCH = "new_match"
ALERT_NEW_PERMIT = "new_permit"
ALERT_MECHANICAL_ADDED = "mechanical_evidence_added"
ALERT_STATUS_CHANGED = "status_changed"
ALERT_PROCUREMENT_CHANGED = "procurement_changed"
ALERT_CLASSIFICATION_CHANGED = "classification_changed"
ALERT_CLOSED = "project_closed"

#: Which detected change kinds raise an alert. A routine correction is recorded on the
#: timeline but does not notify, so the inbox stays a signal rather than a feed.
CHANGE_TO_ALERT: dict[str, str] = {
    "new_permit": ALERT_NEW_PERMIT,
    "mechanical_evidence_added": ALERT_MECHANICAL_ADDED,
    "status_changed": ALERT_STATUS_CHANGED,
    "procurement_changed": ALERT_PROCUREMENT_CHANGED,
    "classification_changed": ALERT_CLASSIFICATION_CHANGED,
    "project_closed": ALERT_CLOSED,
}

ALERT_LABELS: dict[str, str] = {
    ALERT_NEW_MATCH: "New matching opportunity",
    ALERT_NEW_PERMIT: "New permit filed",
    ALERT_MECHANICAL_ADDED: "Mechanical evidence added",
    ALERT_STATUS_CHANGED: "Project status changed",
    ALERT_PROCUREMENT_CHANGED: "Procurement status changed",
    ALERT_CLASSIFICATION_CHANGED: "Classification changed",
    ALERT_CLOSED: "Project closed",
}


@dataclass
class AlertResult:
    created: int = 0
    scanned: int = 0
    skipped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"created": self.created, "scanned": self.scanned, "skipped": self.skipped}


class AlertService:
    """Turns recorded changes into per-user alerts."""

    def __init__(self, db: Database):
        self.db = db

    # --- generation -----------------------------------------------------------

    def generate_from_changes(self, *, limit: int = 1000) -> AlertResult:
        """Create alerts for watched projects from changes recorded since the last run.

        Only watched projects generate alerts. A save is a bookmark and carries no monitoring
        promise, so alerting a saved-but-unwatched project would be a claim the user never made.
        """
        result = AlertResult()
        rows = self.db.conn.execute(
            """
            SELECT c.id, c.project_id, c.change_kind, c.summary, c.detected_at,
                   w.user_id, COALESCE(w.last_seen_change_id, 0) AS last_seen
              FROM project_change c
              JOIN watched_opportunity w ON w.project_id = c.project_id
             ORDER BY c.id
             LIMIT ?
            """,
            (limit,),
        ).fetchall()

        for row in rows:
            change_id = int(row["id"])
            if change_id <= int(row["last_seen"] or 0):
                continue
            result.scanned += 1
            kind = CHANGE_TO_ALERT.get(row["change_kind"])
            if kind is None:
                result.skipped += 1
                self._advance_seen(int(row["user_id"]), int(row["project_id"]), change_id)
                continue
            user_id = int(row["user_id"])
            if not self._notifications_enabled(user_id):
                result.skipped += 1
                self._advance_seen(user_id, int(row["project_id"]), change_id)
                continue
            created = self._insert(
                user_id, int(row["project_id"]), kind, row["summary"], change_id=change_id
            )
            if created:
                result.created += 1
            self._advance_seen(user_id, int(row["project_id"]), change_id)

        self.db.conn.commit()
        return result

    def _advance_seen(self, user_id: int, project_id: int, change_id: int) -> None:
        self.db.conn.execute(
            """
            UPDATE watched_opportunity SET last_seen_change_id = ?
             WHERE user_id = ? AND project_id = ? AND COALESCE(last_seen_change_id, 0) < ?
            """,
            (change_id, user_id, project_id, change_id),
        )

    def record_new_match(self, user_id: int, project_id: int, title: str, body: str) -> bool:
        """Record that a project newly matches a user's criteria.

        Commits, because this is a public entry point that a caller may reach outside the
        generation pass. Without the commit the row would live only in the calling transaction
        and be discarded when the connection closed, turning a real match into a lost alert.

        Attempted at most once per user and project by the partial unique index, so asking
        twice does not produce two alerts. The body is composed by the caller from real match
        reasons, never a generic string, so the alert restates the same facts the card shows.
        """
        created = self._insert(
            user_id, project_id, ALERT_NEW_MATCH, body, change_id=None, title=title
        )
        self.db.conn.commit()
        return created

    def _insert(
        self, user_id: int, project_id: int, kind: str, body: str | None,
        *, change_id: int | None, title: str | None = None,
    ) -> bool:
        headline = title or ALERT_LABELS.get(kind, "Opportunity updated")
        cursor = self.db.conn.execute(
            """
            INSERT OR IGNORE INTO alert_event (user_id, project_id, change_id, kind, title,
                body, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, project_id, change_id, kind, headline, (body or None),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cursor.rowcount > 0

    def _notifications_enabled(self, user_id: int) -> bool:
        row = self.db.conn.execute(
            "SELECT notify_in_app FROM user_preference WHERE user_id = ?", (user_id,)
        ).fetchone()
        # Default to on when no preference row exists, matching how a new account starts.
        return True if row is None else bool(row["notify_in_app"])

    # --- reading --------------------------------------------------------------

    def for_user(
        self, user_id: int, *, unread_only: bool = False, limit: int = 100
    ) -> list[dict[str, Any]]:
        clause = " AND a.read_at IS NULL" if unread_only else ""
        rows = self.db.conn.execute(
            f"""
            SELECT a.*, s.slug, p.project_name, p.address, p.city,
                   c.previous_value, c.current_value, c.field_name, c.source_name, c.source_url
              FROM alert_event a
              JOIN project p ON p.id = a.project_id
              LEFT JOIN project_slug s ON s.project_id = a.project_id
              LEFT JOIN project_change c ON c.id = a.change_id
             WHERE a.user_id = ?{clause}
             ORDER BY a.id DESC
             LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def unread_count(self, user_id: int) -> int:
        return int(
            self.db.conn.execute(
                "SELECT COUNT(*) FROM alert_event WHERE user_id = ? AND read_at IS NULL",
                (user_id,),
            ).fetchone()[0]
        )

    def mark_read(self, user_id: int, alert_id: int) -> bool:
        """Mark one alert read. Scoped to the owner so an alert id cannot touch another's row."""
        cursor = self.db.conn.execute(
            "UPDATE alert_event SET read_at = ? WHERE id = ? AND user_id = ? AND read_at IS NULL",
            (datetime.now(timezone.utc).isoformat(), alert_id, user_id),
        )
        self.db.conn.commit()
        return cursor.rowcount > 0

    def mark_all_read(self, user_id: int) -> int:
        cursor = self.db.conn.execute(
            "UPDATE alert_event SET read_at = ? WHERE user_id = ? AND read_at IS NULL",
            (datetime.now(timezone.utc).isoformat(), user_id),
        )
        self.db.conn.commit()
        return cursor.rowcount

    # --- integrity ------------------------------------------------------------

    def alerts_without_event(self) -> list[dict[str, Any]]:
        """Alerts that reference neither a change row nor a first-match event.

        A nonzero result would mean an alert was fabricated. The operations view exposes this
        so the invariant is observable rather than merely asserted in a test.
        """
        rows = self.db.conn.execute(
            """
            SELECT a.* FROM alert_event a
             WHERE a.change_id IS NULL AND a.kind <> ?
            """,
            (ALERT_NEW_MATCH,),
        ).fetchall()
        return [dict(r) for r in rows]

    def summary(self) -> dict[str, Any]:
        scalar = lambda sql, params=(): int(self.db.conn.execute(sql, params).fetchone()[0])
        return {
            "total": scalar("SELECT COUNT(*) FROM alert_event"),
            "unread": scalar("SELECT COUNT(*) FROM alert_event WHERE read_at IS NULL"),
            "orphaned": len(self.alerts_without_event()),
            "by_kind": {
                r["kind"]: int(r["n"])
                for r in self.db.conn.execute(
                    "SELECT kind, COUNT(*) AS n FROM alert_event GROUP BY kind"
                ).fetchall()
            },
        }


def build_alerts(db: Database) -> dict[str, int]:
    """Run the alert pass over recorded changes. Convenience entry point for the CLI."""
    return AlertService(db).generate_from_changes().as_dict()
