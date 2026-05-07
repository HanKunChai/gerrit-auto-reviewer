# GERA — Gerrit Auto-Review 系统详细设计方案

> 版本: v4 / 2026-05-06

---

## 1. 系统概述

GERA (Gerrit Auto-Review) 是一个面向内网 Gerrit 代码评审平台的自动化代码审查系统。系统持续轮询 Gerrit 中待处理的变更，获取 diff 后依次通过本地规则引擎和 Claude Code CLI 进行代码评审，最终将评审结果自动提交回 Gerrit。

### 1.1 设计目标

| 目标 | 说明 |
|------|------|
| 自动化 | 零人工干预，持续轮询 Gerrit 待处理变更并自动提交评审意见 |
| 双引擎评审 | 本地正则规则引擎（确定性检查）+ AI 语义评审（Claude Code）互补 |
| 并发安全 | 多线程评审不互相干扰，不产生数据竞争 |
| 内网兼容 | 纯 Python 3.8+，Flask + PyYAML + requests 三依赖，无外部 AI API 调用 |
| 幂等 | 同一变更同一版本不会重复评审（REVIEW_TAG + _already_reviewed 双重校验） |

### 1.2 约束

| 约束 | 值 |
|------|-----|
| Python 版本 | >= 3.8（目标环境 3.8.10） |
| 第三方依赖 | flask, pyyaml, requests（仅三包） |
| AI 评审 | Claude Code CLI（claude 或 claude-code 命令），闭源内网部署 |
| 目标代码 | 嵌入式 C 语言（当前），prompt 可扩展 |
| 并发模型 | ThreadPoolExecutor，默认 4 workers |
| 评审失败处理 | 超时 / max-turns / 异常 → 跳过，不提交 Gerrit，只写日志 |

---

