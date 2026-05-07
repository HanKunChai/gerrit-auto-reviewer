#!/bin/bash
# Run all tests for the Gerrit MCP Auto-Review System
set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD"

echo "=== Running all tests ==="
echo ""

python -m pytest tests/ -v --tb=short "$@"
