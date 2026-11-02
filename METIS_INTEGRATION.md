# Metis 集成说明 (替代 Claude Code)

内网环境禁止使用 Claude Code 类 agent, 本项目的 AI 评审后端已切换为
**Metis**(Arm 开源的 agentic 安全代码审查工具), 后端 LLM 为内网 OpenAI 兼容网关。

## 架构

```
Gerrit 轮询 (auto_review_poller.py)
  └─ MetisReviewer (scripts/metis_reviewer.py)   ← 新增, 替代 _invoke_claude*
      └─ subprocess metis CLI (metis-intranet 部署包 venv)
          ├─ --codebase-path <worktree>          # 与 Claude 路径共用同一个 worktree
          ├─ --config <metis.yaml>               # 内网网关配置
          ├─ --custom-prompt <变更上下文.md>      # 变更上下文/提交意图
          └─ --command review_file/review_dir    # 绝对路径, 按变更文件粒度审查
      └─ 结果转换: Metis findings → 评审格式
          {"issues": [{file, line, severity, message}]}
```

## 审查模式 (config.yaml → auto_review.metis.review_mode)

### native (默认, 推荐)

每个变更只跑 **一次** Metis 原生安全审查, 覆盖 Metis 自带的多维度安全透镜
(intra-function、调用图可达性、危险 API sink、错误回滚、计数器对称、路径访问等)
+ 二次验证 + 去重合并。无需按维度重复调用。

| 原 Claude 维度 | native 模式覆盖情况 |
|---|---|
| 安全 | ✅ Metis 原生覆盖 (更强: 可达性分析 + 证据验证) |
| 内存管理 | ✅ Metis 原生覆盖 (溢出/悬垂/双重释放/泄漏/竞态) |
| 逻辑正确性 | ⚠️ 部分 (比较逻辑错误、整数溢出、状态顺序、陈旧元数据) |
| 编码规范 | ❌ Metis 刻意不报非安全问题 (style-only 被过滤) |
| 业务逻辑 | ❌ 非安全范畴 |
| 问题解决评估 | ❌ 提交意图校验, 非代码审查 (可由材料检查代替) |

结论: 对安全驱动的 Gerrit 评审, native 模式效果 ≥ 原 Claude Code, 且开销约为
维度模式的 1/6。

### dimensions (可选)

按 `auto_review.dimensions` 并行逐维度审查, 维度 skills 内容通过
`--custom-prompt` 注入。**注意**: `--custom-prompt` 只作用于 simple_llm_review 路径,
对 C/C++ 的 reachability 路径不生效——因此该模式仅对非 C/C++ 项目有价值。

## 开关与配置 (config.yaml)

```yaml
auto_review:
  use_metis: true          # true=用 Metis, false=回退 Claude Code
  metis:
    executable: "<metis-intranet>/venv/Scripts/metis.exe"   # Linux: venv/bin/metis
    config:     "<metis-intranet>/config/metis.yaml"
    tiktoken_cache_dir: "<metis-intranet>/tiktoken"         # 离线编码缓存, 可选
    timeout: 1800           # 单次 Metis 调用超时 (秒)
    max_concurrent: 4       # 并行 Metis 子进程数 (信号量)
    review_mode: "native"   # native | dimensions
```

Metis 部署包见 `<metis-intranet>/`(Windows/Linux 双平台脚本、
中文输出补丁、离线依赖), 部署后把上面的路径改为本机实际路径即可。

## 行为差异 (与 Claude Code 相比)

| 项目 | Claude Code | Metis |
|---|---|---|
| LLM 后端 | 外网 Claude | 内网 LLM 网关 |
| 审查方式 | 交互式 agent 读 diff | 非交互: `review_file <绝对路径>` 按文件审查 |
| 输出 | 自由 JSON | 结构化 findings (issue/cwe/severity/mitigation) |
| 中文 | 需提示词要求 | 已内置中文输出补丁 (部署包已打) |
| 耗时 | 每变更 ~30min+ | 单文件 ~2min, 变更文件数×2min/并发数 |

注意:
- Metis 的 `review_patch` 仅支持非 C/C++ 语言; C/C++ 项目走 `review_file`(绝对路径)
  按变更文件审查, 评审耗时与变更文件数相关。
- 同一 codebase 并发会竞争本地 codegraph 缓存, 建议 `max_concurrent <= 4`。
- 结果以 `--- Metis 安全审查 ---` 小节写入 Gerrit 评论。

## 升级/回退

- 回退: `config.yaml` 里 `use_metis: false`(Claude 路径代码原样保留)。
- 升级 Metis: 替换 `metis-intranet/metis-source/` 后重跑安装脚本, 再执行
  `python scripts/patch_chinese_output.py metis-source` 重打中文补丁。
