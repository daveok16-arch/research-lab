#!/usr/bin/env bash
# Start the oppintel web app and its persistent online-search automation.
#
# Runs the automation daemon (refresh loop + web server supervisor) in the background
# and records its PID so stop.sh can find it. Safe to re-run: an already-running daemon
# is detected and left alone.
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"
export SECRET_KEY="${SECRET_KEY:-dev-only-not-for-production}"
export OPPINTEL_DB="${OPPINTEL_DB:-data/oppintel.db}"
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-12000}"
export BASE_URL="${BASE_URL:-http://127.0.0.1:${PORT}}"

mkdir -p data
PID_FILE="data/automation.pid"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "automation already running (pid $(cat "$PID_FILE"))"
    exit 0
fi

# Both commands are idempotent, so this is safe on an existing database.
python -m oppintel.cli initdb >/dev/null
python -m flask --app oppintel.app.wsgi init-app >/dev/null

REFRESH_SECONDS="${REFRESH_SECONDS:-21600}"
MAX_PAGES="${MAX_PAGES:-3}"

# stdout/stderr go to a separate file: the daemon owns data/automation.log itself.
nohup python ops/automate.py --serve --refresh-seconds "$REFRESH_SECONDS" \
    --max-pages "$MAX_PAGES" > data/automation.out 2>&1 &
echo $! > "$PID_FILE"
echo "automation started (pid $(cat "$PID_FILE")) on ${HOST}:${PORT}, refresh ${REFRESH_SECONDS}s"
