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

-- Per-source coverage metadata, refreshed on every ingest run. Records what the source
-- actually returned rather than what it is documented to return, so a truncated crawl or a
-- moving coverage window is visible instead of being inferred from the record count.
CREATE TABLE IF NOT EXISTS source_coverage (
    source_id          TEXT PRIMARY KEY REFERENCES source(id) ON DELETE CASCADE,
    earliest_date      TEXT,
    latest_date        TEXT,
    retrieval_date     TEXT NOT NULL,
    record_count       INTEGER NOT NULL DEFAULT 0,
    commercial_count   INTEGER NOT NULL DEFAULT 0,
    mechanical_count   INTEGER NOT NULL DEFAULT 0,
    pagination_pages   INTEGER,
    pagination_notes   TEXT,
    updated_at         TEXT NOT NULL
);

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
    procurement_status        TEXT,
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

#: Application-layer schema. Kept separate from the intelligence schema above so the
#: boundary between "facts we collected" and "how the website operates" stays visible.
#:
#: Nothing here duplicates intelligence data. Saved opportunities and alerts reference a
#: project by id; they never copy project fields, so a re-ingest cannot leave a user's saved
#: record holding stale or contradictory facts.
APP_SCHEMA = """
-- Public URL identity for a project. Generated from the project's own facts, and stored so
-- a published URL never changes when the underlying row is rewritten by a re-assembly.
CREATE TABLE IF NOT EXISTS project_slug (
    slug               TEXT PRIMARY KEY,
    project_id         INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_project_slug_project ON project_slug(project_id);

-- Minimal account model. No passwords are stored in plain text; see app/auth.py.
CREATE TABLE IF NOT EXISTS app_user (
    id                 INTEGER PRIMARY KEY,
    email              TEXT NOT NULL UNIQUE,
    password_hash      TEXT NOT NULL,
    display_name       TEXT,
    access_level       TEXT NOT NULL DEFAULT 'FREE',
    is_active          INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT NOT NULL,
    last_login_at      TEXT
);

-- A saved opportunity stores the *relationship*, never a copy of the project.
CREATE TABLE IF NOT EXISTS saved_opportunity (
    user_id            INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    project_id         INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    saved_at           TEXT NOT NULL,
    note               TEXT,
    PRIMARY KEY (user_id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_saved_user ON saved_opportunity(user_id, saved_at);

-- One preference row per user. Defaults are applied at creation from the active market and
-- trade, so a new user starts on the product's current configuration rather than a literal.
CREATE TABLE IF NOT EXISTS user_preference (
    user_id            INTEGER PRIMARY KEY REFERENCES app_user(id) ON DELETE CASCADE,
    market_id          TEXT,
    trade_id           TEXT,
    cities             TEXT,
    project_types      TEXT,
    notify_in_app      INTEGER NOT NULL DEFAULT 1,
    notify_email       INTEGER NOT NULL DEFAULT 0,
    min_value          REAL,
    max_value          REAL,
    updated_at         TEXT NOT NULL
);

-- =====================================================================
-- Change detection and monitoring
-- =====================================================================

-- One row per *detected difference* between two consecutive assembly passes. Written only
-- when a tracked field actually changed value, so the timeline is a record of real events
-- rather than a log of every pipeline run. `previous_value` and `current_value` are stored
-- verbatim so the product can state exactly what changed without recomputing it.
CREATE TABLE IF NOT EXISTS project_change (
    id                 INTEGER PRIMARY KEY,
    project_id         INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    field_name         TEXT NOT NULL,
    previous_value     TEXT,
    current_value      TEXT,
    change_kind        TEXT NOT NULL,
    summary            TEXT NOT NULL,
    source_id          TEXT,
    source_name        TEXT,
    source_url         TEXT,
    detected_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_change_project ON project_change(project_id, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_change_detected ON project_change(detected_at DESC);

-- The previous state of a project, used to diff the next assembly pass against. Kept apart
-- from `project` so the intelligence table itself carries no monitoring bookkeeping.
CREATE TABLE IF NOT EXISTS project_state_snapshot (
    project_id         INTEGER PRIMARY KEY REFERENCES project(id) ON DELETE CASCADE,
    state_hash         TEXT NOT NULL,
    tracked_values     TEXT NOT NULL,
    captured_at        TEXT NOT NULL
);

-- An opportunity the user has asked the system to monitor. Distinct from a save: a save
-- bookmarks a record, a watch asks for the record to be re-checked for meaningful change.
CREATE TABLE IF NOT EXISTS watched_opportunity (
    user_id            INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    project_id         INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    watched_at         TEXT NOT NULL,
    last_seen_change_id INTEGER,
    PRIMARY KEY (user_id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_watched_user ON watched_opportunity(user_id, watched_at DESC);

-- The user's own workflow state for an opportunity. This is explicitly *not* a statement
-- about the project's procurement status; the vocabulary is kept apart from
-- `project.procurement_status` so the two can never be read as the same thing.
CREATE TABLE IF NOT EXISTS pipeline_entry (
    user_id            INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    project_id         INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    stage              TEXT NOT NULL,
    follow_up_date     TEXT,
    assigned_to        INTEGER REFERENCES app_user(id) ON DELETE SET NULL,
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (user_id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_user ON pipeline_entry(user_id, stage, updated_at DESC);

-- Free-text notes a user keeps against an opportunity. User-authored, never merged into the
-- intelligence record and never shown to another account.
CREATE TABLE IF NOT EXISTS opportunity_note (
    id                 INTEGER PRIMARY KEY,
    user_id            INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    project_id         INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    body               TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_note_user ON opportunity_note(user_id, project_id, created_at DESC);

-- User-applied tags. A tag is the user's own label, so it is stored per user and never
-- treated as a fact about the project.
CREATE TABLE IF NOT EXISTS opportunity_tag (
    user_id            INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    project_id         INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    tag                TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    PRIMARY KEY (user_id, project_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_tag_user ON opportunity_tag(user_id, tag);

-- Per-user activity history over an opportunity: when it was saved, watched, moved, noted.
-- Distinct from `project_change`, which records what the *source data* did.
CREATE TABLE IF NOT EXISTS user_activity (
    id                 INTEGER PRIMARY KEY,
    user_id            INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    project_id         INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    action             TEXT NOT NULL,
    detail             TEXT,
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_activity_user ON user_activity(user_id, created_at DESC);

-- Covering indexes for the directory's hot predicates. The public listing always filters on
-- classification and procurement together and orders by permit date, so a composite index lets
-- SQLite satisfy the filter and the order from one structure instead of a scan plus a sort.
CREATE INDEX IF NOT EXISTS idx_project_public_listing
    ON project(classification, procurement_status, permit_date DESC);
CREATE INDEX IF NOT EXISTS idx_project_public_updated
    ON project(classification, procurement_status, updated_at DESC);
-- Supports the value-band filter and the type landing pages without a full scan.
CREATE INDEX IF NOT EXISTS idx_project_type_public
    ON project(project_type, classification, procurement_status);

-- =====================================================================
-- Organizations and entitlement
-- =====================================================================

-- An organization groups accounts that share work. Membership is the only way one account
-- sees another's private records, and it is granted explicitly rather than inferred.
CREATE TABLE IF NOT EXISTS organization (
    id                 INTEGER PRIMARY KEY,
    name               TEXT NOT NULL,
    slug               TEXT NOT NULL UNIQUE,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS organization_member (
    org_id             INTEGER NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    user_id            INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    role               TEXT NOT NULL DEFAULT 'MEMBER',
    created_at         TEXT NOT NULL,
    PRIMARY KEY (org_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_org_member_user ON organization_member(user_id);

-- Plan definitions. Separated from subscriptions and from entitlements: a plan is a
-- catalogue entry, a subscription is an account's declared relationship to a plan, and an
-- entitlement is the set of features that relationship grants. Payment is deliberately not
-- modelled; `subscription` records a status set by an operator or an external system, and no
-- code path can mark an account paid without that record existing.
CREATE TABLE IF NOT EXISTS plan (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    rank               INTEGER NOT NULL DEFAULT 0,
    description        TEXT,
    entitlements       TEXT NOT NULL DEFAULT '[]',
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscription (
    user_id            INTEGER PRIMARY KEY REFERENCES app_user(id) ON DELETE CASCADE,
    plan_id            TEXT NOT NULL REFERENCES plan(id),
    status             TEXT NOT NULL,
    external_ref       TEXT,
    started_at         TEXT NOT NULL,
    renewed_at         TEXT,
    updated_at         TEXT NOT NULL
);

-- =====================================================================
-- Data quality and error visibility
-- =====================================================================

-- Issues the pipeline observed while ingesting or assembling. Recorded rather than silently
-- repaired: a missing field stays missing and is reported as missing, never filled with a
-- plausible-looking value.
CREATE TABLE IF NOT EXISTS data_quality_issue (
    id                 INTEGER PRIMARY KEY,
    issue_type         TEXT NOT NULL,
    severity           TEXT NOT NULL,
    source_id          TEXT,
    project_id         INTEGER REFERENCES project(id) ON DELETE CASCADE,
    detail             TEXT NOT NULL,
    detected_at        TEXT NOT NULL,
    resolved_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_quality_type ON data_quality_issue(issue_type, severity);
CREATE INDEX IF NOT EXISTS idx_quality_source ON data_quality_issue(source_id);

-- Application-side errors, so a failed request is visible to an operator without reading the
-- process log. Deliberately carries no user id, no email and no request body.
CREATE TABLE IF NOT EXISTS app_error (
    id                 INTEGER PRIMARY KEY,
    path               TEXT NOT NULL,
    method             TEXT NOT NULL,
    status             INTEGER,
    message            TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_app_error_created ON app_error(created_at DESC);

-- Event-driven alerts are created after the application tables are migrated, because an
-- older deployment may hold a different `alert_event` shape. See `_ensure_alert_event`.

-- Product analytics. Deliberately minimal: an event name, an optional project, and a
-- timestamp. No IP address, no user agent, no free-text payload.
CREATE TABLE IF NOT EXISTS analytics_event (
    id                 INTEGER PRIMARY KEY,
    event_name         TEXT NOT NULL,
    project_id         INTEGER REFERENCES project(id) ON DELETE SET NULL,
    market_id          TEXT,
    trade_id           TEXT,
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_analytics_event ON analytics_event(event_name, created_at);

-- Full-text index over the searchable project text. A separate table rather than a virtual
-- column on `project`, so the intelligence schema stays untouched and the index can be
-- rebuilt independently. Rows are referenced by project id.
CREATE VIRTUAL TABLE IF NOT EXISTS project_search USING fts5(
    project_id UNINDEXED,
    project_name,
    address,
    city,
    permit_number,
    work_description,
    owner,
    project_type,
    tokenize = 'unicode61'
);
"""


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        """Create the intelligence schema.

        Deliberately does not create the application tables: the intelligence layer must keep
        working against a database that has never been touched by the web application.
        """
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def init_app_schema(self) -> None:
        """Create the application tables on top of an existing intelligence database.

        Additive and idempotent: every statement is `IF NOT EXISTS`, so this can run against a
        production database that already holds ingested data without altering it.
        """
        self.conn.executescript(APP_SCHEMA)
        self._migrate_app_tables()
        self._ensure_alert_event()
        self.conn.commit()

    def _ensure_alert_event(self) -> None:
        """Create the alert table in its current shape.

        Kept out of `APP_SCHEMA` because an older deployment may already hold `alert_event`
        with a narrower uniqueness constraint and no `change_id` column. Creating the table and
        its indexes here, after the migration step has rebuilt any legacy table, means the
        index statements always apply to the current shape.
        """
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS alert_event (
                id                 INTEGER PRIMARY KEY,
                user_id            INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
                project_id         INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
                change_id          INTEGER REFERENCES project_change(id) ON DELETE CASCADE,
                kind               TEXT NOT NULL,
                title              TEXT NOT NULL DEFAULT '',
                body               TEXT,
                created_at         TEXT NOT NULL,
                read_at            TEXT,
                email_sent_at      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_alert_user ON alert_event(user_id, read_at);
            -- One alert per detected change per user. Partial indexes are used because SQLite
            -- treats NULLs as distinct, so the change-scoped and match-scoped uniqueness rules
            -- have to be declared separately.
            CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_change
                ON alert_event(user_id, change_id, kind) WHERE change_id IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_match
                ON alert_event(user_id, project_id, kind) WHERE change_id IS NULL;
            """
        )

    def _migrate_app_tables(self) -> None:
        """Bring an existing application schema forward to the current shape.

        `CREATE TABLE IF NOT EXISTS` does not alter a table that already exists, so a deployed
        database would otherwise miss a newly added column or keep an outdated constraint. Two
        kinds of change are handled, both idempotent:

        * A **column addition**, applied with `ALTER TABLE ... ADD COLUMN`, which preserves the
          existing rows untouched.
        * A **constraint change**, which SQLite cannot express with `ALTER`, so the table is
          rebuilt: renamed aside, recreated from the current definition, rows copied, and the
          old table dropped. This is done inside the caller's transaction so a failure leaves
          the original table in place.
        """
        expected: dict[str, tuple[tuple[str, str], ...]] = {
            "user_preference": (("min_value", "REAL"), ("max_value", "REAL")),
        }
        for table, columns in expected.items():
            if not self._table_exists(table):
                continue
            present = self._columns(table)
            for name, kind in columns:
                if name not in present:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

        # `alert_event` originally carried a `UNIQUE (user_id, project_id, kind)` constraint,
        # which would silently drop a second alert of the same kind on one project. The alert
        # model needs one alert per detected change, so the constraint has to go, and only a
        # rebuild can remove it.
        if self._table_exists("alert_event") and "change_id" not in self._columns("alert_event"):
            self._rebuild_alert_event()

    def _columns(self, table: str) -> set[str]:
        return {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}

    def _rebuild_alert_event(self) -> None:
        """Recreate `alert_event` with the current definition, preserving existing rows.

        Old rows are carried across with a null `change_id`. They remain valid alerts; they
        simply predate change-linking, which is stated rather than fabricated, so the integrity
        check that looks for an underlying event treats a null change as the first-match case.
        """
        self.conn.execute("ALTER TABLE alert_event RENAME TO alert_event_legacy")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS alert_event (
                id                 INTEGER PRIMARY KEY,
                user_id            INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
                project_id         INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
                change_id          INTEGER REFERENCES project_change(id) ON DELETE CASCADE,
                kind               TEXT NOT NULL,
                title              TEXT NOT NULL DEFAULT '',
                body               TEXT,
                created_at         TEXT NOT NULL,
                read_at            TEXT,
                email_sent_at      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_alert_user ON alert_event(user_id, read_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_change
                ON alert_event(user_id, change_id, kind) WHERE change_id IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_match
                ON alert_event(user_id, project_id, kind) WHERE change_id IS NULL;
            """
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO alert_event (id, user_id, project_id, change_id, kind, title,
                body, created_at, read_at)
            SELECT id, user_id, project_id, NULL, kind, kind, NULL, created_at, read_at
              FROM alert_event_legacy
            """
        )
        self.conn.execute("DROP TABLE alert_event_legacy")

    def _table_exists(self, name: str) -> bool:
        return bool(
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
            ).fetchone()
        )

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
                procurement_status, classification, classification_score,
                classification_reasons, disputed_fields, discrepancies, created_at,
                updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                procurement_status = excluded.procurement_status,
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
                project.location_precision, project.procurement_status, project.classification,
                project.classification_score, reasons,
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

    def record_source_coverage(
        self,
        source_id: str,
        *,
        retrieval_date: datetime,
        pagination_pages: int | None = None,
        pagination_notes: str | None = None,
    ) -> None:
        """Recompute and store coverage metadata for a source from the stored permits.

        Derived from the database rather than from the crawl's own counters, so the figures
        describe what is actually held and cannot drift from the permit table.
        """
        row = self.conn.execute(
            """
            SELECT MIN(permit_date) AS earliest,
                   MAX(permit_date) AS latest,
                   COUNT(*) AS records,
                   SUM(CASE WHEN is_commercial = 1 THEN 1 ELSE 0 END) AS commercial,
                   SUM(CASE WHEN LOWER(COALESCE(permit_type,'')) LIKE '%mechanical%'
                            THEN 1 ELSE 0 END) AS mechanical
              FROM permit WHERE source_id = ?
            """,
            (source_id,),
        ).fetchone()

        now = datetime.now(timezone.utc).isoformat()
        # Preserve a previously recorded pagination count when this call does not supply one.
        # The ingest run knows the page count, but the later assemble run does not, so without
        # this the figure would be erased the moment projects were rebuilt.
        if pagination_pages is None:
            prior = self.conn.execute(
                "SELECT pagination_pages FROM source_coverage WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            if prior is not None:
                pagination_pages = prior["pagination_pages"]
        if pagination_notes is None:
            prior_notes = self.conn.execute(
                "SELECT pagination_notes FROM source_coverage WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            if prior_notes is not None:
                pagination_notes = prior_notes["pagination_notes"]

        self.conn.execute(
            """
            INSERT INTO source_coverage (source_id, earliest_date, latest_date, retrieval_date,
                record_count, commercial_count, mechanical_count, pagination_pages,
                pagination_notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                earliest_date = excluded.earliest_date,
                latest_date = excluded.latest_date,
                retrieval_date = excluded.retrieval_date,
                record_count = excluded.record_count,
                commercial_count = excluded.commercial_count,
                mechanical_count = excluded.mechanical_count,
                pagination_pages = excluded.pagination_pages,
                pagination_notes = excluded.pagination_notes,
                updated_at = excluded.updated_at
            """,
            (
                source_id,
                row["earliest"], row["latest"], retrieval_date.date().isoformat(),
                row["records"] or 0, row["commercial"] or 0, row["mechanical"] or 0,
                pagination_pages, pagination_notes, now,
            ),
        )
        self.conn.commit()

    # --- change detection -----------------------------------------------------

    def has_app_schema(self) -> bool:
        """Whether the application tables exist.

        The intelligence layer must keep working against a database the web application has
        never touched, so monitoring and data-quality bookkeeping are skipped rather than
        required when those tables are absent.
        """
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'project_change'"
        ).fetchone()
        return row is not None

    def project_ids(self) -> list[int]:
        """Every project id, for a monitoring pass over the whole dataset."""
        return [int(r["id"]) for r in self.conn.execute("SELECT id FROM project ORDER BY id")]

    def project_row(self, project_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM project WHERE id = ?", (project_id,)
        ).fetchone()
        return dict(row) if row else None

    def permits_for_project(self, project_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT pm.*, s.name AS source_display_name
              FROM permit pm
              JOIN project_permit pp ON pp.permit_id = pm.id
              LEFT JOIN source s ON s.id = pm.source_id
             WHERE pp.project_id = ?
             ORDER BY pm.permit_date, pm.permit_number
            """,
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def previous_snapshot(self, project_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM project_state_snapshot WHERE project_id = ?", (project_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["tracked_values"])
        except (TypeError, ValueError):
            return None

    def save_snapshot(self, project_id: int, values: dict[str, Any], digest: str) -> None:
        self.conn.execute(
            """
            INSERT INTO project_state_snapshot (project_id, state_hash, tracked_values, captured_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                state_hash = excluded.state_hash,
                tracked_values = excluded.tracked_values,
                captured_at = excluded.captured_at
            """,
            (project_id, digest, json.dumps(values, sort_keys=True), _utcnow()),
        )

    def record_changes(self, changes: list[Any]) -> int:
        """Persist detected changes. Returns the number written.

        A change row is only ever written by the detector, so the table is a record of
        observed differences and cannot be seeded with invented events.
        """
        if not changes:
            return 0
        now = _utcnow()
        for change in changes:
            self.conn.execute(
                """
                INSERT INTO project_change (project_id, field_name, previous_value,
                    current_value, change_kind, summary, source_id, source_name, source_url,
                    detected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    change.project_id, change.field_name,
                    None if change.previous_value is None else str(change.previous_value),
                    None if change.current_value is None else str(change.current_value),
                    change.change_kind, change.summary, change.source_id, change.source_name,
                    change.source_url, now,
                ),
            )
        self.conn.commit()
        return len(changes)

    def changes_for_project(self, project_id: int, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM project_change
             WHERE project_id = ?
             ORDER BY id DESC
             LIMIT ?
            """,
            (project_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def changes_since(self, since_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT c.*, p.project_name, p.address, p.city
              FROM project_change c
              JOIN project p ON p.id = c.project_id
             WHERE c.id > ?
             ORDER BY c.id
             LIMIT ?
            """,
            (since_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- data quality ---------------------------------------------------------

    def record_quality_issue(
        self, issue_type: str, severity: str, detail: str,
        *, source_id: str | None = None, project_id: int | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO data_quality_issue (issue_type, severity, source_id, project_id,
                detail, detected_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (issue_type, severity, source_id, project_id, detail, _utcnow()),
        )

    def quality_issues(self, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM data_quality_issue
             WHERE resolved_at IS NULL
             ORDER BY
               CASE severity WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END,
               detected_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def clear_quality_issues(self, issue_type: str | None = None) -> None:
        """Retire open issues at the start of a pass so counts reflect the current state."""
        if issue_type:
            self.conn.execute(
                "DELETE FROM data_quality_issue WHERE issue_type = ?", (issue_type,)
            )
        else:
            self.conn.execute("DELETE FROM data_quality_issue")
        self.conn.commit()

    def record_app_error(self, path: str, method: str, status: int | None, message: str) -> None:
        self.conn.execute(
            """
            INSERT INTO app_error (path, method, status, message, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (path[:300], method[:10], status, message[:500], _utcnow()),
        )
        self.conn.commit()

    def recent_app_errors(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM app_error ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def source_coverage(self) -> list[dict[str, Any]]:
        """Coverage metadata for every source, joined to its human-readable name."""
        return [
            dict(r)
            for r in self.conn.execute(
                """
                SELECT s.name, s.jurisdiction_city, s.market_coverage, c.*
                  FROM source s
                  LEFT JOIN source_coverage c ON c.source_id = s.id
                 ORDER BY c.record_count DESC NULLS LAST, s.name
                """
            )
        ]

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