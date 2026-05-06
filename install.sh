#!/bin/bash
# ============================================================================
# install.sh — Gerrit Auto-Review Linux 部署安装脚本
#
# 前提:
#   - Python 3.8+ (目标环境为 3.8.10)
#   - git
#
# 用法:
#   # 方式 1: 内网有 PyPI 镜像 (推荐)
#   export PIP_INDEX_URL=http://你的镜像地址/simple
#   export PIP_TRUSTED_HOST=你的镜像主机
#   bash install.sh
#
#   # 方式 2: 有离线包
#   bash install.sh
#
#   # 方式 3: 直接在线安装 (如外网有权限)
#   bash install.sh
# ============================================================================

set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

echo "=== Gerrit Auto-Review Linux 部署 ==="
echo "目录: $PROJECT_DIR"
echo ""

# ------------------------------------------------------------------
# 0. 检查环境
# ------------------------------------------------------------------
echo "[0/5] 检查环境..."

PYTHON_VERSION=$(python3 --version 2>&1)
echo "  Python: $PYTHON_VERSION"

if ! command -v git &>/dev/null; then
    echo "  错误: 需要安装 git"
    exit 1
fi
echo "  git:    $(git --version)"

# 检查 PyPI 镜像配置
if [ -n "$PIP_INDEX_URL" ]; then
    echo "  PyPI 镜像: $PIP_INDEX_URL"
elif [ -f "$HOME/.pip/pip.conf" ]; then
    echo "  pip.conf:  $HOME/.pip/pip.conf"
elif [ -f "/etc/pip.conf" ]; then
    echo "  pip.conf:  /etc/pip.conf"
else
    echo "  PyPI 源:   默认 (pypi.org)"
fi

# ------------------------------------------------------------------
# 1. 配置 .env
# ------------------------------------------------------------------
echo ""
echo "[1/5] 配置 .env..."
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo "  已从 .env.example 创建 .env"
    else
        cat > .env << 'EOF'
GERRIT_PASSWORD=your_http_password_here
EOF
        echo "  已创建 .env"
    fi
    echo "  ⚠ 请编辑 .env 填入 GERRIT_PASSWORD (Gerrit HTTP 密码)"
else
    echo "  .env 已存在，跳过"
fi

# ------------------------------------------------------------------
# 2. 安装 Python 依赖
# ------------------------------------------------------------------
echo ""
echo "[2/5] 安装 Python 依赖..."

PIP_ARGS="-r requirements.txt --no-cache-dir"

# 检测可用安装方式
OFFLINE_DIR="$PROJECT_DIR/offline-packages"
if [ -d "$OFFLINE_DIR" ] && [ -n "$(ls -A "$OFFLINE_DIR" 2>/dev/null)" ]; then
    echo "  方式: 离线安装 (offline-packages)"
    pip3 install --no-index --find-links "$OFFLINE_DIR" $PIP_ARGS
elif [ -n "$PIP_INDEX_URL" ]; then
    echo "  方式: 内网 PyPI 镜像"
    pip3 install $PIP_ARGS
else
    echo "  方式: 在线 PyPI (若无外网权限会失败)"
    echo "  提示: 设置 PIP_INDEX_URL 使用内网镜像"
    pip3 install $PIP_ARGS
fi
echo "  ✅ 依赖安装完成"

# ------------------------------------------------------------------
# 3. 创建运行时目录
# ------------------------------------------------------------------
echo ""
echo "[3/5] 创建运行时目录..."
mkdir -p local-repo reviews .cache/review-cache logs

echo "  ✅ 目录已创建:"
echo "    - local-repo/          (git 仓库缓存)"
echo "    - reviews/             (评审结果)"
echo "    - .cache/review-cache/ (缓存)"
echo "    - logs/                (日志)"

# ------------------------------------------------------------------
# 4. 配置 PyPI 镜像 (可选)
# ------------------------------------------------------------------
echo ""
echo "[4/5] 配置 PyPI 镜像 (可选)..."
if [ -z "$PIP_INDEX_URL" ] && [ ! -f "$HOME/.pip/pip.conf" ] && [ ! -f "/etc/pip.conf" ]; then
    echo "  需要配置内网 PyPI 镜像? [y/N]"
    read -r answer
    if [ "$answer" = "y" ] || [ "$answer" = "Y" ]; then
        echo "  请输入 PyPI 镜像地址 (如 http://mirror.internal/simple):"
        read -r mirror_url
        if [ -n "$mirror_url" ]; then
            mkdir -p "$HOME/.pip"
            cat > "$HOME/.pip/pip.conf" << EOF
[global]
index-url = $mirror_url
trusted-host = $(echo "$mirror_url" | sed -E 's|https?://([^/]+).*|\1|')
EOF
            echo "  ✅ 已写入 $HOME/.pip/pip.conf"
        fi
    fi
fi

# ------------------------------------------------------------------
# 5. 验证安装
# ------------------------------------------------------------------
echo ""
echo "[5/5] 验证安装..."
python3 -c "
from mcp_gerrit_server.config import load_config
cfg = load_config()
print(f'  配置加载: OK (mode={cfg.mode})')
print(f'  Gerrit:   {cfg.gerrit.base_url}')
print(f'  用户:     {cfg.gerrit.username}')
print(f'  密码:     {\"已配置\" if cfg.gerrit.password else \"未配置\"}')

import flask, yaml, requests
print(f'  Flask:    {flask.__version__}')
print(f'  PyYAML:   {yaml.__version__}')
print(f'  requests: {requests.__version__}')
"
echo "  ✅ 验证完成"

# ------------------------------------------------------------------
# 完成
# ------------------------------------------------------------------
echo ""
echo "=== 部署完成 ==="
echo ""
echo "下一步:"
echo "  1. 编辑 .env 填入 GERRIT_PASSWORD"
echo "  2. 检查 config.yaml 配置"
echo "  3. 诊断认证: python3 scripts/diagnose_auth.py"
echo "  4. 启动服务:  bash run.sh"
