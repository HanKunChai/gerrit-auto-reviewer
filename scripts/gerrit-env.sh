#!/bin/bash
# Gerrit local test environment
# Usage: source scripts/gerrit-env.sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
export GERRIT_PASSWORD="admin"
export JAVA_HOME="$PROJECT_DIR/tools/jdk17"
echo "Gerrit test environment configured"
echo "  Java: $JAVA_HOME"
echo "  Config: $PROJECT_DIR/local-gerrit-config.yaml"
echo "  Gerrit: http://localhost:8080"
