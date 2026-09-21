"""Command-line interface for the opportunity intelligence pipeline.

Usage:
    python -m oppintel.cli initdb
    python -m oppintel.cli sources
    python -m oppintel.cli ingest [--source ID ...] [--since YYYY-MM-DD] [--max-pages N]
    python -m oppintel.cli assemble
    python -m oppintel.cli stats
    python -m oppintel.cli projects [--classification HIGH] [--city "Fort Worth"] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

from .config import DATA_DIR, load_sources
from .connectors import connector_ids
from .db import Database

DEFAULT_DB = DATA_DIR / "oppintel.db"


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def cmd_initdb(args: argparse.Namespace) -> int:
    with Database(args.db) as db:
        db.init_schema()
        for cfg in load_sources().values():
            db.upsert_source(cfg)
    print(f"Initialized database at {args.db}")
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    sources = load_sources()
    print(f"{'id':<26} {'enabled':<8} {'coverage':<11} name")
    print("-" * 100)
    for sid in connector_ids():
        cfg = sources.get(sid)
        if not cfg:
            print(f"{sid:<26} {'-':<8} {'-':<11} (no config entry)")
            continue
        print(f"{sid:<26} {str(cfg.enabled):<8} {cfg.market_coverage:<11} {cfg.name}")
        if cfg.coverage_note:
            print(f"{'':<26} note: {' '.join(cfg.coverage_note.split())}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    from .pipeline import Pipeline

    since = date.fromisoformat(args.since) if args.since else None
    with Database(args.db) as db:
        pipeline = Pipeline(db)
        report = pipeline.run(
            args.source or None, since=since, max_pages=args.max_pages
        )

    print(json.dumps(report.as_dict(), indent=2))
    errors = [r for r in report.results if r.status != "ok"]
    if errors:
        print(f"\n{len(errors)} source(s) failed.", file=sys.stderr)
        return 1
    return 0


def cmd_assemble(args: argparse.Namespace) -> int:
    from .pipeline import Pipeline

    with Database(args.db) as db:
        db.init_schema()
        pipeline = Pipeline(db)
        pipeline._register_sources()
        report = pipeline.assemble_and_classify()
    print(json.dumps(report.as_dict(), indent=2))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with Database(args.db) as db:
        db.init_schema()
        stats = db.stats()
    print(json.dumps(stats, indent=2))
    return 0


def cmd_projects(args: argparse.Namespace) -> int:
    """List assembled projects. Used to inspect Phase 1 output before the dashboard."""
    clauses: list[str] = []
    params: list = []
    if args.classification:
        clauses.append("classification = ?")
        params.append(args.classification)
    if args.city:
        clauses.append("city = ?")
        params.append(args.city)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with Database(args.db) as db:
        db.init_schema()
        rows = db.conn.execute(
            f"""
            SELECT project_key, project_name, address, city, project_type,
                   estimated_project_value, square_footage, project_status,
                   classification, classification_score, mechanical_hvac_evidence,
                   source_url, last_verified
              FROM project
              {where}
             ORDER BY classification_score DESC, estimated_project_value DESC
             LIMIT ?
            """,
            (*params, args.limit),
        ).fetchall()

    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2, default=str))
    else:
        print(f"{len(rows)} project(s)\n")
        for row in rows:
            value = row["estimated_project_value"]
            value_text = f"${value:,.0f}" if value else "Not verified."
            print(f"[{row['classification'] or '?':<18} {row['classification_score'] or 0:>4}] "
                  f"{row['project_type'] or 'Not verified.'}")
            print(f"    {row['address'] or 'Not verified.'}, {row['city'] or '?'}")
            print(f"    value={value_text}  status={row['project_status'] or 'Not verified.'}")
            if row["mechanical_hvac_evidence"]:
                print(f"    HVAC: {row['mechanical_hvac_evidence']}")
            print(f"    source: {row['source_url'] or 'Not verified.'}")
            print()
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Write the coverage report and, optionally, a validation report for HIGH projects."""
    from pathlib import Path

    from .reporting import coverage_report, validation_report

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with Database(args.db) as db:
        db.init_schema()
        coverage_path = out_dir / "coverage_report.md"
        coverage_path.write_text(coverage_report(db))

        ids = [
            r["id"]
            for r in db.conn.execute(
                """
                SELECT id FROM project
                 WHERE classification = ?
                 ORDER BY classification_score DESC
                """,
                ("HIGH",),
            )
        ]
        validation_path = out_dir / "validation_report.md"
        validation_path.write_text(validation_report(db, ids))

    print(f"Coverage report:   {coverage_path}")
    print(f"Validation report: {validation_path}  ({len(ids)} projects)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oppintel", description="DFW construction opportunity intelligence (MVP)"
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to the SQLite database")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("initdb", help="Create the database schema").set_defaults(func=cmd_initdb)
    sub.add_parser("sources", help="List configured data sources").set_defaults(func=cmd_sources)

    ingest = sub.add_parser("ingest", help="Fetch, land, and normalize permits")
    ingest.add_argument("--source", action="append",
                        help="Source id to ingest (repeatable). Defaults to all enabled.")
    ingest.add_argument("--since", help="Only ingest records on/after this date (YYYY-MM-DD)")
    ingest.add_argument("--max-pages", type=int, help="Cap pages per source (for smoke tests)")
    ingest.set_defaults(func=cmd_ingest)

    sub.add_parser("assemble", help="Assemble permits into projects and classify"
                   ).set_defaults(func=cmd_assemble)
    sub.add_parser("stats", help="Show database statistics").set_defaults(func=cmd_stats)

    projects = sub.add_parser("projects", help="List assembled projects")
    projects.add_argument("--classification", choices=["HIGH", "MEDIUM", "NEEDS_VERIFICATION"])
    projects.add_argument("--city")
    projects.add_argument("--limit", type=int, default=25)
    projects.add_argument("--json", action="store_true")
    projects.set_defaults(func=cmd_projects)

    report = sub.add_parser(
        "report", help="Write the coverage report and a validation report for HIGH projects"
    )
    report.add_argument("--out-dir", default="reports/out")
    report.set_defaults(func=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())