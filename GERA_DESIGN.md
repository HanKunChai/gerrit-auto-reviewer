# GERA — Gerrit Auto-Review 系统详细设计方案

> 版本: v4 / 2026-05-08

---

## 1. 系统概述

GERA (Gerrit Auto-Review) 是一个面向内网 Gerrit 代码评审平台的自动化代码审查系统。系统持续轮询 Gerrit 中待处理的变更，对每个变更创建独立 git worktree，并行启动 5 个维度专用 Claude Code 进程（各加载 `.claude/skills/` 中的专业 skill），最终将结构化评审结果提交回 Gerrit。

### 1.1 设计目标

| 目标 | 说明 |
|------|------|
| 自动化 | 零人工干预，持续轮询 Gerrit 待处理变更并自动提交评审意见 |
| 多维度并行 | 5 个 Claude 进程并行评审安全/内存/编码/逻辑/问题评估 |
| Skill 驱动 | 13 个独立 skill 文件，配置驱动，新增 skill 只需放 .md + 配置引用 |
| 并发安全 | Semaphore 限流 + 命名 ref + worktree 隔离 + lock 保护 |
| 内网兼容 | 纯 Python 3.8+，Flask + PyYAML + requests 三依赖 |
| 幂等 | REVIEW_TAG + _already_reviewed 双重校验，v4 版本标记 |

### 1.2 约束

| 约束 | 值 |
|------|-----|
| Python 版本 | >= 3.8（目标环境 3.8.10） |
| 第三方依赖 | flask, pyyaml, requests（仅三包） |
| AI 评审 | Claude Code CLI，内网部署 |
| 目标代码 | 嵌入式 C 语言（skill 可扩展） |
| 并发模型 | ThreadPoolExecutor(2) review × ThreadPoolExecutor(5) dimension × Semaphore(4) Claude |
| 评审隔离 | git worktree detached at `local-repo/_review/{id}/{rev}/` |
| 评审失败处理 | 超时 / 异常 / returncode≠0 / 空输出 → 跳过提交 |

---

## 2. 系统架构

```
┌──────────────────────────────────────────────────────────────────────┐
│                      GERA 系统架构 v4                                 │
└──────────────────────────────────────────────────────────────────────┘

                        ┌──────────────┐
                        │   Gerrit     │
                        │   REST API   │
                        └──────┬───────┘
                               │ HTTP Basic Auth
              ┌────────────────┼──────────────────┐
              ▼                ▼                    ▼
     ┌────────────────┐ ┌──────────────┐ ┌─────────────────────┐
     │   Poller       │ │  MCP Server  │ │  LocalRepo          │
     │   (轮询调度)    │ │  (server.py) │ │  shallow clone      │
     └───────┬────────┘ └──────────────┘ │  + worktree 创建     │
             │                           │  + 命名 ref fetch    │
             │                           └──────────┬──────────┘
             │                                      │
             ▼                                      ▼
     ┌─────────────────────────────────────────────────────────────┐
     │          _review_change() — 单次评审生命周期                  │
     │                                                             │
     │  1. fetch diff + 创建 worktree                               │
     │  2. [可选] 规则引擎                                           │
     │  3. _invoke_claude_dimensions() — 5 维度并行                  │
     │     ├─ [问题解决评估] problem-solving skill                   │
     │     ├─ [安全] buffer-overflow + injection + unsafe-functions │
     │     ├─ [内存管理] leak-detection + use-after-free + null-ptr │
     │     ├─ [编码规范] brace-style + line-limit + variable-style  │
     │     └─ [逻辑正确性] bounds-check + return-value + loop-term  │
     │  4. _build_review_message() → 按维度分章节                    │
     │  5. GerritClient.post_review()                               │
     │  6. 清理: diff 文件 → worktree → review ref                  │
     └─────────────────────────────────────────────────────────────┘
```

### 2.1 组件清单

