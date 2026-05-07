#!/bin/bash
# Stop local Gerrit instance
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -f "$PROJECT_DIR/gerrit.pid" ]; then
    PID=$(cat "$PROJECT_DIR/gerrit.pid")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Stopping Gerrit (PID: $PID)..."
        kill "$PID"
        sleep 2
        if kill -0 "$PID" 2>/dev/null; then
            kill -9 "$PID" 2>/dev/null || true
        fi
        echo "Gerrit stopped."
    else
        echo "Gerrit not running."
    fi
    rm -f "$PROJECT_DIR/gerrit.pid"
else
    echo "No PID file found."
fi

# Also try graceful shutdown via Gerrit SSH command if available
if command -v ssh &> /dev/null; then
    ssh -p 29418 localhost gerrit shutdown 2>/dev/null || true
fi
