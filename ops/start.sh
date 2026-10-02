#!/usr/bin/env bash
# Start the oppintel web app and its persistent online-search automation.
#
# Two modes, chosen automatically:
#
#   * Render / any container host (foreground). Detected by `$RENDER`, or by an explicit
#     `FOREGROUND=1`. The platform requires the process to stay in the foreground and to bind
#     the port it assigns, so this runs the supervisor directly. `$PORT` comes from the platform;
#     `$OPPINTEL_DATA_DIR` points at the mounted disk so the database survives a redeploy.
#
#   * Local / any shell without a process manager (background). Runs the supervisor in the
#     background and records its PID so stop.sh can find it. Safe to re-run: an already-running
#     daemon is detected and left alone.
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"
export SECRET_KEY="${SECRET_KEY:-dev-only-not-for-production}"
export HOST="${HOST:-0.0.0.0}"

# Read the caller's intent before overwriting it: assigning first made `FOREGROUND=1` a no-op,
# so the documented way to force foreground mode silently ran the local branch instead.
if [ "${FOREGROUND:-0}" = "1" ] || [ -n "${RENDER:-}" ]; then
    FOREGROUND=1
else
    FOREGROUND=0
fi

# A host that assigns a port must win over the local default.
if [ "$FOREGROUND" = "1" ]; then
    export PORT="${PORT:?PORT must be set by the platform}"
else
    export PORT="${PORT:-12000}"
fi

# Default to the checkout's own data directory, which always exists and is writable. A host with
# a mounted disk sets OPPINTEL_DATA_DIR to the mount path to keep state across a redeploy.
export OPPINTEL_DATA_DIR="${OPPINTEL_DATA_DIR:-$REPO_ROOT/data}"

if ! mkdir -p "$OPPINTEL_DATA_DIR" 2>/dev/null || [ ! -w "$OPPINTEL_DATA_DIR" ]; then
    echo "error: cannot write to OPPINTEL_DATA_DIR=$OPPINTEL_DATA_DIR" >&2
    echo "  On Render this means the disk is not attached at that path. Free instances have no" >&2
    echo "  disk, so the mount path is never created. Unset OPPINTEL_DATA_DIR to use the" >&2
    echo "  checkout, or attach a disk and point this at its mount path." >&2
    exit 1
fi

export OPPINTEL_DB="${OPPINTEL_DB:-$OPPINTEL_DATA_DIR/oppintel.db}"
# The public origin, used for canonical links, Open Graph tags and the sitemap. An explicit
# BASE_URL wins; otherwise use the host's own notion of its URL, which is what a platform injects.
# Loopback is the last resort and belongs to a local run only: letting it reach a real deployment
# puts http://127.0.0.1:<port> in every canonical tag and every sitemap entry, and the app's own
# fallback (relative paths when the origin is unknown) is never reached because this sets a value.
export BASE_URL="${BASE_URL:-${RENDER_EXTERNAL_URL:-http://127.0.0.1:${PORT}}}"

# The database may be pointed outside OPPINTEL_DATA_DIR, so check its directory too: sqlite
# creates the file, not the directory, and a missing mount path fails as a raw traceback.
DB_DIR="$(dirname "$OPPINTEL_DB")"
if ! mkdir -p "$DB_DIR" 2>/dev/null || [ ! -w "$DB_DIR" ]; then
    echo "error: cannot write to the database directory $DB_DIR" >&2
    echo "  Unset OPPINTEL_DB and OPPINTEL_DATA_DIR to fall back to the checkout's data/" >&2
    exit 1
fi

# Both commands are idempotent, so this is safe on an existing database.
python -m oppintel.cli initdb >/dev/null
python -m flask --app oppintel.app.wsgi init-app >/dev/null

REFRESH_SECONDS="${REFRESH_SECONDS:-21600}"
MAX_PAGES="${MAX_PAGES:-3}"

if [ "$FOREGROUND" = "1" ]; then
    echo "starting automation in the foreground on ${HOST}:${PORT}, refresh ${REFRESH_SECONDS}s"
    exec python ops/automate.py --serve --refresh-seconds "$REFRESH_SECONDS" \
        --max-pages "$MAX_PAGES"
fi

PID_FILE="$OPPINTEL_DATA_DIR/automation.pid"
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "automation already running (pid $(cat "$PID_FILE"))"
    exit 0
fi

# stdout/stderr go to a separate file: the daemon owns data/automation.log itself.
nohup python ops/automate.py --serve --refresh-seconds "$REFRESH_SECONDS" \
    --max-pages "$MAX_PAGES" > "$OPPINTEL_DATA_DIR/automation.out" 2>&1 &
echo $! > "$PID_FILE"
echo "automation started (pid $(cat "$PID_FILE")) on ${HOST}:${PORT}, refresh ${REFRESH_SECONDS}s"
