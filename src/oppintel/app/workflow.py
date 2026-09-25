"""Opportunity workflow: watching, pipeline, notes, tags and activity.

This is the user's own working record over an opportunity. It is deliberately kept apart from
the intelligence layer in both storage and vocabulary:

* A **pipeline stage** is what the user is doing about a project. It says nothing about the
  project's procurement status, and the two are never rendered as if they were the same thing.
  "Target" means the account is pursuing it; it does not mean anyone is officially seeking bids.
* A **watch** is a request for the system to re-check the record for meaningful change. It is
  distinct from a **save**, which is a bookmark with no monitoring implied.
* A **note** or a **tag** is authored by the user and is never merged into the project record,
  so a user's private working notes can never be mistaken for a collected fact.

Every mutation also appends to `user_activity`, so an account can see what it did and when.
Storage references a project by id only; nothing here copies a project field, so a re-ingest
cannot leave a note or a stage attached to a stale snapshot of the project.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..db import Database

#: The user's workflow vocabulary. Ordered as the pipeline reads.
#:
#: The labels are chosen so that none of them can be read as a procurement state. "Closed" is
#: deliberately rendered as "Closed out": the procurement vocabulary already uses "Closed" for
#: a project the sources say is finished, and a single word carrying both meanings is exactly
#: how an account's own filing decision gets mistaken for a fact about the market.
PIPELINE_STAGES: tuple[tuple[str, str], ...] = (
    ("NEW", "New"),
    ("REVIEWING", "Reviewing"),
    ("WATCHING", "Watching"),
    ("TARGET", "Target"),
    ("CONTACTED", "Contacted"),
    ("PURSUING", "Pursuing"),
    ("CLOSED", "Closed out"),
)

STAGE_LABELS: dict[str, str] = dict(PIPELINE_STAGES)

#: The stage a project enters the pipeline at when a user acts on it without choosing one.
DEFAULT_STAGE = "NEW"

#: Stages that are the user's own outcome, not a claim about the project. Surfaced in the UI
#: so the distinction is stated rather than assumed.
TERMINAL_STAGES = ("CLOSED",)

MAX_NOTE_LENGTH = 4000
MAX_TAG_LENGTH = 40
MAX_TAGS_PER_PROJECT = 20

#: Tags are the user's own labels, so the rules are about display safety, not vocabulary.
_TAG_CLEAN = re.compile(r"[^A-Za-z0-9 +&/._-]+")


class WorkflowError(Exception):
    """A user-facing workflow problem with a safe message."""


def normalise_stage(stage: str | None) -> str:
    key = (stage or "").strip().upper()
    return key if key in STAGE_LABELS else DEFAULT_STAGE


def clean_tag(tag: str) -> str:
    """Trim and sanitise a user tag.

    Tags are rendered back to the user and into a filter, so control characters, angle
    brackets and leading/trailing punctuation are stripped rather than escaped at render time.
    """
    cleaned = _TAG_CLEAN.sub(" ", (tag or "").strip())
    cleaned = " ".join(cleaned.split())
    return cleaned[:MAX_TAG_LENGTH]


@dataclass
class PipelineCard:
    """One opportunity as the user's pipeline sees it."""

    project_id: int
    stage: str
    slug: str | None = None
    project: dict[str, Any] = field(default_factory=dict)
    follow_up_date: str | None = None
    assigned_to: int | None = None
    tags: list[str] = field(default_factory=list)
    note_count: int = 0
    updated_at: str | None = None