| 组件 | 文件 | 职责 |
|------|------|------|
| Poller | `scripts/auto_review_poller.py` | 主入口：轮询 → 调度 → 多维度评审 → 提交 |
| MCP Server | `mcp_gerrit_server/server.py` | MCP 服务，供外部客户端调用 |
| Gerrit Client | `mcp_gerrit_server/gerrit_client.py` | Gerrit REST API（Basic Auth） |
| Local Repo | `mcp_gerrit_server/local_repo.py` | git clone + fetch + worktree + diff |
| Config | `mcp_gerrit_server/config.py` | YAML 配置加载 + DimensionConfig |
| Cache | `mcp_gerrit_server/cache.py` | 评审缓存（TTL） |
| Mock API | `mcp_gerrit_server/mock_api.py` | 本地测试用 mock Gerrit |
| Skills | `.claude/skills/*.md` | 13 个维度专用评审 skill |
| Rules Engine | `review_rules/engine.py` | diff 解析 + 正则匹配（可选） |

---

## 3. 数据流详解

### 3.1 单次评审流程

```
poll_and_submit()
  ├─ Gerrit GET /changes/?q=reviewer:xxx+status:open
  ├─ 过滤: 自己的提交 / _in_flight / _already_reviewed(tag+v4+revision)
  └─ self._executor.submit(_review_task)
       └─ _review_change()
            │
            ├─ [1] LocalRepo.ensure_branch(base)
            ├─ [1] LocalRepo.fetch_change() → refs/review/{id}/{rev} → SHA
            ├─ [1] LocalRepo.get_diff(base, SHA, context_lines=3)
            ├─ [1] LocalRepo.create_worktree(SHA) → worktree 目录
            │      └─ local-repo/_review/{change_id}/{rev_num}/
            ├─ [1] 写入 _review_diff.txt 到 worktree
            │
            ├─ [2] ReviewEngine.run(diff) → issues（可选，默认关闭）
            │
            ├─ [3] _invoke_claude_dimensions()
            │    ├─ _ensure_skill_files() → 复制 .claude/skills/ 到 worktree
            │    ├─ ThreadPoolExecutor(5) 并行:
            │    │   ├─ _build_dimension_prompt(dim, 问题, 方案, files, SHA)
            │    │   │    ├─ 合并维度下所有 skill body
            │    │   │    ├─ _build_status_table() → 文件变更清单
            │    │   │    ├─ needs_context 检测 → 追加上下文指令
            │    │   │    └─ 注入输出约束
            │    │   └─ _invoke_claude(prompt, dimension=dim.name)
            │    │        ├─ Semaphore.acquire()
            │    │        ├─ subprocess: claude -p "..." --dangerously-skip-permissions
            │    │        │    cwd = worktree 目录
            │    │        ├─ timeout / rc≠0 / 空输出 → return None
            │    │        └─ Semaphore.release()
            │    └─ return {dim_name: output_text}
            │
            ├─ [4] _build_review_message(dimension_results)
            │    ├─ _strip_thinking() 过滤散文
            │    └─ 按维度分章节 Markdown
            │
            ├─ [5] GerritClient.post_review(message, score=0, tag=v4)
            │
            └─ 清理: os.remove(_review_diff.txt)
                     repo.remove_worktree()
                     repo.cleanup_review_ref()
```

### 3.2 Git 操作并发安全

```
问题 1: 多线程共享 FETCH_HEAD 被覆盖
方案:   fetch_change → git fetch gerrit refs/changes/*:refs/review/{id}/{rev}
        → rev-parse refs/review/{id}/{rev} → 返回稳定 SHA

问题 2: 多线程共享 working tree，cat 源文件读到旧版本
方案:   create_worktree(SHA) → detached worktree 到
        local-repo/_review/{id}/{rev}/，每个 change 独立文件系统视图

问题 3: 多线程共用 git config 中的密码 URL
方案:   gerrit_push_url 使用 bare URL，不嵌入密码
```

### 3.3 失败处理

```
_invoke_claude 返回 None:
  ├─ claude 命令未找到
  ├─ subprocess.TimeoutExpired (config.dimension_timeout / claude_timeout)
  ├─ proc.returncode != 0
  └─ stdout 为空

多维度路径:
  任一维度失败 → 标注"评审未完成"
  全部维度失败 → 跳过 Gerrit 提交

单维度路径:
  失败 → 跳过 Gerrit 提交
```

---

## 4. Skill 系统

### 4.1 Skill 文件规范

位置：`.claude/skills/{name}.md`

```markdown
---
name: buffer-overflow
description: 嵌入式 C 缓冲区溢出检测专家
dimension: security
severity: error
needs_context: true
---

# 你是嵌入式 C 语言缓冲区溢出检测专家

## 核心职责
**只检查缓冲区溢出风险**，不检查编码规范、逻辑错误或内存泄漏。

## 检查项
- strcpy / strcat 无条件边界检查
- sprintf 无限制写入
...

## 输出
每行一条: `文件:行号 [error/warning] 简短原因`
**简洁原则**: 只列问题+一行简短原因。
```

