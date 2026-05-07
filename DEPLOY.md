# Gerrit MCP Auto-Review System — 内网部署指南

## 前置要求

| 组件 | 版本要求 | 说明 |
|------|---------|------|
| Python | >= 3.10 | 运行 MCP Server |
| Git | >= 2.30 | 本地仓库操作 |
| pip | - | 安装 Python 依赖 |

## 快速部署

### 1. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 2. 配置连接

编辑 `config.yaml`，修改 Gerrit 地址和账号：

```yaml
mode: "production"

gerrit:
  base_url: "https://内网gerrit地址"   # ← 改为实际地址
  auth:
    username: "code-reviewer"          # ← 改为实际账号

repo:
  local_path: "./local-repo"
  # remote_url 自动从 gerrit.base_url + 项目名拼接
  initial_clone_depth: 10
```

### 3. 设置密码

**方法一：`.env` 文件（推荐，无需每次设置）**

在项目根目录创建 `.env` 文件：

```ini
GERRIT_PASSWORD=你的HTTP密码
```

**方法二：环境变量**

```bash
# Windows CMD（命令提示符）
set GERRIT_PASSWORD=你的HTTP密码

# Windows PowerShell
$env:GERRIT_PASSWORD = "你的HTTP密码"

# Git Bash / Linux
export GERRIT_PASSWORD="你的HTTP密码"
```

> **重要：** 此密码不是 Gerrit 登录密码！
> 需要在 Gerrit Web 界面 → Settings → HTTP Password → 点击 **Generate/Regenerate Password**
> 生成的随机字符串才是 HTTP API 密码。

### 4. 启动服务

```bash
# 前台运行（调试用，可看到详细日志）
python -m mcp_gerrit_server.server --verbose

# 后台运行（Windows 用 start /B）
start /B python -m mcp_gerrit_server.server > server.log
```

首次启动会自动：
1. 连接 Gerrit REST API 验证认证
2. 检测到新 change 后自动 `git clone` 本地仓库
3. 执行代码评审并提交结果

### 5. 验证

```python
# 验证 Gerrit 连接和认证
python -c "
import asyncio
from mcp_gerrit_server.server import GerritReviewServer
svr = GerritReviewServer()
changes = asyncio.run(svr._handle_list_changes({'status': 'open', 'limit': 5}))
print(f'待评审 changes: {len(changes)}')
for c in changes:
    print(f'  #{c[\"_number\"]}: {c[\"subject\"][:50]}')
"

# 运行单元测试
python -m pytest tests/ -v
```

> 如果认证失败，会提示 `Authentication failed (HTTP 401)`。
> 请检查：① Gerrit 是否生成了 HTTP Password ② `.env` 文件或环境变量是否正确

## 常见认证问题

| 现象 | 原因 | 解决 |
|------|------|------|
| HTTP 401 | 密码错误或未设置 | 生成 HTTP Password，写入 `.env` |
| HTTP 404 | URL 或项目不存在 | 检查 `gerrit.base_url` 配置 |
| 连接超时 | 内网 DNS/网络不通 | `ping 内网gerrit地址` 检查网络 |
| SSL 证书错误 | 内网 HTTPS 证书自签 | 设置 `REQUESTS_CA_BUNDLE` 或联系 IT |

## 自动化评审流程

系统通过轮询自动工作：

```yaml
auto_review:
  poll_interval: 120   # 每120秒检查一次
  reviewer: "code-reviewer"  # 当此用户被加为 reviewer 时触发
```

开发者操作流程：
1. `git commit` 提交代码
2. `git review` 推送到 Gerrit
3. 在 Gerrit Web UI 上将 `code-reviewer` 添加为 Reviewer
4. 系统自动拉取 diff → 规则引擎扫描 → Claude 深度评审 → 提交评分

## 手动触发评审

```bash
python scripts/auto_review.py --change-id 12345 --base-branch master
cat reviews/review_12345_*.json
```

## 本地测试模式（无需 Gerrit）

```bash
bash scripts/setup-test-env.sh
python -m mcp_gerrit_server.server --mock --verbose
python scripts/auto_review.py --mock --change-id test-001 --base-branch base
```

## 目录结构

```
Gerrit-Auto-Viewer/
├── mcp_gerrit_server/    # MCP Server 核心
│   ├── server.py         # 7 个 MCP 工具
│   ├── config.py         # 配置（支持 .env 文件）
│   ├── gerrit_client.py  # Gerrit REST API
│   ├── mock_api.py       # 模拟 Gerrit
│   ├── local_repo.py     # 本地仓库（自动 clone）
│   └── cache.py          # 评审缓存
├── review_rules/         # 17 条 C 语言规则
├── review_prompts/       # 中文评审提示词
├── scripts/              # 辅助脚本
├── tests/                # 60 个单元测试
├── config.yaml           # 主配置
├── .env                  # 密码配置（需手动创建）
└── USER_GUIDE.md         # 用户手册
```
