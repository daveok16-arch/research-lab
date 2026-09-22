"""Ingestion pipeline orchestration.

Stages: FETCH -> LAND -> NORMALIZE -> PERSIST permits -> ASSEMBLE -> CLASSIFY -> PERSIST.

Stages after LAND are pure functions over data and never touch the network, which is why
the test suite can exercise the real assembly and classification paths offline.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterator

import yaml

from .assemble import assemble_project, cluster_permits
from .classify import classify
from .config import CONFIG_DIR, SourceConfig, active_trade, load_sources, load_trades
from .connectors import build_connector, connector_ids
from .db import Database
from .discrepancy import find_discrepancies, mark_disputed
from .procurement import procurement_status
from .models import Permit, RawPermit, normalize_address, utcnow

log = logging.getLogger(__name__)

#: Commit the permit batch every N rows so a long crawl is not an all-or-nothing transaction.
COMMIT_EVERY = 250


@dataclass
class IngestResult:
    source_id: str
    status: str = "ok"
    rows_fetched: int = 0
    rows_landed: int = 0
    permits_created: int = 0
    error: str | None = None
    #: Pages fetched per permit type where the connector reports them. Makes a crawl that
    #: was cut short by a page cap visible rather than presenting as complete.
    pages_by_type: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "status": self.status,
            "rows_fetched": self.rows_fetched,
            "rows_landed": self.rows_landed,
            "permits_created": self.permits_created,
            "error": self.error,
            "pages_by_type": self.pages_by_type,
            "total_pages": sum(self.pages_by_type.values()),
        }


@dataclass
class PipelineReport:
    results: list[IngestResult] = field(default_factory=list)
    projects_written: int = 0
    classification_counts: dict[str, int] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sources": [r.as_dict() for r in self.results],
            "projects_written": self.projects_written,
            "classification_counts": self.classification_counts,
            "coverage": self.coverage,
        }


class Pipeline:
    def __init__(self, db: Database, *, trade_id: str | None = None):
        self.db = db
        self.trade = active_trade() if trade_id is None else load_trades()[trade_id]
        self.source_names = {sid: cfg.name for sid, cfg in load_sources().items()}
        self.source_defaults = load_sources()
        #: Pages fetched per source during this pipeline's lifetime, for coverage metadata.
        self._pages_seen: dict[str, int] = {}
        self._raw_defaults: dict[str, Any] = yaml.safe_load(
            (CONFIG_DIR / "sources.yaml").read_text()
        ).get("defaults", {})

    def _register_sources(self) -> None:
        for cfg in load_sources().values():
            self.db.upsert_source(cfg)

    # --- stage 1-3: fetch, land, normalize ------------------------------------

    def ingest_source(
        self,
        source_id: str,
        *,
        since: date | None = None,
        max_pages: int | None = None,
        progress: Callable[[int], None] | None = None,
    ) -> IngestResult:
        """Run one source end to end and persist its permits."""
        cfg = self.source_defaults.get(source_id)
        if cfg is None:
            return IngestResult(source_id=source_id, status="error",
                                error="Source has no configuration entry")

        result = IngestResult(source_id=source_id)
        run_id = self.db.begin_run(source_id)
        connector = build_connector(source_id, self._raw_defaults)
        if max_pages is not None:
            connector.max_pages = max_pages

        try:
            landing = connector.landing_path()
            raw_stream = connector.fetch_raw(since=since)

            def counted() -> Iterator[RawPermit]:
                for raw in raw_stream:
                    result.rows_fetched += 1
                    yield raw

            result.rows_landed = connector.land_to_disk(counted(), landing)

            for raw in _replay(landing):
                if self.db.land_raw(
                    raw.source_id, run_id, raw.natural_key, raw.payload,
                    raw.payload_hash, raw.fetched_at,
                ):
                    pass
                permit = connector.normalize(raw)
                if permit is None:
                    continue
                self.db.upsert_permit(permit, normalize_address(permit.address))
                result.permits_created += 1
                # Commit periodically. A long crawl over a slow public portal can be
                # interrupted by a timeout or an operator, and without an intermediate
                # commit every permit fetched so far would be rolled back and re-fetched.
                if result.permits_created % COMMIT_EVERY == 0:
                    self.db.commit()
                    log.info(
                        "%s: committed %d permits", source_id, result.permits_created
                    )
                if progress and result.permits_created % 500 == 0:
                    progress(result.permits_created)

            result.pages_by_type = dict(getattr(connector, "pages_by_type", {}) or {})
            self._pages_seen[source_id] = sum(result.pages_by_type.values())
            self.db.commit()
            self.db.finish_run(
                run_id, "ok", result.rows_fetched, result.rows_landed, result.permits_created
            )
        except Exception as exc:  # noqa: BLE001 - recorded on the run, then surfaced
            log.exception("Ingest failed for %s", source_id)
            self.db.finish_run(
                run_id, "error", result.rows_fetched, result.rows_landed,
                result.permits_created, str(exc),
            )
            result.status = "error"
            result.error = str(exc)
        return result

    # --- stage 4-6: assemble, classify, persist --------------------------------

    def assemble_and_classify(self) -> PipelineReport:
        """Cluster stored permits into projects, classify them, and persist."""
        report = PipelineReport()
        permits_with_ids = self.db.load_permits_for_assembly()
        permits = [p for p, _ in permits_with_ids]
        id_by_key = {(p.source_id, p.natural_key): pid for p, pid in permits_with_ids}

        clusters = cluster_permits(permits, self.trade)
        observed = utcnow()

        for address_key, cluster in clusters.items():
            project = assemble_project(
                address_key, cluster, self.trade, self.source_names, observed_at=observed,
            )
            # Discrepancies are detected from the recorded evidence before classification, so
            # the classifier's reasons include any unresolved disagreement between sources.
            mark_disputed(project, find_discrepancies(project))
            classify(project, self.trade)
            # Procurement status is derived from what the sources state, never assumed.
            project.procurement_status = procurement_status(project)
            project_id = self.db.upsert_project(project)
            for permit in cluster:
                pid = id_by_key.get((permit.source_id, permit.natural_key))
                if pid:
                    self.db.link_permit(project_id, pid)
            self.db.record_classification(project_id, project)

            report.projects_written += 1
            label = project.classification or "UNCLASSIFIED"
            report.classification_counts[label] = report.classification_counts.get(label, 0) + 1

        self.db.commit()
        return report

    # --- convenience ----------------------------------------------------------

    def run(
        self,
        source_ids: list[str] | None = None,
        *,
        since: date | None = None,
        max_pages: int | None = None,
    ) -> PipelineReport:
        self.db.init_schema()
        self._register_sources()

        ids = source_ids or [
            sid for sid, cfg in self.source_defaults.items() if cfg.enabled
        ]
        unknown = [s for s in ids if s not in connector_ids()]
        if unknown:
            raise ValueError(
                f"Unknown source(s): {', '.join(unknown)}. "
                f"Available: {', '.join(connector_ids())}"
            )

        report = PipelineReport()
        for source_id in ids:
            log.info("Ingesting %s", source_id)
            report.results.append(
                self.ingest_source(source_id, since=since, max_pages=max_pages)
            )

        assembled = self.assemble_and_classify()
        report.projects_written = assembled.projects_written
        report.classification_counts = assembled.classification_counts
        report.coverage = self.record_coverage()
        return report

    def record_coverage(self, pagination_pages: int | None = None) -> dict[str, Any]:
        """Persist per-source coverage metadata for every configured source.

        Called after ingestion so that the coverage figures describe what is actually held.
        Pagination counts come from the connector where it exposes them, so a crawl that was
        truncated by a page cap is recorded rather than silently presenting as complete.
        """
        retrieval = utcnow()
        coverage: dict[str, Any] = {}
        for source_id, cfg in self.source_defaults.items():
            pages = (
                pagination_pages
                if pagination_pages is not None
                else self._pages_seen.get(source_id)
            )
            notes = cfg.coverage_note

            if source_id == "dallas_accela_permits":
                notes = (
                    "Current. Searched by commercial record type from 2026-01-01. "
                    "Paginated to exhaustion; the portal's result counter grows as it is "
                    "paged (100+ then 200+) and is a lower bound, not a total."
                )

            self.db.record_source_coverage(
                source_id,
                retrieval_date=retrieval,
                pagination_pages=pages,
                pagination_notes=notes,
            )
            row = self.db.conn.execute(
                "SELECT * FROM source_coverage WHERE source_id = ?", (source_id,)
            ).fetchone()
            coverage[source_id] = dict(row) if row else {}
        return coverage


def _replay(landing_path) -> Iterator[RawPermit]:
    """Re-read a landing file so the DB write path consumes the same bytes written.

    Reading back from disk rather than from memory means the landed file is proven to be
    complete and parseable, which is what makes replay-after-a-bug-fix trustworthy.
    """
    import json
    from datetime import datetime

    with open(landing_path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            data = json.loads(line)
            yield RawPermit(
                source_id=data["source_id"],
                natural_key=data["natural_key"],
                payload=data["payload"],
                source_url=data.get("source_url"),
                fetched_at=datetime.fromisoformat(data["fetched_at"]),
            )