### 4.2 13 个 Skill

| 维度 | Skill | needs_context |
|------|-------|:---:|
| 问题解决评估 | problem-solving | ✅ |
| 安全 | buffer-overflow, injection, unsafe-functions | |
| 内存管理 | leak-detection, use-after-free, null-pointer | ✅ |
| 编码规范 | brace-style, line-limit, variable-style | |
| 逻辑正确性 | bounds-check, return-value, loop-termination | ✅ |

`needs_context: true` → `_build_dimension_prompt` 追加强制指令：
> "发现潜在问题时必须 cat 源文件阅读完整函数上下文后再下结论"

### 4.3 扩展机制

新增 skill：
1. 创建 `.claude/skills/new-skill.md`（含 frontmatter）
2. 在 `config.yaml` 的对应维度 `skills` 列表中添加 `"new-skill"`

---

## 5. 配置

```yaml
auto_review:
  poll_interval: 600
  reviewer: "code-reviewer"
  query: "reviewer:code-reviewer+status:open"
  max_changes_per_poll: 10
  use_rules_engine: false
  max_concurrent_reviews: 2        # review 任务线程池
  max_concurrent_claude: 4         # Claude 子进程 Semaphore
  claude_timeout: 1800             # 单维度超时（秒）
  use_multi_dimension: true
  dimension_timeout: 1800          # 多维度超时（秒）
  dimensions:
    - name: "问题解决评估"
      skills: ["problem-solving"]
    - name: "安全"
      skills: ["buffer-overflow", "injection", "unsafe-functions"]
    - name: "内存管理"
      skills: ["leak-detection", "use-after-free", "null-pointer"]
    - name: "编码规范"
      skills: ["brace-style", "line-limit", "variable-style"]
    - name: "逻辑正确性"
      skills: ["bounds-check", "return-value", "loop-termination"]
```

---

## 6. 并发模型

```
self._executor (ThreadPoolExecutor, max_workers=2)
  │
  ├─ _review_task → _review_change
  │    └─ _invoke_claude_dimensions
  │         └─ ThreadPoolExecutor(max_workers=5)
  │              ├─ _run(安全)       ┐
  │              ├─ _run(编码规范)   │
  │              ├─ _run(内存管理)   ├─ 5 个 _invoke_claude
  │              ├─ _run(逻辑)      │   Semaphore(4) 限流
  │              └─ _run(问题评估)   ┘
  │
  └─ _review_task (第2个review)
       └─ ...同上...
```

| 层级 | 限制 | 机制 |
|------|------|------|
| Review 任务 | 2 | self._executor(max_workers=2) |
| 维度任务 | 5 | ThreadPoolExecutor(5) |
| Claude 子进程 | 4 | threading.Semaphore(4) |

---

## 7. 评审消息格式

```markdown
## 自动代码评审结果

**变更**: Iabc1234
**标题**: Fix buffer overflow

### 问题解决评估
[已解决] snprintf 替代 sprintf 修复了缓冲区溢出

### 安全
src/config.c:15 [error] snprintf 未检查返回值

### 内存管理
*该维度评审未完成*

### 编码规范
大括号风格符合 Allman 规范。

### 逻辑正确性
src/config.c:42 [warning] config_key 传入前未校验 NULL

---
*由 Gerrit Auto-Review + Claude Code 自动生成*
```

---

## 8. 安全约束

| 约束 | 实现 |
|------|------|
| 密码不泄露到日志 | `_sanitize_url()` 脱敏 3 处日志点 |
| 并发隔离 | 命名 ref + worktree + Semaphore |
| 评审失败不泄露 | 失败 → 只写日志，不提交 Gerrit |
| 凭证安全 | 密码从 .env / 环境变量读取 |

---

## 9. 已知限制

| 限制 | 说明 |
|------|------|
| 仅 C 语言 | skill 和 rules 均针对嵌入式 C |
| 无自动评分 | `score=0`，不根据问题严重度自动调整 |
| worktree 磁盘占用 | 每个 change 一份完整源码（评审完即清理） |
| prompt 模板非动态 | `c_review_prompt_fast.md` 用于单维度回退路径 |