## 2. 系统架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                      GERA 系统架构图                                 │
└─────────────────────────────────────────────────────────────────────┘

                         ┌──────────────┐
                         │   Gerrit     │
                         │   REST API   │
                         └──────┬───────┘
                                │ HTTP Basic Auth
                                │
              ┌─────────────────┼─────────────────┐
              ▼                 ▼                 ▼
     ┌────────────────┐ ┌──────────────┐ ┌────────────────┐
     │   Poller       │ │  MCP Server  │ │  git fetch     │
     │ (auto_review_  │ │  (server.py) │ │ refs/changes/* │
     │  poller.py)    │ │              │ └────────┬───────┘
     └───────┬────────┘ └──────┬───────┘          │
             │                 │                  ▼
             │                 │           ┌────────────────┐
             │                 │           │  LocalRepo     │
             │                 │           │  (shallow      │
             │                 │           │   clone)       │
             │                 │           └───────┬────────┘
             │                 │                   │
             ▼                 ▼                   ▼
     ┌─────────────────────────────────────────────────────┐
     │                Review Pipeline                      │
     │  ┌──────────┐  ┌──────────┐  ┌───────────────────┐  │
     │  │ Rules    │  │  Claude  │  │  _build_review_   │  │
     │  │ Engine   │  │  Code    │  │  message()        │  │
     │  │ (regex)  │  │  (CLI)   │  │                   │  │
     │  └──────────┘  └──────────┘  └──────────┬────────┘  │
     │                                         │           │
     └─────────────────────────────────────────┼───────────┘
                                               │
                                               ▼
                                        ┌──────────────┐
                                        │  Gerrit POST │
                                        │  /review     │
                                        └──────────────┘
```

### 2.1 组件清单

| 组件 | 文件 | 职责 |
|------|------|------|
| Poller | `scripts/auto_review_poller.py` | 主入口：轮询 Gerrit → 调度评审 → 提交结果 |
| MCP Server | `mcp_gerrit_server/server.py` | MCP 协议服务，8 个工具供 Claude Code MCP 客户端调用 |
| Gerrit Client | `mcp_gerrit_server/gerrit_client.py` | Gerrit REST API HTTP 客户端（Basic Auth） |
| Local Repo | `mcp_gerrit_server/local_repo.py` | 本地 git 仓库管理（shallow clone + 按需 fetch） |
| Config | `mcp_gerrit_server/config.py` | YAML 配置加载 + .env 文件支持 |
| Cache | `mcp_gerrit_server/cache.py` | 评审结果缓存（TTL + 文件持久化） |
| Mock API | `mcp_gerrit_server/mock_api.py` | Flask mock Gerrit REST API（本地测试用） |
| MCP Compat | `mcp_gerrit_server/mcp_compat.py` | 轻量 MCP 协议实现（替代 mcp 包） |
| Rules Engine | `review_rules/engine.py` | diff 解析 + 正则规则匹配引擎 |
| Rules Loader | `review_rules/loader.py` | YAML 规则文件扫描、加载、校验 |
| Rules (builtin) | `review_rules/builtin/*.yaml` | 17 条 C 语言内置规则 |
| Prompts | `review_prompts/*.md` | Claude Code 评审提示词模板 |

---

## 3. 数据流详解

### 3.1 轮询流程（主路径）

```
poll_and_submit()
  │
  ├─ Gerrit REST: GET /changes/?q=reviewer:code-reviewer+status:open
  │
  ├─ 过滤：跳过自己的提交 (account_id / username 匹配)
  ├─ 过滤：跳过 _in_flight（正在评审中）
  ├─ 过滤：跳过 _already_reviewed（Gerrit API 查 messages tag）
  │
  └─ ThreadPoolExecutor.submit(_review_task, change)
       │
       └─ _review_change()
            │
            ├─ [1/4] GerritClient.get_change() → 获取项目/分支/commit message
            ├─ [1/4] LocalRepo.ensure_branch(base) → 确保 base branch 存在
            ├─ [1/4] LocalRepo.fetch_change() → git fetch → refs/review/* SHA
            ├─ [1/4] LocalRepo.get_diff(base, head_ref=SHA) → unified diff
            ├─ [1/4] LocalRepo.list_changed_files(base, head_ref=SHA) → file list
            │
            ├─ [2/4] ReviewEngine.run(diff) → list[Issue]（可选，默认关闭）
            │
            ├─ [3/4] _invoke_claude()
            │    ├─ 构造 prompt（模板 + 问题/方案 + 文件列表 + git diff SHA）
            │    ├─ subprocess: claude -p "..." --dangerously-skip-permissions --max-turns 10
            │    ├─ 等待 stdout + 异步读 stderr
            │    ├─ 失败检测：timeout / returncode != 0 / 空输出 / max-turns
            │    └─ 返回 Claude 评审文本
            │
            ├─ [4/4] _build_review_message() → Markdown 格式评审意见
            │
            ├─ [5/5] GerritClient.post_review()
            │    └─ POST /changes/{id}/revisions/{rev}/review
            │       { message, labels: {Code-Review: 0}, tag, notify: "OWNER" }
            │
            └─ LocalRepo.cleanup_review_ref() → 删除 refs/review/* 临时 ref
```

### 3.2 失败处理路径

```
_invoke_claude 返回 None 的情况：
  ├─ claude 命令未找到 → return None
  ├─ subprocess.TimeoutExpired → kill + communicate → log → return None
  ├─ proc.returncode != 0 → log → return None
  ├─ stdout 为空 → log → return None
  └─ stderr/stdout 匹配 max-turns pattern → log → return None

_review_change 处理 None：
  if claude_review is None:
      logger.error("...评审失败，跳过上报")
      return  ← 不调 Gerrit post_review
```

### 3.3 Git 操作并发安全

```
问题：多个线程共享同一个 git 工作目录，FETCH_HEAD 会被覆盖

方案：每个 review ref 使用独立命名空间
  - fetch_change(change_number, rev_num)
    → git fetch gerrit refs/changes/{xx}/{num}/{rev}:refs/review/{num}/{rev}
    → git rev-parse refs/review/{num}/{rev}
    → 返回稳定 SHA（而非依赖 FETCH_HEAD）
  - 所有后续操作（get_diff、list_changed_files）均使用该 SHA
  - 评审完成后 cleanup_review_ref() 删除临时 ref

保证：
  - 即使 4 个线程同时 fetch 不同 change，refs/review/* 互不覆盖
  - SHA 一经捕获即固定，不受后续 fetch 影响
```

---

## 4. 组件详细设计

### 4.1 Config (`mcp_gerrit_server/config.py`)

配置加载顺序：
1. `.env` 文件（`GERRIT_PASSWORD` 等敏感信息）
2. `config.yaml`（结构化配置）
3. 环境变量（覆盖 `.env`）

```yaml
mode: "local_test"          # local_test | production
gerrit:
  base_url: "http://..."
  auth:
    username: "code-reviewer"
auto_review:
  poll_interval: 600        # 秒
  reviewer: "code-reviewer"
  query: "reviewer:code-reviewer+status:open"
  max_changes_per_poll: 10
  use_rules_engine: false   # 规则引擎默认关闭
  max_concurrent_reviews: 4 # 最大并发线程
  claude_timeout: 1800      # Claude 超时秒数（30分钟）
```

### 4.2 Gerrit Client (`mcp_gerrit_server/gerrit_client.py`)

- 基于 `requests.Session`，支持 HTTP Basic Auth
- Gerrit 要求认证请求使用 `/a/` URL 前缀（配置项 `use_a_prefix` 控制）
- 内置 `GERRIT_MAGIC_PREFIX = ")]}'\\n"` 自动剥离
- 异常层次：`GerritAuthError(401)` / `GerritConnectionError` / `GerritApiError`
- 关键方法：`list_changes` / `get_change` / `fetch_files` / `fetch_patch` / `post_review` / `get_change_detail`

`post_review` 请求体：
```json
{
  "message": "## 自动代码评审结果\n...",
  "labels": {"Code-Review": 0},
  "comments": {},
  "tag": "autogenerated:gerrit:auto-review:v4",
  "notify": "OWNER"
}
```

### 4.3 Local Repo (`mcp_gerrit_server/local_repo.py`)

仓库管理方案：
- 每个 Gerrit project 对应一个本地 shallow clone（`./local-repo/{project}/`）
- 首次 `ensure_clone()` 执行 `git clone --depth 10`
- 按需 `ensure_branch()` fetch 非默认分支
- `fetch_change()` 从 Gerrit remote 拉取 `refs/changes/{last2}/{id}/{rev}` 到本地 `refs/review/{id}/{rev}`
- `get_diff()` 使用 `git diff {base}...{sha}` 三点语法
- `list_changed_files()` 使用 `git diff --name-status {base}...{sha}`
- 评审完成后 `cleanup_review_ref()` 删除临时 ref
- 可选自动 deepen（`auto_deepen: true`）

### 4.4 评审消息构造 (`_build_review_message`)

输出格式（Markdown，约 500-3000 字符）：

```markdown
## 自动代码评审结果

**变更**: Iabc1234
**标题**: Fix buffer overflow in config parser

### Claude Code 评审
[已解决] 变更正确使用 snprintf 替代 sprintf 修复了缓冲区溢出问题

src/config.c:15 [error] 使用 snprintf 而非 sprintf，正确

### 规则引擎检查
**摘要**: 2 个问题 (1 errors, 1 warnings)

#### 错误
- **C-SEC-001** `src/config.c:15` 使用 strcpy...

---
*由 Gerrit Auto-Review + Claude Code 自动生成*
```

### 4.5 Claude 调用 (`_invoke_claude`)

#### 进程模型
```
subprocess.Popen(
    [claude, -p, prompt, --dangerously-skip-permissions, --max-turns, 10],
    stdout=PIPE, stderr=PIPE, cwd=repo_dir, env={..., CLAUDE_CODE_SKIP_AUTH=1}
)
```

| 参数 | 值 | 说明 |
|------|-----|------|
| `-p` | `{prompt_template}\n\n问题: {problem}\n方案: {solution}\n文件: {files}\ndiff: git diff {base}...{sha}` | 完整提示词 |
| `--max-turns` | 10 | Claude 最大交互轮次，>=10 视为失败 |
| `--dangerously-skip-permissions` | - | 跳过权限确认提示 |
| `cwd` | repo_dir | 在 git 仓库目录执行，Claude 可读文件 |
| timeout | `config.claude_timeout` (default 1800s) | 进程级超时 |

#### Prompt 构造

模板: `review_prompts/c_review_prompt_fast.md`
- 问题解决评估（第一行输出 `[已解决/部分解决/未解决]`）
- 安全检查（strcpy/strcat/sprintf/gets/scanf、system 注入、内存安全）
- 编码规范（Allman 大括号、行长 ≤ 200、变量初始化、单变量/行）
- 逻辑检查（数组越界、返回值检查、循环终止）
- 输出最多 10 条 issue，Claude 在 repo 目录内自行 `git diff {base}...{sha}`

#### 失败检测层次

```
1. claude 命令未安装     → return None
2. subprocess 超时       → return None
3. returncode != 0       → return None
4. stdout 为空           → return None
5. max-turns 命中        → return None（正则检测 stderr/stdout）
   pattern: max.*turn | turn.*limit.*reach | limit.*reach
6. 成功                  → return stdout
```

### 4.6 规则引擎 (`review_rules/engine.py`)

#### 内置规则（17 条）

| ID | 类别 | 严重度 | 描述 |
|----|------|--------|------|
| C-SEC-001 | security | error | strcpy 缓冲区溢出 |
| C-SEC-002 | security | error | strcat 缓冲区溢出 |
| C-SEC-003 | security | error | sprintf 缓冲区溢出 |
| C-SEC-004 | security | error | gets 不安全 |
| C-SEC-005 | security | warning | scanf 不安全 |
| C-SEC-006 | security | warning | system() 命令注入 |
| C-STYLE-001 | coding_style | error | 函数大括号非 Allman 风格 |
| C-STYLE-002 | coding_style | error | 行超过 200 字符 |
| C-STYLE-003 | coding_style | info | 变量未初始化定义 |
| C-STYLE-004 | coding_style | warning | 单行多变量定义 |
| C-STYLE-005 | coding_style | warning | 局部变量与全局同名 |
| C-BUG-001 | common_bugs | error | 空指针解引用 |
| C-BUG-002 | common_bugs | error | 缓冲区溢出（变量 size） |
| C-BUG-003 | common_bugs | error | free 后使用 |
| C-BUG-004 | common_bugs | warning | 内存泄漏（alloc 无 free 检查） |
| C-BUG-005 | common_bugs | warning | 除以变量（零除风险） |
| C-BUG-006 | common_bugs | info | 数组索引 off-by-one |

#### Diff 解析算法

```
输入: unified diff 文本
1. 逐行扫描
2. `+++ b/<path>` → 设置当前文件
3. `@@ -old,count +new,count @@` → 设置当前行号
4. `+内容` → 记录 (当前行号, 内容)
5. `-内容` → 跳过（只检查新增行）
6. ` 内容` → 上下文行，仅递增行号
输出: {file_path: [(line_no, content), ...]}
```

#### 规则匹配

- 每行配对所有适用规则（按 include/exclude 文件 glob 过滤）
- 仅支持 `regex` 类型 pattern
- 无效 pattern 静默跳过

### 4.7 MCP 服务 (`mcp_gerrit_server/server.py`)

提供 8 个 MCP 工具：

| 工具 | 功能 |
|------|------|
| `list_changes` | 列出 Gerrit 待处理变更 |
| `fetch_diff` | 获取指定变更的 diff |
| `get_file_context` | 获取仓库中指定文件内容 |
| `post_review` | 提交评审意见 |
| `repo_status` | 查看本地仓库状态 |
| `run_rules` | 运行规则引擎 |
| `c_review_prompt` | 获取 C 评审提示词模板 |

MCP 协议为自定义轻量实现（`mcp_compat.py`），JSON-RPC over stdio，无需 `mcp` 包依赖。

---

## 5. 启动方式

### MCP 服务模式
```bash
python -m mcp_gerrit_server.server [--mock] [--verbose]
```
等待 Claude Code 等 MCP 客户端连接，通过工具调用被动执行评审。

### 自动轮询模式
```bash
python scripts/auto_review_poller.py [--once] [--mock] [--verbose]
```
持续轮询 Gerrit → 异步评审 → 自动提交，无需外部客户端。

两种模式互斥，通过 `run.sh` 统一入口切换。

---

## 6. 配置参考

### config.yaml 完整字段

```yaml
mode: "local_test"                    # local_test | production
gerrit:
  base_url: "http://host:8081"
  auth:
    username: "code-reviewer"
  # GERRIT_PASSWORD 从 .env 或环境变量读取

auto_review:
  enabled: true                       # 总开关
  poll_interval: 600                  # 轮询间隔（秒）
  reviewer: "code-reviewer"                  # reviewer 用户名
  query: "reviewer:code-reviewer+status:open"
  max_changes_per_poll: 10            # 每次轮询最大变更数
  use_rules_engine: false             # 规则引擎开关
  max_concurrent_reviews: 4           # 并发评审线程数
  claude_timeout: 1800                # Claude 超时（秒）

repo:
  local_path: "./local-repo"
  fallback_project: "default"
  gerrit_remote: "gerrit"
  initial_clone_depth: 10
  auto_deepen: true
  deepen_step: 100

cache:
  enabled: true
  ttl_hours: 24
```

---

## 7. 文件结构

```
Gerrit-Auto-Viewer/
├── mcp_gerrit_server/           # 核心服务层
│   ├── server.py                # MCP 服务 + 8 个工具
│   ├── config.py                # 配置加载
│   ├── gerrit_client.py         # Gerrit HTTP 客户端
│   ├── local_repo.py            # 本地 git 仓库管理
│   ├── cache.py                 # 评审缓存
│   ├── mock_api.py              # Mock Gerrit REST API
│   ├── mcp_compat.py            # 轻量 MCP 协议
│   ├── event_handler.py         # 事件处理（预留）
│   ├── webhook.py               # Webhook（预留）
│   └── tools/__init__.py        # 工具包
├── review_rules/                # 规则引擎
│   ├── engine.py                # 核心引擎（diff 解析 + 规则匹配）
│   ├── loader.py                # YAML 规则加载器
│   ├── builtin/                 # 内置规则（17条）
│   │   ├── security.yaml        # 6 条安全规则
│   │   ├── coding_style.yaml    # 5 条编码规范规则
│   │   └── common_bugs.yaml     # 6 条常见缺陷规则
│   └── custom/                  # 自定义规则（空）
├── review_prompts/              # Claude Code 提示词
│   ├── c_review_prompt_fast.md  # 快速 C 评审模板（生产用）
│   ├── c_review_prompt.md       # 完整 C 评审模板
│   └── general_review_prompt.md # 通用评审模板
├── scripts/                     # 运维脚本
│   ├── auto_review_poller.py    # 自动轮询评审主程序
│   ├── auto_review.py           # CLI 单次评审入口
│   ├── auto_review_poller.py    # 轮询器
│   ├── diagnose_auth.py         # 认证诊断
│   └── run_5_reviews.py         # 批量测试评审
├── tests/                       # 60 个单元测试
│   ├── test_local_repo.py
│   ├── test_gerrit_client.py
│   ├── test_mock_api.py
│   ├── test_review_engine.py
│   └── test_cache.py
├── config.yaml                  # 主配置
├── .env.example                 # 环境变量模板
├── requirements.txt             # Python 依赖
├── install.sh                   # Linux 部署脚本
├── run.sh                       # 启动脚本
├── DEPLOY.md                    # 部署指南
└── USER_GUIDE.md                # 用户手册
```

---

## 8. 版本控制策略

`REVIEWER_VERSION = "v4"` 具有双重作用：

1. **Gerrit 评审 tag**: `autogenerated:gerrit:auto-review:v4`
2. **重评审触发**: 版本号变更后，所有旧版本已评审的变更因 tag 不匹配会被重新评审

`_already_reviewed()` 使用三要素判断是否已评审：
- `_account_id == self._my_account_id`（同一用户）
- `tag == REVIEW_TAG`（同一版本）
- `_revision_number == rev_num`（同一 patch set）

---

## 9. 安全约束

| 约束 | 实现 |
|------|------|
| 凭证安全 | Gerrit 密码从 `.env` 或环境变量读取，不进代码库 |
| CLI 注入防护 | `auto_review.py` 用 Python 替代 shell 脚本的 `-c` 调用 |
| 评审失败不泄露 | Claude 失败 → 只写日志，不向 Gerrit 上报任何信息 |
| 并发隔离 | 命名 ref `refs/review/*` 替代 `FETCH_HEAD`，消除数据竞争 |
| 命令权限 | Claude CLI 使用 `--dangerously-skip-permissions`（内网可控环境） |

---

## 10. 已知限制

| 限制 | 说明 | 改进方向 |
|------|------|----------|
| 规则引擎 false positive | C-STYLE-003 在函数声明的参数列表里可能误报 | 增加上下文判断 |
| 规则引擎 false positive | C-BUG-002 在 size 为常量时仍报警 | 区分变量与常量 |
| Claude max-turns 检测 | 依赖正则匹配，Claude 更新输出格式后可能漏报 | 持续同步 pattern |
| 仅 C 语言支持 | 规则和 prompt 均针对嵌入式 C | 按语言扩展规则 + prompt |
| 无评分 | `post_review` 固定 `score=0`（仅发表评论不给分） | 根据问题严重度自动评分 |
| refs/review/* 清理 | 单次评审后立即清理，但异常退出可能残留 | gc() 中定期清理全部 |
