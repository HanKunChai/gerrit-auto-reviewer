#!/bin/bash
# Gerrit local test environment
# Usage: source scripts/gerrit-env.sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# 本地测试 Gerrit 的临时口令 (由 scripts/gerrit-setup-test.sh 创建, 仅限 localhost 测试环境)
# 生产环境请通过外部环境变量 GERRIT_PASSWORD 覆盖
export GERRIT_PASSWORD="${GERRIT_PASSWORD:-admin}"
export JAVA_HOME="$PROJECT_DIR/tools/jdk17"
echo "Gerrit test environment configured"
echo "  Java: $JAVA_HOME"
echo "  Config: $PROJECT_DIR/local-gerrit-config.yaml"
echo "  Gerrit: http://localhost:8080"
