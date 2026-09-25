"""Stable public slugs for projects.

A published URL must not change. Project rows are rewritten on every re-assembly (ids are
stable, but addresses and names can be corrected), so a slug derived on the fly would move
underneath a link someone had already shared or a search engine had already indexed.

The slug is therefore generated once and stored in `project_slug`. Regeneration only happens
for projects that do not yet have one, so an existing URL is never rewritten.

Slug shape: ``<street-address>-<city>-<short-hash>``, for example
``1807-ross-ave-300-dallas-a1b2c3``.

The hash is a truncated digest of the project key. It is what stops two suites at one street
number from colliding, and it is stable across runs because the project key is.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from .db import Database

#: Characters that are safe in a URL path segment.
_UNSAFE = re.compile(r"[^a-z0-9]+")

#: Longest slug we will generate before truncating the readable part. Keeps URLs shareable
#: while leaving room for the disambiguating hash.
MAX_BASE_LENGTH = 72

SHORT_HASH_LENGTH = 6


def _slugify(text: str | None) -> str:
    if not text:
        return ""
    lowered = text.lower()
    # Ampersands read better spelled out than dropped: "hvac & mechanical" -> "hvac-mechanical".
    lowered = lowered.replace("&", " ")
    return _UNSAFE.sub("-", lowered).strip("-")


def short_hash(project_key: str) -> str:
    """A short, stable digest of the project key."""
    return hashlib.sha1(project_key.encode("utf-8")).hexdigest()[:SHORT_HASH_LENGTH]


def build_slug(
    *,
    address: str | None,
    city: str | None,
    project_key: str,
) -> str:
    """Derive the readable, stable slug for a project.

    Falls back to the city alone when the address is unusable, because some Dallas records
    carry descriptive names rather than a street number.
    """
    base = "-".join(part for part in (_slugify(address), _slugify(city)) if part)
    if not base:
        base = "opportunity"
    if len(base) > MAX_BASE_LENGTH:
        base = base[:MAX_BASE_LENGTH].rstrip("-")
    return f"{base}-{short_hash(project_key)}"


def ensure_slugs(db: Database, *, project_ids: list[int] | None = None) -> int:
    """Generate slugs for projects that do not have one yet. Returns the number created.

    Never overwrites an existing slug, so a URL that has been published stays valid even if
    the underlying address is later corrected.
    """
    where = "WHERE p.id NOT IN (SELECT project_id FROM project_slug)"
    params: tuple = ()
    if project_ids:
        placeholders = ",".join("?" for _ in project_ids)
        where += f" AND p.id IN ({placeholders})"
        params = tuple(project_ids)

    rows = db.conn.execute(
        f"""
        SELECT p.id, p.address, p.city, p.project_key
          FROM project p
          {where}
        """,
        params,
    ).fetchall()

    now = datetime.now(timezone.utc).isoformat()
    created = 0
    for row in rows:
        slug = build_slug(
            address=row["address"], city=row["city"], project_key=row["project_key"]
        )
        # A hash collision at six characters is unlikely but not impossible; extend it rather
        # than silently drop the project from the site.
        candidate = slug
        attempt = 0
        while db.conn.execute(
            "SELECT 1 FROM project_slug WHERE slug = ?", (candidate,)
        ).fetchone():
            attempt += 1
            candidate = f"{slug}-{attempt}"
        db.conn.execute(
            "INSERT OR IGNORE INTO project_slug (slug, project_id, created_at) VALUES (?, ?, ?)",
            (candidate, row["id"], now),
        )
        created += 1
    db.conn.commit()
    return created


def slug_for_project(db: Database, project_id: int) -> str | None:
    row = db.conn.execute(
        "SELECT slug FROM project_slug WHERE project_id = ?", (project_id,)
    ).fetchone()
    return row["slug"] if row else None


def project_id_for_slug(db: Database, slug: str) -> int | None:
    row = db.conn.execute(
        "SELECT project_id FROM project_slug WHERE slug = ?", (slug,)
    ).fetchone()
    return int(row["project_id"]) if row else None