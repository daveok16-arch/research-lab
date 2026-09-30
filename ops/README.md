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

Each refresh fetches the newest `MAX_PAGES` pages (default 3) per source. Every
connector orders newest-first, so a bounded fetch still covers recent permits, and the
pipeline is idempotent, so nothing is fetched twice. Override with environment variables:

```bash
PORT=12001 REFRESH_SECONDS=3600 MAX_PAGES=5 ops/start.sh
```

For a one-off full backfill of every page of every source:

```bash
PYTHONPATH=src python ops/automate.py --once --full
```

## Publish to GitHub Pages

GitHub Pages serves static files, so the dynamic app cannot run there. `.github/workflows/pages.yml`
runs the intelligence layer in the runner, freezes the public site and deploys it:

**https://daveok16-arch.github.io/research-lab/**

The workflow runs on every push to `main`, daily on a schedule, and on manual dispatch (with
`max_pages` or `full` inputs). It tests, ingests, assembles, indexes, exports and deploys.

`ops/export_static.py` does the freezing. It walks `sitemap.xml` — the app's own list of canonical
URLs — renders each through the app's test client, and follows same-site links to pick up pages the
sitemap omits (the report detail pages). Passing the project sub-path as `SCRIPT_NAME` makes
`url_for` emit prefixed links, so the frozen pages resolve their own assets without an HTML rewrite.

```bash
PYTHONPATH=src python ops/export_static.py \
    --base-url https://daveok16-arch.github.io/research-lab --out dist --strict
```

`--strict` fails the build if any internal link or asset resolves to no exported file.

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
