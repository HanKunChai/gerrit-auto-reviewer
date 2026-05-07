# Gerrit MCP Auto-Review System — User Guide

## Overview

An MCP-based system that automatically reviews Gerrit code changes using Claude Code. It clones the repo locally once, then uses `git fetch` + `git diff` to analyze changes incrementally.

## Quick Start

### Prerequisites
- Python 3.10+
- Git
- Claude Code

### Installation

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Test the setup
python -m pytest tests/ -v
```

## Modes

### Local Test Mode (no Gerrit needed)

1. **Create a test repository:**
```bash
bash scripts/setup-test-env.sh
```
This creates `test-data/test-repo/` with a `base` and `feature` branch containing sample C code with intentional issues.

2. **Start the MCP server in mock mode:**
```bash
python -m mcp_gerrit_server.server --mock --verbose
```

3. **Verify it works:**
```bash
# In another terminal, test via Python:
python -c "
import asyncio
from mcp_gerrit_server.server import GerritReviewServer
svr = GerritReviewServer(mock=True)

# List changes
changes = asyncio.run(svr._handle_list_changes({'status': 'open', 'limit': 10}))
print(f'Changes: {len(changes)}')

# Fetch diff
diff = asyncio.run(svr._handle_fetch_diff({
    'change_id': 'test-001',
    'base_branch': 'base',
}))
print(f'Diff lines: {len(diff[\"diff\"].splitlines())}')
"
```

4. **Run a full auto-review:**
```bash
bash scripts/auto-review.sh --mock test-001
```

### Production Mode (with real Gerrit)

1. **Configure `config.yaml`:**
```yaml
mode: "production"
gerrit:
  base_url: "https://gerrit.your-company.com"
  auth:
    type: "http"
    username: "code-reviewer"
# Password via env: GERRIT_PASSWORD

auto_review:
  enabled: true
  poll_interval: 120
  reviewer: "code-reviewer"
  query: "reviewer:code-reviewer+status:open"

repo:
  local_path: "./local-repo"
  remote_url: "https://gerrit.your-company.com/project"
  gerrit_push_url: "ssh://code-reviewer@gerrit:29418/project"
```

2. **Set credentials:**
```bash
export GERRIT_PASSWORD=your-http-password
```

3. **Initialize local repo and start server:**
```bash
python -m mcp_gerrit_server.server --verbose
```

## How Auto-Review Works

### Trigger Mechanism (No Admin Required)

The system **polls** Gerrit's REST API periodically (default: every 120s) using the query:
```
reviewer:code-reviewer+status:open
```

This detects changes where `code-reviewer` has been added as a reviewer — no webhooks or admin permissions needed.

### Review Pipeline

```
1. Poll: List changes where reviewer=code-reviewer AND status=open
2. Fetch: git fetch gerrit refs/changes/XX/YYYY/Z  (incremental, seconds)
3. Diff:  git diff master...FETCH_HEAD              (local, milliseconds)
4. Rules: Run deterministic review rules engine
5. AI:    Claude Code analyzes diff + rules output
6. Post:  Submit review score + comments back to Gerrit
7. Cache: Store result to avoid re-reviewing same patch
```

### Manual Review (via Claude Code)

```bash
# Step 1: Fetch the change diff
claude mcp call fetch_diff --args '{"change_id": "12345"}'

# Step 2: Run rules engine
claude mcp call run_rules --args '{"diff_text": "...paste diff here..."}'

# Step 3: Get C review prompt
claude mcp call c_review_prompt

# Step 4: Post review
claude mcp call post_review --args '{
  "change_id": "12345",
  "message": "Looks good, minor style issues",
  "score": 1,
  "comments": {"src/main.c": [{"line": 42, "message": "use snprintf"}]}
}'
```

## Configuration Reference

See `config.yaml` for all options. Key sections:

| Section | Key Settings | Description |
|---------|-------------|-------------|
| `auto_review` | `poll_interval`, `reviewer`, `query` | Auto-review trigger |
| `gerrit` | `base_url`, `auth` | Gerrit connection |
| `repo` | `local_path`, `remote_url`, `gerrit_push_url` | Local repo management |
| `local_test` | `repo_path`, `base_branch`, `feature_branch` | Test environment |
| `rules` | `custom_rules_dir` | Custom rules location |
| `cache` | `ttl_hours`, `max_size_mb` | Review result caching |

## Large Repository Handling

1. **First run**: `git clone --depth=10` (shallow clone, fast)
2. **Background**: `git fetch --deepen=100` gradually deepens
3. **Each review**: `git fetch` only transfers missing objects (seconds)
4. **Diff**: `git diff` is purely local (milliseconds)

## Adding Custom Rules

Create `.yaml` files in `review_rules/custom/`:

```yaml
rules:
  - id: "CUSTOM-001"
    severity: "error"
    category: "custom"
    description: "No magic numbers"
    pattern:
      type: "regex"
      value: "\b\d{4,}\b"
    message: "Avoid magic numbers; use named constants"
    include:
      - '\.(?:c|h)$'
```

## Architecture

```
MCP Gerrit Server (Python)
├── server.py         — MCP tool registration and handlers
├── gerrit_client.py  — Real Gerrit REST API client
├── mock_api.py       — Mock Gerrit for local testing
├── local_repo.py     — Local git repo management
├── cache.py          — Review result caching
├── webhook.py        — Optional webhook listener
└── event_handler.py  — Event → review pipeline

review_rules/         — Deterministic review rules
├── engine.py         — Rule engine
├── loader.py         — YAML rule loader
└── builtin/          — Built-in C rules
    ├── coding_style.yaml
    ├── security.yaml
    └── common_bugs.yaml

review_prompts/       — Claude Code review prompts
├── c_review_prompt.md
└── general_review_prompt.md
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `ModuleNotFoundError: No module named 'flask'` | Run `pip install -r requirements.txt` |
| Git fetch hangs | Ensure SSH key is configured for Gerrit |
| Mock API returns 404 | Check change_id exists in mock data |
| Rules engine finds no issues | Make sure `file_path` ends with `.c` or `.h` |
| Tests fail on Windows paths | Use `pytest tests/` from project root |
