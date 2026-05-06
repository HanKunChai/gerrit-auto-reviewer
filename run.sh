#!/bin/bash
# ============================================================================
# run.sh — 启动 Gerrit Auto-Review
#
# 两种模式：
#   1. MCP 服务 (默认): 由 Claude Code 等 MCP 客户端调用
#   2. 自动轮询: 持续查询 Gerrit 并自动执行评审
#
# 用法:
#   bash run.sh                    # MCP 服务模式 (默认)
#   bash run.sh --poller           # 自动轮询模式
#   bash run.sh --poller --once    # 轮询模式：只跑一轮
#   bash run.sh --poller --verbose # 轮询模式：详细日志
#   bash run.sh --mock             # Mock 模式测试
#   bash run.sh --verbose          # MCP 服务 + 详细日志
# ============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 加载 .env (如果存在)
if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

# 确保日志目录存在
mkdir -p logs

# 检测模式
if [ "$1" = "--poller" ] || [ "$1" = "--poller-once" ]; then
    # ---- 自动轮询评审模式 ----
    POLLER_ARGS=""
    if [ "$1" = "--poller-once" ]; then
        POLLER_ARGS="$POLLER_ARGS --once"
    fi
    shift

    while [ $# -gt 0 ]; do
        case "$1" in
            --once)      POLLER_ARGS="$POLLER_ARGS --once" ;;
            --mock)      POLLER_ARGS="$POLLER_ARGS --mock" ;;
            --verbose|-v) POLLER_ARGS="$POLLER_ARGS --verbose" ;;
            --config)    POLLER_ARGS="$POLLER_ARGS --config $2"; shift ;;
            --interval)  POLLER_ARGS="$POLLER_ARGS --interval $2"; shift ;;
            *)           echo "未知参数: $1"; exit 1 ;;
        esac
        shift
    done

    echo "Starting Gerrit Auto-Review Poller..." >&2
    exec python3 scripts/auto_review_poller.py $POLLER_ARGS

else
    # ---- MCP 服务模式 (默认) ----
    MOCK=""
    CONFIG=""
    VERBOSE=""

    while [ $# -gt 0 ]; do
        case "$1" in
            --mock)      MOCK="--mock" ;;
            --config)    CONFIG="--config $2"; shift ;;
            --verbose|-v) VERBOSE="--verbose" ;;
            *)           echo "未知参数: $1"; exit 1 ;;
        esac
        shift
    done

    echo "Starting Gerrit Auto-Review MCP server..." >&2
    exec python3 -m mcp_gerrit_server.server $MOCK $CONFIG $VERBOSE
fi
