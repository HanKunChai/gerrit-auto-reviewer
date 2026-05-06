#!/bin/bash
# Auto-review script — delegates to Python CLI for safety.
#
# Usage:
#   ./scripts/auto-review.sh [--mock] <change-id> [revision] [base-branch]
#
# Examples:
#   ./scripts/auto-review.sh 12345
#   ./scripts/auto-review.sh --mock 12345
#   ./scripts/auto-review.sh 12345 2 main

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

MOCK=""
CHANGE_ID=""
REVISION="1"
BASE_BRANCH="master"

for arg in "$@"; do
    case "$arg" in
        --mock) MOCK="--mock" ;;
        --help|-h)
            echo "Usage: $0 [--mock] <change-id> [revision] [base-branch]"
            exit 0
            ;;
        *)
            if [ -z "$CHANGE_ID" ]; then
                CHANGE_ID="$arg"
            elif [ "$REVISION" = "1" ]; then
                REVISION="$arg"
            else
                BASE_BRANCH="$arg"
            fi
            ;;
    esac
done

if [ -z "$CHANGE_ID" ]; then
    echo "Error: change-id is required"
    echo "Usage: $0 [--mock] <change-id> [revision] [base-branch]"
    exit 1
fi

# Delegate to Python CLI entry point (safe — no shell injection)
cd "$PROJECT_DIR"
exec python scripts/auto_review.py \
    --change-id "$CHANGE_ID" \
    --revision "$REVISION" \
    --base-branch "$BASE_BRANCH" \
    $MOCK