class WorkflowService:
    """The user's working record over opportunities."""

    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    # --- guards ---------------------------------------------------------------

    def _project_exists(self, project_id: int) -> bool:
        return bool(
            self.db.conn.execute(
                "SELECT 1 FROM project WHERE id = ?", (project_id,)
            ).fetchone()
        )

    def _log(self, user_id: int, project_id: int, action: str, detail: str | None = None) -> None:
        self.db.conn.execute(
            """
            INSERT INTO user_activity (user_id, project_id, action, detail, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, project_id, action, (detail or None), self._now()),
        )

    # --- organizations --------------------------------------------------------

    def org_ids_for(self, user_id: int) -> list[int]:
        rows = self.db.conn.execute(
            "SELECT org_id FROM organization_member WHERE user_id = ?", (user_id,)
        ).fetchall()
        return [int(r["org_id"]) for r in rows]

    def peers_for(self, user_id: int) -> list[dict[str, Any]]:
        """Other members of any organization this user belongs to.

        Membership is the only basis for one account seeing another's identity. A user with no
        organization has no peers, so a default account cannot reach anyone else.
        """
        org_ids = self.org_ids_for(user_id)
        if not org_ids:
            return []
        placeholders = ",".join("?" for _ in org_ids)
        rows = self.db.conn.execute(
            f"""
            SELECT u.id, u.display_name, u.email
              FROM organization_member m
              JOIN app_user u ON u.id = m.user_id
             WHERE m.org_id IN ({placeholders}) AND u.id <> ? AND u.is_active = 1
             ORDER BY u.display_name, u.email
            """,
            org_ids + [user_id],
        ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "display_name": r["display_name"] or r["email"].split("@")[0],
            }
            for r in rows
        ]

    def can_assign_to(self, user_id: int, assignee_id: int | None) -> bool:
        """Whether a user may assign work to another account.

        Only a member of the same organization may be assigned, so an arbitrary user id cannot
        be used to attach work to, or probe, another account.
        """
        if assignee_id is None:
            return True
        if assignee_id == user_id:
            return True
        return assignee_id in {peer["id"] for peer in self.peers_for(user_id)}

    # --- watching -------------------------------------------------------------

    def watch(self, user_id: int, project_id: int) -> bool:
        """Start monitoring a project. Returns True when newly watched.

        A watch also places the project in the user's pipeline if it is not already there and
        in a later stage, so the two views agree about what the account is working on. It never
        moves a project *backwards* in the pipeline.
        """
        if not self._project_exists(project_id):
            return False
        cursor = self.db.conn.execute(
            """
            INSERT OR IGNORE INTO watched_opportunity (user_id, project_id, watched_at)
            VALUES (?, ?, ?)
            """,
            (user_id, project_id, self._now()),
        )
        created = cursor.rowcount > 0
        if created:
            self._log(user_id, project_id, "watched", "Monitoring for changes.")
            self._ensure_pipeline_entry(user_id, project_id, "WATCHING")
        self.db.conn.commit()
        return created

    def unwatch(self, user_id: int, project_id: int) -> bool:
        cursor = self.db.conn.execute(
            "DELETE FROM watched_opportunity WHERE user_id = ? AND project_id = ?",
            (user_id, project_id),
        )
        if cursor.rowcount:
            self._log(user_id, project_id, "unwatched", "Stopped monitoring.")
        self.db.conn.commit()
        return cursor.rowcount > 0

    def is_watched(self, user_id: int | None, project_id: int) -> bool:
        if not user_id:
            return False
        return bool(
            self.db.conn.execute(
                "SELECT 1 FROM watched_opportunity WHERE user_id = ? AND project_id = ?",
                (user_id, project_id),
            ).fetchone()
        )

    def watched_ids(self, user_id: int) -> list[int]:
        rows = self.db.conn.execute(
            "SELECT project_id FROM watched_opportunity WHERE user_id = ? "
            "ORDER BY watched_at DESC",
            (user_id,),
        ).fetchall()
        return [int(r["project_id"]) for r in rows]

    # --- pipeline -------------------------------------------------------------

    def _ensure_pipeline_entry(self, user_id: int, project_id: int, stage: str) -> None:
        """Place a project in the pipeline without demoting it.

        SQLite's upsert would overwrite the stage, so the insert is conditional and the update
        only runs when the existing stage is the entry stage. That is what stops "Watch" from
        pulling a project out of "Pursuing".
        """
        now = self._now()
        self.db.conn.execute(
            """
            INSERT INTO pipeline_entry (user_id, project_id, stage, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, project_id) DO NOTHING
            """,
            (user_id, project_id, stage, now),
        )
        self.db.conn.execute(
            """
            UPDATE pipeline_entry SET stage = ?
             WHERE user_id = ? AND project_id = ? AND stage = ?
            """,
            (stage, user_id, project_id, DEFAULT_STAGE),
        )

    def set_stage(
        self, user_id: int, project_id: int, stage: str,
        *, follow_up_date: str | None = None,
    ) -> bool:
        """Move a project to a workflow stage. This is the user's state, not the market's."""
        if not self._project_exists(project_id):
            return False
        stage = normalise_stage(stage)
        now = self._now()
        self.db.conn.execute(
            """
            INSERT INTO pipeline_entry (user_id, project_id, stage, follow_up_date, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, project_id) DO UPDATE SET
                stage = excluded.stage,
                follow_up_date = COALESCE(excluded.follow_up_date, pipeline_entry.follow_up_date),
                updated_at = excluded.updated_at
            """,
            (user_id, project_id, stage, (follow_up_date or None), now),
        )
        self._log(user_id, project_id, "pipeline_stage", STAGE_LABELS[stage])
        self.db.conn.commit()
        return True

    def remove_from_pipeline(self, user_id: int, project_id: int) -> bool:
        cursor = self.db.conn.execute(
            "DELETE FROM pipeline_entry WHERE user_id = ? AND project_id = ?",
            (user_id, project_id),
        )
        if cursor.rowcount:
            self._log(user_id, project_id, "pipeline_removed", None)
        self.db.conn.commit()
        return cursor.rowcount > 0

    def stage_for(self, user_id: int, project_id: int) -> str | None:
        row = self.db.conn.execute(
            "SELECT stage FROM pipeline_entry WHERE user_id = ? AND project_id = ?",
            (user_id, project_id),
        ).fetchone()
        return row["stage"] if row else None

    def pipeline_rows(self, user_id: int) -> list[dict[str, Any]]:
        rows = self.db.conn.execute(
            """
            SELECT pe.*, s.slug
              FROM pipeline_entry pe
              LEFT JOIN project_slug s ON s.project_id = pe.project_id
             WHERE pe.user_id = ?
             ORDER BY pe.updated_at DESC
            """,
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def pipeline_counts(self, user_id: int) -> dict[str, int]:
        rows = self.db.conn.execute(
            "SELECT stage, COUNT(*) AS n FROM pipeline_entry WHERE user_id = ? GROUP BY stage",
            (user_id,),
        ).fetchall()
        by_stage = {r["stage"]: int(r["n"]) for r in rows}
        return {key: by_stage.get(key, 0) for key, _ in PIPELINE_STAGES}

    def stage_project_ids(self, user_id: int, stage: str) -> list[int]:
        rows = self.db.conn.execute(
            "SELECT project_id FROM pipeline_entry WHERE user_id = ? AND stage = ? "
            "ORDER BY updated_at DESC",
            (user_id, normalise_stage(stage)),
        ).fetchall()
        return [int(r["project_id"]) for r in rows]

    def assign(self, user_id: int, project_id: int, assignee_id: int | None) -> bool:
        """Assign an opportunity to a member of the account's organization."""
        if not self._project_exists(project_id):
            return False
        if not self.can_assign_to(user_id, assignee_id):
            raise WorkflowError("That account is not a member of your organization.")
        now = self._now()
        self.db.conn.execute(
            """
            INSERT INTO pipeline_entry (user_id, project_id, stage, assigned_to, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, project_id) DO UPDATE SET
                assigned_to = excluded.assigned_to,
                updated_at = excluded.updated_at
            """,
            (user_id, project_id, DEFAULT_STAGE, assignee_id, now),
        )
        self._log(
            user_id, project_id, "assigned",
            str(assignee_id) if assignee_id is not None else "unassigned",
        )
        self.db.conn.commit()
        return True

    def set_follow_up(self, user_id: int, project_id: int, follow_up_date: str | None) -> bool:
        if not self._project_exists(project_id):
            return False
        now = self._now()
        self.db.conn.execute(
            """
            INSERT INTO pipeline_entry (user_id, project_id, stage, follow_up_date, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, project_id) DO UPDATE SET
                follow_up_date = excluded.follow_up_date,
                updated_at = excluded.updated_at
            """,
            (user_id, project_id, DEFAULT_STAGE, (follow_up_date or None), now),
        )
        self._log(user_id, project_id, "follow_up", follow_up_date or "cleared")
        self.db.conn.commit()
        return True

    # --- notes ----------------------------------------------------------------

    def add_note(self, user_id: int, project_id: int, body: str) -> int:
        """Attach a private note. User-authored text, stored per account."""
        text = (body or "").strip()
        if not text:
            raise WorkflowError("A note cannot be empty.")
        if len(text) > MAX_NOTE_LENGTH:
            raise WorkflowError(f"A note cannot exceed {MAX_NOTE_LENGTH} characters.")
        if not self._project_exists(project_id):
            raise WorkflowError("That opportunity no longer exists.")
        cursor = self.db.conn.execute(
            """
            INSERT INTO opportunity_note (user_id, project_id, body, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, project_id, text, self._now()),
        )
        self._log(user_id, project_id, "note_added", None)
        self.db.conn.commit()
        return int(cursor.lastrowid)

    def notes_for(self, user_id: int, project_id: int) -> list[dict[str, Any]]:
        rows = self.db.conn.execute(
            """
            SELECT * FROM opportunity_note
             WHERE user_id = ? AND project_id = ?
             ORDER BY created_at DESC, id DESC
            """,
            (user_id, project_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_note(self, user_id: int, note_id: int) -> bool:
        """Delete a note. Scoped to the owner so a note id cannot delete another's record."""
        row = self.db.conn.execute(
            "SELECT project_id FROM opportunity_note WHERE id = ? AND user_id = ?",
            (note_id, user_id),
        ).fetchone()
        if row is None:
            return False
        self.db.conn.execute(
            "DELETE FROM opportunity_note WHERE id = ? AND user_id = ?", (note_id, user_id)
        )
        self._log(user_id, int(row["project_id"]), "note_deleted", None)
        self.db.conn.commit()
        return True

    def note_count(self, user_id: int, project_id: int) -> int:
        return int(
            self.db.conn.execute(
                "SELECT COUNT(*) FROM opportunity_note WHERE user_id = ? AND project_id = ?",
                (user_id, project_id),
            ).fetchone()[0]
        )

    def note_counts_by_project(self, user_id: int) -> dict[int, int]:
        rows = self.db.conn.execute(
            "SELECT project_id, COUNT(*) AS n FROM opportunity_note WHERE user_id = ? "
            "GROUP BY project_id",
            (user_id,),
        ).fetchall()
        return {int(r["project_id"]): int(r["n"]) for r in rows}

    def note_counts_many(self, project_ids: list[int]) -> dict[int, int]:
        if not project_ids:
            return {}
        placeholders = ",".join("?" for _ in project_ids)
        rows = self.db.conn.execute(
            f"SELECT project_id, COUNT(*) AS n FROM evidence WHERE project_id IN ({placeholders}) "
            f"GROUP BY project_id",
            project_ids,
        ).fetchall()
        return {int(r["project_id"]): int(r["n"]) for r in rows}

    # --- tags -----------------------------------------------------------------

    def add_tag(self, user_id: int, project_id: int, tag: str) -> bool:
        cleaned = clean_tag(tag)
        if not cleaned:
            raise WorkflowError("Enter a tag.")
        existing = self.db.conn.execute(
            "SELECT COUNT(*) FROM opportunity_tag WHERE user_id = ? AND project_id = ?",
            (user_id, project_id),
        ).fetchone()[0]
        if int(existing) >= MAX_TAGS_PER_PROJECT:
            raise WorkflowError(f"An opportunity cannot carry more than {MAX_TAGS_PER_PROJECT} tags.")
        if not self._project_exists(project_id):
            raise WorkflowError("That opportunity no longer exists.")
        cursor = self.db.conn.execute(
            """
            INSERT OR IGNORE INTO opportunity_tag (user_id, project_id, tag, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, project_id, cleaned, self._now()),
        )
        if cursor.rowcount:
            self._log(user_id, project_id, "tag_added", cleaned)
        self.db.conn.commit()
        return cursor.rowcount > 0

    def remove_tag(self, user_id: int, project_id: int, tag: str) -> bool:
        cursor = self.db.conn.execute(
            "DELETE FROM opportunity_tag WHERE user_id = ? AND project_id = ? AND tag = ?",
            (user_id, project_id, clean_tag(tag)),
        )
        self.db.conn.commit()
        return cursor.rowcount > 0

    def tags_for(self, user_id: int, project_id: int) -> list[str]:
        rows = self.db.conn.execute(
            "SELECT tag FROM opportunity_tag WHERE user_id = ? AND project_id = ? ORDER BY tag",
            (user_id, project_id),
        ).fetchall()
        return [r["tag"] for r in rows]

    def all_tags(self, user_id: int) -> list[tuple[str, int]]:
        rows = self.db.conn.execute(
            """
            SELECT tag, COUNT(*) AS n FROM opportunity_tag
             WHERE user_id = ? GROUP BY tag ORDER BY n DESC, tag
            """,
            (user_id,),
        ).fetchall()
        return [(r["tag"], int(r["n"])) for r in rows]

    # --- activity -------------------------------------------------------------

    def activity_for(self, user_id: int, project_id: int, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.conn.execute(
            """
            SELECT * FROM user_activity
             WHERE user_id = ? AND project_id = ?
             ORDER BY id DESC LIMIT ?
            """,
            (user_id, project_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_activity(self, user_id: int, limit: int = 25) -> list[dict[str, Any]]:
        rows = self.db.conn.execute(
            """
            SELECT a.*, s.slug, p.project_name, p.address, p.city
              FROM user_activity a
              JOIN project p ON p.id = a.project_id
              LEFT JOIN project_slug s ON s.project_id = a.project_id
             WHERE a.user_id = ?
             ORDER BY a.id DESC LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- summary --------------------------------------------------------------

    def summary(self, user_id: int) -> dict[str, Any]:
        """Counts for the dashboard, each a real count of stored rows for this account."""
        return {
            "saved": int(
                self.db.conn.execute(
                    "SELECT COUNT(*) FROM saved_opportunity WHERE user_id = ?", (user_id,)
                ).fetchone()[0]
            ),
            "watching": int(
                self.db.conn.execute(
                    "SELECT COUNT(*) FROM watched_opportunity WHERE user_id = ?", (user_id,)
                ).fetchone()[0]
            ),
            "pipeline": int(
                self.db.conn.execute(
                    "SELECT COUNT(*) FROM pipeline_entry WHERE user_id = ?", (user_id,)
                ).fetchone()[0]
            ),
            "notes": int(
                self.db.conn.execute(
                    "SELECT COUNT(*) FROM opportunity_note WHERE user_id = ?", (user_id,)
                ).fetchone()[0]
            ),
            "stages": self.pipeline_counts(user_id),
        }

    def project_ids_with_activity(self, user_id: int, limit: int = 20) -> list[int]:
        rows = self.db.conn.execute(
            """
            SELECT project_id, MAX(id) AS last_id FROM user_activity
             WHERE user_id = ? GROUP BY project_id
             ORDER BY last_id DESC LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [int(r["project_id"]) for r in rows]
