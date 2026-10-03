# Operations: running and automating the app

The app is served by gunicorn on `0.0.0.0:12000` and kept fresh by one background
daemon. There is no cron or systemd in this environment, so `ops/automate.py` provides
both the schedule and the process supervision in a single Python process.

## Start

```bash
ops/start.sh
```

This launches `ops/automate.py --serve`, which:

1. starts the gunicorn WSGI server and restarts it if it exits;
2. runs the online-search pipeline every `REFRESH_SECONDS` (default 6 hours):
   `ingest` -> `assemble` -> `build-search-index` -> `monitor`.

Each refresh fetches the newest `MAX_PAGES` pages (default 3) per source, then **doubles that
depth after every clean pass** until it reaches `BACKFILL_MAX_PAGES` (default 200, the
connectors' own page limit). Every connector orders newest-first, so a bounded fetch still
covers recent permits, and the pipeline is idempotent, so nothing is fetched twice.

The doubling matters: a fixed `--max-pages 3` re-reads the same newest pages on every pass and
never reaches older history. That is why a live instance sat at roughly 3k rows per source while
a manual full run collected 196k. Growing the depth lets a service left running on a small
interval walk back through the source history on its own, without one long request that the host
might time out. A pass that fails does not widen — it is retried at the same width.

Override with environment variables:

```bash
PORT=12001 REFRESH_SECONDS=3600 MAX_PAGES=5 ops/start.sh
GROW_BACKFILL=0 MAX_PAGES=10 ops/start.sh   # pin the depth instead of growing it
```

`BACKFILL_MAX_PAGES` sets the ceiling. The recorded depth lives in
`data/automation_state.json` under `backfill_pages`, so it survives a restart; delete that key
(or the file) to start the backfill from `MAX_PAGES` again.

On a container host the same script runs in the foreground instead — set `FOREGROUND=1`,
or let it detect `$RENDER`. It then binds the platform's `$PORT`. With a disk attached, set
`OPPINTEL_DATA_DIR` to its mount path so the database survives a redeploy:

```bash
RENDER=true PORT=10000 OPPINTEL_DATA_DIR=/var/data ops/start.sh
```

Without `OPPINTEL_DATA_DIR` it writes to the checkout's `data/`. That path always exists, which
is what makes the Free plan (no disk) start rather than fail on a missing `/var/data`. A path that
is configured but not writable is reported as such and exits 1, rather than surfacing as a sqlite
traceback.

`ops/stop.sh` stops the background daemon; it has nothing to do in the foreground case,
where the platform owns the process.

For a one-off full backfill of every page of every source:

```bash
PYTHONPATH=src python ops/automate.py --once --full
```

## Stop

```bash
ops/stop.sh
```

## One-off refresh

```bash
PYTHONPATH=src python ops/automate.py --once --max-pages 2
```

## State and logs

| File | Contents |
|---|---|
| `data/automation.pid` | PID of the running daemon |
| `data/automation_state.json` | Last run time, per-step result, run count |
| `data/automation.log` | Daemon log: step start/ok/failed lines |
| `data/automation.out` | Raw stdout/stderr of the daemon process |
| `data/web.log` | gunicorn access and error log |

## Why it is safe to run often

The pipeline is idempotent. Raw records are keyed by content hash, so re-ingesting
unchanged data writes nothing; assembly diffs each project against its previous snapshot
and records only real changes; `monitor` creates an alert only for a recorded change.
A refresh that finds nothing new produces no new rows and no alerts.
