"""Full-text search index over projects.

Search runs against an FTS5 table rather than `LIKE` against the permit tables. Two reasons:

1. **Speed.** A public search box hits this on every request. Scanning 22,000 permit rows per
   search would not stay fast as the dataset grows.
2. **Separation.** The index is derived data that can be dropped and rebuilt at any time,
   which keeps the intelligence schema authoritative.

Only text a customer would actually search is indexed. Nothing here invents or alters a
project fact: the index holds copies of existing columns purely to make them findable, and
every result is re-read from `project` before display, so a page never renders indexed text as
the source of truth.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .db import Database

log = logging.getLogger(__name__)

#: Indexed columns, in insert order. Kept in one place so the rebuild and the query agree.
INDEXED_COLUMNS = (
    "project_id",
    "project_name",
    "address",
    "city",
    "permit_number",
    "work_description",
    "owner",
    "project_type",
)

#: The project columns those index fields are populated from.
#:
#: The work description lives on the permits that formed the project. SQLite rejects
#: `GROUP_CONCAT(DISTINCT x, sep)` — a DISTINCT aggregate takes exactly one argument — so the
#: separator is applied by concatenating distinct values with a plain comma and normalising
#: the punctuation afterwards. The text is only indexed for matching, never displayed.
SOURCE_QUERY = """
SELECT p.id AS project_id,
       p.project_name,
       p.address,
       p.city,
       p.permit_number,
       (SELECT REPLACE(GROUP_CONCAT(DISTINCT pm.work_description), ',', ' ')
          FROM permit pm
          JOIN project_permit pp ON pp.permit_id = pm.id
         WHERE pp.project_id = p.id
           AND pm.work_description IS NOT NULL) AS work_description,
       p.owner,
       p.project_type
  FROM project p
"""


def rebuild_index(db: Database, *, batch_size: int = 500) -> int:
    """Rebuild the search index from scratch. Returns the number of projects indexed.

    A full rebuild rather than incremental updates: the whole index is cheap to construct
    relative to an ingestion run, and a rebuild cannot drift from the project table the way a
    sequence of partial updates can.
    """
    started = datetime.now(timezone.utc)
    db.conn.execute("DELETE FROM project_search")
    db.conn.commit()

    total = 0
    cursor = db.conn.execute(SOURCE_QUERY)
    batch: list[tuple] = []
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        for row in rows:
            batch.append(
                tuple(
                    "" if row[column] is None else str(row[column])
                    for column in INDEXED_COLUMNS
                )
            )
        db.conn.executemany(
            f"INSERT INTO project_search ({', '.join(INDEXED_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in INDEXED_COLUMNS)})",
            batch,
        )
        total += len(batch)
        batch.clear()
        db.conn.commit()

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    log.info("Rebuilt search index: %d projects in %.1fs", total, elapsed)
    return total


def index_count(db: Database) -> int:
    return int(db.conn.execute("SELECT COUNT(*) FROM project_search").fetchone()[0])


def quote_for_fts(text: str) -> str:
    """Turn user input into a safe FTS5 query string.

    User input is not valid FTS5 syntax on its own: an unbalanced quote or a bare ``AND``
    raises an OperationalError, which would surface as a 500. Each whitespace-separated term
    is therefore quoted and prefix-matched, so "ross ave" becomes ``"ross"* "ave"*``.

    This is a safety and ergonomics measure, not a security boundary: the value is still bound
    as a query parameter.
    """
    terms = [t for t in text.replace('"', " ").split() if t]
    if not terms:
        return ""
    return " ".join(f'"{term}"*' for term in terms)