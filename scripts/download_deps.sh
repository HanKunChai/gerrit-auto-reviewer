#!/bin/bash
# ============================================================================
# download_deps.sh — 下载 Python 离线依赖包 (Linux x86_64)
#
# 用法:
#   在外网机器上运行此脚本，它会将 Linux x86_64 兼容的 wheel 包下载到
#   ../offline-packages/ 目录。然后把 offline-packages 整个目录拷贝到
#   内网 Linux 服务器。
#
#   注意: 需要 pip >= 20.0 才能使用 --platform 参数。
#         如在外网 Linux 机器上运行，会自动编译平台相关包。
# ============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REQUIREMENTS="$PROJECT_DIR/requirements.txt"
OUTPUT_DIR="$PROJECT_DIR/offline-packages"

echo "=== Gerrit Auto-Review 离线依赖包下载 ==="
echo "项目目录:   $PROJECT_DIR"
echo "输出目录:   $OUTPUT_DIR"
echo ""

if [ ! -f "$REQUIREMENTS" ]; then
    echo "错误: 找不到 requirements.txt"
    exit 1
fi

mkdir -p "$OUTPUT_DIR"

# ------------------------------------------------------------------
# 检测是否在 Linux 上运行
# ------------------------------------------------------------------
IS_LINUX=false
if [ "$(uname -s)" = "Linux" ]; then
    IS_LINUX=true
fi

# ------------------------------------------------------------------
# 下载依赖
# ------------------------------------------------------------------
if [ "$IS_LINUX" = true ]; then
    # 在 Linux 上: 直接用 pip wheel 构建本地 wheel
    echo "[1/2] 在 Linux 上构建 wheel 包..."
    pip3 wheel \
        -r "$REQUIREMENTS" \
        --wheel-dir "$OUTPUT_DIR" \
        --no-cache-dir
else
    # 在非 Linux (Windows/Mac) 上: 下载预编译的 manylinux wheel
    echo "[1/2] 下载 Linux x86_64 (manylinux) 预编译 wheel 包..."
    pip download \
        --platform manylinux2014_x86_64 \
        --platform manylinux_2_17_x86_64 \
        --only-binary=:all: \
        -r "$REQUIREMENTS" \
        -d "$OUTPUT_DIR" \
        --no-cache-dir \
        2>&1 || echo "(部分包可能没有预编译 wheel，将下载源码包)"

    # 补充下载没有预编译 wheel 的源码包
    echo "[2/2] 补充下载源码包..."
    pip download \
        --no-binary=:all: \
        -r "$REQUIREMENTS" \
        -d "$OUTPUT_DIR" \
        --no-cache-dir \
        2>&1 || true
fi

# ------------------------------------------------------------------
# 结果统计
# ------------------------------------------------------------------
echo ""
echo "=== 下载完成 ==="
WHEEL_COUNT=$(ls "$OUTPUT_DIR"/*.whl 2>/dev/null | wc -l)
SRC_COUNT=$(ls "$OUTPUT_DIR"/*.tar.gz 2>/dev/null | wc -l)
ZIP_COUNT=$(ls "$OUTPUT_DIR"/*.zip 2>/dev/null | wc -l)
echo "  Wheel 包:   $WHEEL_COUNT"
echo "  源码包:     $SRC_COUNT ($((SRC_COUNT + ZIP_COUNT)) total)"
echo "  总大小:     $(du -sh "$OUTPUT_DIR" | cut -f1)"
echo ""
echo "请将 offline-packages 目录拷贝到内网 Linux 服务器。"
echo "在内网服务器上运行: cd gerrit-auto-review && bash install.sh"
