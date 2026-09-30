#!/usr/bin/env bash
# Stop the automation daemon and the web server it supervises.
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PID_FILE="data/automation.pid"
if [ ! -f "$PID_FILE" ]; then
    echo "no automation pid file; nothing to stop"
    exit 0
fi

PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    echo "sent SIGTERM to automation pid $PID"
else
    echo "automation pid $PID not running"
fi
rm -f "$PID_FILE"
