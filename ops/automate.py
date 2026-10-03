#!/usr/bin/env python3
"""Continuous automation for the oppintel web application.

One process provides both halves of the deployment, so it needs no cron, systemd unit
or external process manager:

* a supervisor loop that keeps the WSGI server answering on the configured port,
  restarting it if it ever exits;
* a refresh loop, on its own thread, that runs the online-search pipeline on an
  interval -- fetch public permit sources, land and normalise them, assemble and
  classify projects, rebuild the search index, then raise alerts from any detected
  change.

The refresh runs on a separate thread so a long ingestion (which can take minutes)
never stops the supervisor from restarting the web server.

State is written to ``data/automation_state.json`` and the log to
``data/automation.log`` so a restart resumes the schedule instead of refetching
immediately, and so the current state is readable without attaching to the process.

Everything is idempotent. Re-running the pipeline with unchanged source data writes
nothing new (raw records are keyed by content hash, assembly diffs against the previous
snapshot), so a short interval is cheap and safe.

    python ops/automate.py --serve
    python ops/automate.py --once          # one refresh pass, no server, no loop
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"

#: Where runtime state lives — the database, the log and the schedule state file. `OPPINTEL_DATA_DIR`
#: overrides it so a host with a mounted disk keeps state across a redeploy instead of writing into
#: the (ephemeral) checkout.
DATA_DIR = Path(os.environ.get("OPPINTEL_DATA_DIR") or (REPO_ROOT / "data"))
STATE_FILE = DATA_DIR / "automation_state.json"
LOG_FILE = DATA_DIR / "automation.log"
WEB_LOG = DATA_DIR / "web.log"

#: Worker and thread counts for the WSGI server. SQLite serializes writers, so a large worker
#: count mostly adds lock contention; reads are concurrent and dominate, which is why a small
#: worker count with a thread pool is the better shape. A host with a fractional CPU wants one
#: worker, which is why both are overridable.
WEB_WORKERS = os.environ.get("WEB_WORKERS", "2")
WEB_THREADS = os.environ.get("WEB_THREADS", "4")

DEFAULT_REFRESH_SECONDS = 6 * 60 * 60

#: Pages fetched per source on a recurring refresh. Every connector orders newest-first, so a
#: bounded fetch always covers the most recent permits; re-running is idempotent, so the backlog
#: is filled in over successive passes rather than in one long request. ``--full`` removes the
#: cap for an initial backfill.
DEFAULT_MAX_PAGES = 3

#: How far the progressive backfill may widen. A bounded fetch alone never reaches older
#: history: `--max-pages 3` re-reads the same newest pages forever, which is why a live instance
#: sat at ~3k rows per source while a manual run collected 196k. Since the host may not allow one
#: long request, the depth doubles after each successful pass until it reaches this ceiling.
#: The default matches the connectors' own page limit, so the ceiling is effectively "everything".
BACKFILL_CEILING_PAGES = int(os.environ.get("BACKFILL_MAX_PAGES") or 200)

#: Multiplier applied to the page cap after each successful pass.
BACKFILL_GROWTH = 2

_LOG_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    line = f"{_now()} {message}"
    print(line, flush=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _LOG_LOCK:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("SECRET_KEY", "dev-only-not-for-production")
    env.setdefault("OPPINTEL_DB", str(DATA_DIR / "oppintel.db"))
    return env


def run_step(label: str, args: list[str], timeout: int) -> bool:
    """Run one pipeline command. Returns True on a clean exit."""
    started = time.time()
    log(f"step start: {label}")
    try:
        proc = subprocess.run(
            args,
            cwd=str(REPO_ROOT),
            env=_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log(f"step timeout: {label} after {timeout}s")
        return False
    duration = time.time() - started
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        log(f"step FAILED: {label} rc={proc.returncode} in {duration:.1f}s :: {' | '.join(tail)}")
        return False
    log(f"step ok: {label} in {duration:.1f}s")
    return True


def _next_depth(state: dict, base: int) -> int:
    """The page cap for the pass about to run.

    Depth grows only after a clean pass, so a failing source is retried at the same width
    rather than widening a broken request. Once the ceiling is reached it stays there: at that
    point each pass reads the full history, which is what keeps a fresh deployment from
    re-deriving old permits one bounded slice at a time.
    """
    depth = int(state.get("backfill_pages") or base)
    return max(base, min(depth, BACKFILL_CEILING_PAGES))


def refresh_once(max_pages: int | None = None, *, grow: bool = False) -> dict:
    """Run the full online-search pipeline once and record the outcome.

    With ``grow`` set the page cap is taken from the recorded backfill depth and doubled after a
    clean pass, so a service left running on a small interval walks back through the source
    history on its own. Without it the caller's cap is used exactly, which is what ``--once``
    and an explicit ``--max-pages`` expect.
    """
    py = sys.executable
    state = load_state()

    if grow and max_pages is not None:
        effective_pages = _next_depth(state, max_pages)
    else:
        effective_pages = max_pages

    ingest = [py, "-m", "oppintel.cli", "ingest"]
    if effective_pages is not None:
        ingest += ["--max-pages", str(effective_pages)]

    steps = [
        ("ingest", ingest, 1800),
        ("assemble", [py, "-m", "oppintel.cli", "assemble"], 900),
        ("build-search-index", [py, "-m", "flask", "--app", "oppintel.app.wsgi", "build-search-index"], 600),
        ("monitor", [py, "-m", "flask", "--app", "oppintel.app.wsgi", "monitor"], 600),
    ]

    results: dict[str, bool] = {}
    for label, args, timeout in steps:
        results[label] = run_step(label, args, timeout)
        if not results[label]:
            break

    ok = all(results.values()) and len(results) == len(steps)
    if grow and max_pages is not None:
        if ok and effective_pages is not None:
            state["backfill_pages"] = min(effective_pages * BACKFILL_GROWTH, BACKFILL_CEILING_PAGES)
        else:
            state["backfill_pages"] = effective_pages
    state["last_run"] = _now()
    state["last_run_ok"] = ok
    state["last_steps"] = results
    state["last_pages"] = effective_pages
    state["runs"] = int(state.get("runs", 0)) + 1
    save_state(state)
    return state


def start_server() -> subprocess.Popen:
    env = _env()
    env.setdefault("HOST", "0.0.0.0")
    env.setdefault("PORT", "12000")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handle = WEB_LOG.open("a", encoding="utf-8")
    log(f"starting web server on {env['HOST']}:{env['PORT']}")
    return subprocess.Popen(
        [
            sys.executable, "-m", "gunicorn",
            "--workers", WEB_WORKERS, "--threads", WEB_THREADS,
            "--bind", f"{env['HOST']}:{env['PORT']}",
            "--access-logfile", "-", "--error-logfile", "-",
            "oppintel.app.wsgi:application",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )


def _refresh_loop(refresh_seconds: int, max_pages: int | None, stop: threading.Event,
                  grow: bool = False) -> None:
    """Run the refresh on its own thread so the supervisor is never blocked."""
    while not stop.is_set():
        try:
            refresh_once(max_pages=max_pages, grow=grow)
        except Exception as exc:  # keep the loop alive across an unexpected failure
            log(f"refresh loop error: {exc!r}")
        stop.wait(refresh_seconds)


def run_forever(refresh_seconds: int, serve: bool, max_pages: int | None,
                grow: bool = False) -> None:
    stop = threading.Event()

    def _handle(_signum, _frame):
        log("shutdown signal received")
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    server: subprocess.Popen | None = None
    if serve:
        server = start_server()

    refresh_thread = threading.Thread(
        target=_refresh_loop, args=(refresh_seconds, max_pages, stop, grow), daemon=True
    )
    refresh_thread.start()

    try:
        while not stop.is_set():
            if serve and server is not None and server.poll() is not None:
                log(f"web server exited rc={server.returncode}; restarting")
                server = start_server()
            stop.wait(5)
    finally:
        if server is not None and server.poll() is None:
            log("stopping web server")
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description="Automate the oppintel online-search pipeline.")
    parser.add_argument("--once", action="store_true", help="run one refresh pass and exit")
    parser.add_argument("--serve", action="store_true", help="also supervise the web server")
    parser.add_argument("--refresh-seconds", type=int, default=DEFAULT_REFRESH_SECONDS,
                        help="seconds between refresh passes (default 6h)")
    parser.add_argument("--host", default=None,
                        help="bind address for the web server (default $HOST or 0.0.0.0)")
    parser.add_argument("--port", default=None,
                        help="port for the web server (default $PORT or 12000)")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES,
                        help=f"pages fetched per source (default {DEFAULT_MAX_PAGES})")
    parser.add_argument("--full", action="store_true",
                        help="fetch every page of every source instead of the newest few")
    parser.add_argument("--grow-backfill", action="store_true",
                        help="double the page cap after each clean pass, up to BACKFILL_MAX_PAGES")
    args = parser.parse_args()

    max_pages = None if args.full else args.max_pages

    # The bind address is read from the environment by `start_server`, so an explicit flag is
    # applied there rather than threaded through the supervisor loop.
    if args.host is not None:
        os.environ["HOST"] = args.host
    if args.port is not None:
        os.environ["PORT"] = str(args.port)

    if args.once:
        state = refresh_once(max_pages=max_pages)
        return 0 if state.get("last_run_ok") else 1

    run_forever(args.refresh_seconds, args.serve, max_pages, grow=args.grow_backfill)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
