"""SQLite persistence layer.

The schema deliberately avoids SQLite-only constructs (no AUTOINCREMENT keyword beyond
INTEGER PRIMARY KEY, no STRICT tables) so migrating to PostgreSQL later is a driver swap
rather than a rewrite of the queries.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import Evidence, Permit, Project, ProjectParty

SCHEMA = """
CREATE TABLE IF NOT EXISTS source (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    publisher          TEXT,
    kind               TEXT NOT NULL,
    jurisdiction_city  TEXT,
    market_coverage    TEXT,
    coverage_note      TEXT,
    portal_url         TEXT,
    reliability        REAL,
    notes              TEXT,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_run (
    id                 INTEGER PRIMARY KEY,
    source_id          TEXT NOT NULL REFERENCES source(id),
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    status             TEXT NOT NULL,
    rows_fetched       INTEGER DEFAULT 0,
    rows_landed        INTEGER DEFAULT 0,
    permits_created    INTEGER DEFAULT 0,
    error              TEXT
);

CREATE TABLE IF NOT EXISTS raw_record (
    id                 INTEGER PRIMARY KEY,
    source_id          TEXT NOT NULL REFERENCES source(id),
    run_id             INTEGER REFERENCES ingest_run(id),
    natural_key        TEXT NOT NULL,
    payload            TEXT NOT NULL,
    payload_hash       TEXT NOT NULL,
    fetched_at         TEXT NOT NULL,
    UNIQUE (source_id, natural_key, payload_hash)
);

CREATE INDEX IF NOT EXISTS idx_raw_record_source ON raw_record(source_id, natural_key);

CREATE TABLE IF NOT EXISTS permit (
    id                 INTEGER PRIMARY KEY,
    source_id          TEXT NOT NULL REFERENCES source(id),
    natural_key        TEXT NOT NULL,
    permit_number      TEXT NOT NULL,
    permit_type        TEXT,
    permit_subtype     TEXT,
    permit_date        TEXT,
    status             TEXT,
    address            TEXT,
    address_key        TEXT,
    city               TEXT,
    state              TEXT,
    zip_code           TEXT,
    work_description   TEXT,
    land_use           TEXT,
    specific_use       TEXT,
    job_value          REAL,
    square_footage     REAL,
    owner              TEXT,
    contractor         TEXT,
    is_commercial      INTEGER,
    source_url         TEXT,
    source_date        TEXT,
    updated_at         TEXT NOT NULL,
    UNIQUE (source_id, natural_key)
);

CREATE INDEX IF NOT EXISTS idx_permit_address_key ON permit(address_key);
CREATE INDEX IF NOT EXISTS idx_permit_date ON permit(permit_date);
CREATE INDEX IF NOT EXISTS idx_permit_commercial ON permit(is_commercial);

CREATE TABLE IF NOT EXISTS project (
    id                        INTEGER PRIMARY KEY,
    project_key               TEXT NOT NULL UNIQUE,
    trade                     TEXT NOT NULL,
    project_name              TEXT,
    address                   TEXT,
    city                      TEXT,
    state                     TEXT,
    project_type              TEXT,
    estimated_project_value   REAL,
    square_footage            REAL,
    permit_number             TEXT,
    permit_date               TEXT,
    project_status            TEXT,
    owner                     TEXT,
    developer                 TEXT,
    general_contractor        TEXT,
    architect                 TEXT,
    mechanical_hvac_evidence  TEXT,
    source_name               TEXT,
    source_url                TEXT,
    source_date               TEXT,
    last_verified             TEXT,
    mechanical_evidence_tier  INTEGER,
    property_class            TEXT,
    location_precision        TEXT,
    classification            TEXT,
    classification_score      INTEGER,
    classification_reasons    TEXT,
    disputed_fields           TEXT,
    discrepancies             TEXT,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_project_city ON project(city);
CREATE INDEX IF NOT EXISTS idx_project_classification ON project(classification);
CREATE INDEX IF NOT EXISTS idx_project_type ON project(project_type);
CREATE INDEX IF NOT EXISTS idx_project_value ON project(estimated_project_value);
CREATE INDEX IF NOT EXISTS idx_project_permit_date ON project(permit_date);

CREATE TABLE IF NOT EXISTS project_permit (
    project_id         INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    permit_id          INTEGER NOT NULL REFERENCES permit(id) ON DELETE CASCADE,
    PRIMARY KEY (project_id, permit_id)
);

CREATE TABLE IF NOT EXISTS evidence (
    id                 INTEGER PRIMARY KEY,
    project_id         INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    field_name         TEXT NOT NULL,
    value              TEXT,
    source_id          TEXT NOT NULL,
    source_name        TEXT NOT NULL,
    source_url         TEXT,
    source_record_key  TEXT,
    source_date        TEXT,
    observed_at        TEXT NOT NULL,
    evidence_type      TEXT,
    tier               INTEGER,
    excerpt            TEXT
);

CREATE INDEX IF NOT EXISTS idx_evidence_project ON evidence(project_id, field_name);

CREATE TABLE IF NOT EXISTS project_party (
    id                 INTEGER PRIMARY KEY,
    project_id         INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    role               TEXT NOT NULL,
    name               TEXT NOT NULL,
    source_id          TEXT NOT NULL,
    source_url         TEXT,
    excerpt            TEXT
);

CREATE INDEX IF NOT EXISTS idx_party_project ON project_party(project_id, role);
CREATE INDEX IF NOT EXISTS idx_party_name ON project_party(name);

CREATE TABLE IF NOT EXISTS project_classification (
    id                 INTEGER PRIMARY KEY,
    project_id         INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    classified_at      TEXT NOT NULL,
    classification     TEXT NOT NULL,
    score              INTEGER NOT NULL,
    reasons            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_classification_project ON project_classification(project_id);
"""


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


class Database:
    """Thin wrapper around sqlite3 with the operations the pipeline needs."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- sources and runs -----------------------------------------------------

    def upsert_source(self, cfg: Any) -> None:
        self.conn.execute(
            """
            INSERT INTO source (id, name, publisher, kind, jurisdiction_city, market_coverage,
                                coverage_note, portal_url, reliability, notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                publisher = excluded.publisher,
                kind = excluded.kind,
                jurisdiction_city = excluded.jurisdiction_city,
                market_coverage = excluded.market_coverage,
                coverage_note = excluded.coverage_note,
                portal_url = excluded.portal_url,
                reliability = excluded.reliability,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (
                cfg.id, cfg.name, cfg.publisher, cfg.kind, cfg.jurisdiction_city,
                cfg.market_coverage,
                cfg.coverage_note, cfg.portal_url, cfg.reliability, cfg.notes,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()

    def begin_run(self, source_id: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO ingest_run (source_id, started_at, status) VALUES (?, ?, ?)",
            (source_id, datetime.now(timezone.utc).isoformat(), "running"),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        status: str,
        rows_fetched: int = 0,
        rows_landed: int = 0,
        permits_created: int = 0,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE ingest_run
               SET finished_at = ?, status = ?, rows_fetched = ?, rows_landed = ?,
                   permits_created = ?, error = ?
             WHERE id = ?
            """,
            (
                datetime.now(timezone.utc).isoformat(), status, rows_fetched, rows_landed,
                permits_created, error, run_id,
            ),
        )
        self.conn.commit()

    # --- raw landing ----------------------------------------------------------

    def land_raw(
        self, source_id: str, run_id: int, natural_key: str,
        payload: dict[str, Any], payload_hash: str, fetched_at: datetime,
    ) -> bool:
        """Store a verbatim source record. Returns False if already stored."""
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO raw_record
                (source_id, run_id, natural_key, payload, payload_hash, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (source_id, run_id, natural_key, json.dumps(payload, default=str),
             payload_hash, fetched_at.isoformat()),
        )
        return cur.rowcount > 0

    def commit(self) -> None:
        self.conn.commit()

    # --- permits --------------------------------------------------------------

    def upsert_permit(self, permit: Permit, address_key: str | None) -> int:
        now = datetime.now(timezone.utc).isoformat()
        is_commercial = None if permit.is_commercial is None else int(permit.is_commercial)
        self.conn.execute(
            """
            INSERT INTO permit (source_id, natural_key, permit_number, permit_type,
                permit_subtype, permit_date, status, address, address_key, city, state,
                zip_code, work_description, land_use, specific_use, job_value,
                square_footage, owner, contractor, is_commercial, source_url, source_date,
                updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, natural_key) DO UPDATE SET
                permit_number = excluded.permit_number,
                permit_type = excluded.permit_type,
                permit_subtype = excluded.permit_subtype,
                permit_date = excluded.permit_date,
                status = excluded.status,
                address = excluded.address,
                address_key = excluded.address_key,
                city = excluded.city,
                state = excluded.state,
                zip_code = excluded.zip_code,
                work_description = excluded.work_description,
                land_use = excluded.land_use,
                specific_use = excluded.specific_use,
                job_value = excluded.job_value,
                square_footage = excluded.square_footage,
                owner = excluded.owner,
                contractor = excluded.contractor,
                is_commercial = excluded.is_commercial,
                source_url = excluded.source_url,
                source_date = excluded.source_date,
                updated_at = excluded.updated_at
            """,
            (
                permit.source_id, permit.natural_key, permit.permit_number,
                permit.permit_type, permit.permit_subtype, _iso(permit.permit_date),
                permit.status, permit.address, address_key, permit.city, permit.state,
                permit.zip_code, permit.work_description, permit.land_use,
                permit.specific_use, permit.job_value, permit.square_footage,
                permit.owner, permit.contractor, is_commercial, permit.source_url,
                _iso(permit.source_date), now,
            ),
        )
        row = self.conn.execute(
            "SELECT id FROM permit WHERE source_id = ? AND natural_key = ?",
            (permit.source_id, permit.natural_key),
        ).fetchone()
        return int(row["id"])

    def load_permits_for_assembly(self) -> list[tuple[Permit, int]]:
        """Load commercial permits for clustering into projects.

        Residential rows are excluded here, before assembly, so they can never
        contaminate a commercial opportunity.
        """
        rows = self.conn.execute(
            """
            SELECT * FROM permit
             WHERE is_commercial = 1 OR is_commercial IS NULL
             ORDER BY address_key, permit_date
            """
        ).fetchall()
        result: list[tuple[Permit, int]] = []
        for row in rows:
            from .models import parse_date

            permit = Permit(
                source_id=row["source_id"],
                permit_number=row["permit_number"],
                natural_key=row["natural_key"],
                permit_type=row["permit_type"],
                permit_subtype=row["permit_subtype"],
                permit_date=parse_date(row["permit_date"]),
                status=row["status"],
                address=row["address"],
                city=row["city"],
                state=row["state"],
                zip_code=row["zip_code"],
                work_description=row["work_description"],
                land_use=row["land_use"],
                specific_use=row["specific_use"],
                job_value=row["job_value"],
                square_footage=row["square_footage"],
                owner=row["owner"],
                contractor=row["contractor"],
                is_commercial=None if row["is_commercial"] is None else bool(row["is_commercial"]),
                source_url=row["source_url"],
                source_date=parse_date(row["source_date"]),
            )
            result.append((permit, int(row["id"])))
        return result

    # --- projects -------------------------------------------------------------

    def find_project_id(self, project_key: str) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM project WHERE project_key = ?", (project_key,)
        ).fetchone()
        return int(row["id"]) if row else None

    def upsert_project(self, project: Project) -> int:
        now = datetime.now(timezone.utc).isoformat()
        reasons = json.dumps(project.classification_reasons or [])
        self.conn.execute(
            """
            INSERT INTO project (project_key, trade, project_name, address, city, state,
                project_type, estimated_project_value, square_footage, permit_number,
                permit_date, project_status, owner, developer, general_contractor,
                architect, mechanical_hvac_evidence, source_name, source_url, source_date,
                last_verified, mechanical_evidence_tier, property_class, location_precision,
                classification, classification_score, classification_reasons, disputed_fields,
                discrepancies, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_key) DO UPDATE SET
                project_name = excluded.project_name,
                address = excluded.address,
                city = excluded.city,
                state = excluded.state,
                project_type = excluded.project_type,
                estimated_project_value = excluded.estimated_project_value,
                square_footage = excluded.square_footage,
                permit_number = excluded.permit_number,
                permit_date = excluded.permit_date,
                project_status = excluded.project_status,
                owner = excluded.owner,
                developer = excluded.developer,
                general_contractor = excluded.general_contractor,
                architect = excluded.architect,
                mechanical_hvac_evidence = excluded.mechanical_hvac_evidence,
                source_name = excluded.source_name,
                source_url = excluded.source_url,
                source_date = excluded.source_date,
                last_verified = excluded.last_verified,
                mechanical_evidence_tier = excluded.mechanical_evidence_tier,
                property_class = excluded.property_class,
                location_precision = excluded.location_precision,
                classification = excluded.classification,
                classification_score = excluded.classification_score,
                classification_reasons = excluded.classification_reasons,
                disputed_fields = excluded.disputed_fields,
                discrepancies = excluded.discrepancies,
                updated_at = excluded.updated_at
            """,
            (
                project.project_key, project.trade, project.project_name, project.address,
                project.city, project.state, project.project_type,
                project.estimated_project_value, project.square_footage,
                project.permit_number, _iso(project.permit_date), project.project_status,
                project.owner, project.developer, project.general_contractor,
                project.architect, project.mechanical_hvac_evidence, project.source_name,
                project.source_url, _iso(project.source_date), _iso(project.last_verified),
                project.mechanical_evidence_tier, project.property_class,
                project.location_precision, project.classification, project.classification_score, reasons,
                json.dumps(project.disputed_fields or []),
                json.dumps(project.discrepancies or []), now, now,
            ),
        )
        row = self.conn.execute(
            "SELECT id FROM project WHERE project_key = ?", (project.project_key,)
        ).fetchone()
        project_id = int(row["id"])

        # Evidence, permit links, and parties are fully rebuilt on each pass so that a
        # corrected source record does not leave stale facts attached to the project.
        self.conn.execute("DELETE FROM evidence WHERE project_id = ?", (project_id,))
        self.conn.execute("DELETE FROM project_permit WHERE project_id = ?", (project_id,))
        self.conn.execute("DELETE FROM project_party WHERE project_id = ?", (project_id,))

        for ev in project.evidence:
            self._insert_evidence(project_id, ev)
        for party in project.parties:
            self.conn.execute(
                """
                INSERT INTO project_party (project_id, role, name, source_id, source_url, excerpt)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (project_id, party.role, party.name, party.source_id,
                 party.source_url, party.excerpt),
            )
        self.conn.commit()
        return project_id

    def _insert_evidence(self, project_id: int, ev: Evidence) -> None:
        self.conn.execute(
            """
            INSERT INTO evidence (project_id, field_name, value, source_id, source_name,
                source_url, source_record_key, source_date, observed_at, evidence_type,
                tier, excerpt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id, ev.field_name, ev.value, ev.source_id, ev.source_name,
                ev.source_url, ev.source_record_key, _iso(ev.source_date),
                _iso(ev.observed_at), ev.evidence_type, ev.tier, ev.excerpt,
            ),
        )

    def link_permit(self, project_id: int, permit_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO project_permit (project_id, permit_id) VALUES (?, ?)",
            (project_id, permit_id),
        )

    def record_classification(self, project_id: int, project: Project) -> None:
        """Append a classification decision to the history table.

        Only appended when the decision actually changes, so the history stays a record of
        real transitions rather than a log of every pipeline pass.
        """
        if not project.classification:
            return
        last = self.conn.execute(
            """
            SELECT classification, score FROM project_classification
             WHERE project_id = ? ORDER BY id DESC LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if last and last["classification"] == project.classification:
            return
        self.conn.execute(
            """
            INSERT INTO project_classification (project_id, classified_at, classification,
                score, reasons)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                project_id, datetime.now(timezone.utc).isoformat(),
                project.classification, project.classification_score or 0,
                json.dumps(project.classification_reasons or []),
            ),
        )
        self.conn.commit()

    # --- reporting ------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        def scalar(sql: str, params: Iterable[Any] = ()) -> Any:
            row = self.conn.execute(sql, tuple(params)).fetchone()
            return row[0] if row else None

        by_class = {
            r["classification"]: r["n"]
            for r in self.conn.execute(
                "SELECT classification, COUNT(*) AS n FROM project GROUP BY classification"
            )
        }
        by_city = {
            r["city"]: r["n"]
            for r in self.conn.execute(
                "SELECT city, COUNT(*) AS n FROM project GROUP BY city ORDER BY n DESC LIMIT 15"
            )
        }
        by_source = [
            dict(r)
            for r in self.conn.execute(
                """
                SELECT s.name AS source_name, s.market_coverage, COUNT(DISTINCT p.id) AS permits
                  FROM source s LEFT JOIN permit p ON p.source_id = s.id
                 GROUP BY s.id ORDER BY permits DESC
                """
            )
        ]
        return {
            "projects": scalar("SELECT COUNT(*) FROM project") or 0,
            "permits": scalar("SELECT COUNT(*) FROM permit") or 0,
            "raw_records": scalar("SELECT COUNT(*) FROM raw_record") or 0,
            "evidence_rows": scalar("SELECT COUNT(*) FROM evidence") or 0,
            "with_mechanical_evidence": scalar(
                "SELECT COUNT(*) FROM project WHERE mechanical_hvac_evidence IS NOT NULL"
            ) or 0,
            "by_classification": by_class,
            "by_city": by_city,
            "by_source": by_source,
        }