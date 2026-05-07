#!/usr/bin/env python3
"""
Gerrit Auto-Review Poller — 自动轮询 Gerrit 并执行代码评审。

持续查询 Gerrit 中分配给 review 的待处理变更，
获取 diff → 运行规则引擎 → 提交评审意见。

用法:
    python scripts/auto_review_poller.py                # 后台轮询
    python scripts/auto_review_poller.py --once         # 只跑一轮
    python scripts/auto_review_poller.py --verbose      # 详细日志
    python scripts/auto_review_poller.py --mock         # Mock 模式测试
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import List, Optional
from urllib.parse import quote

# 确保项目根目录在 sys.path 中
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

# 评审系统版本号 — 变更后已评审的变更会被重新评审
REVIEWER_VERSION = "v4"

from mcp_gerrit_server.config import load_config
from mcp_gerrit_server.gerrit_client import GerritClient
from mcp_gerrit_server.local_repo import LocalRepo
from review_rules.engine import ReviewEngine

logger = logging.getLogger("auto-review-poller")


def _parse_commit_msg(msg: str) -> tuple:
    """从 commit message 中提取 问题/方案 两段。

    预期格式::
        问题现象: xxx
        方案: xxx
        功能说明: xxx
        解决办法: xxx
    """
    problem = ""
    solution = ""

    for line in msg.splitlines():
        line = line.strip()
        if not line:
            continue
        # 要求关键词后跟冒号，避免误匹配
        m = re.match(r'^(问题现象|功能说明|问题描述|Bug|缺陷)[:：]\s*(.*)', line, re.IGNORECASE)
        if m:
            problem = m.group(2) or line
            continue
        m = re.match(r'^(方案|解决办法|修复方案|解决方案|Fix|Solution)[:：]\s*(.*)', line, re.IGNORECASE)
        if m:
            solution = m.group(2) or line
            continue
        if problem and not solution:
            problem += " " + line
        elif solution:
            solution += " " + line

    # If no structured format found, use first 200 chars as problem
    if not problem and not solution:
        problem = msg[:200].replace("\n", " ")
        solution = "(见commit message)"

    return (problem or "(无)", solution or "(无)")


class AutoReviewPoller:
    """Gerrit 自动评审轮询器。

    周期性查询 Gerrit 中等待评审的变更，自动执行代码评审并提交结果。
    """

    def __init__(self, mock: bool = False, config_path: Optional[str] = None):
        self.config = load_config(config_path)
        self.mock = mock
        self._my_account_id: Optional[int] = None
        self._in_flight: set = set()  # 正在评审中的 change_id（防重复投递）
        self._engine = ReviewEngine()

        # 加载规则
        self._engine.load_builtin_rules()
        custom_dir = self.config.rules.custom_rules_dir
        if os.path.isdir(custom_dir):
            self._engine.load_rules(custom_dir)
        logger.info("已加载 %d 条规则", len(self._engine.rules))

        # 初始化 Gerrit 客户端
        if not mock:
            gc = self.config.gerrit
            auth = (gc.username, gc.password) if gc.username and gc.password else None
            self._client = GerritClient(
                gc.base_url, auth=auth,
                use_a_prefix=gc.use_a_prefix,
            )
            logger.info("Gerrit 客户端已初始化: %s (user=%s)", gc.base_url, gc.username)

            # 获取当前用户的 account_id，用于过滤自己的提交
            try:
                from mcp_gerrit_server.gerrit_client import parse_gerrit_response
                resp = self._client._get("/accounts/self")
                account = parse_gerrit_response(resp.text)
                self._my_account_id = account.get("_account_id")
                logger.info("当前用户 account_id=%s, name=%s",
                            self._my_account_id, account.get("name", "?"))
            except Exception as e:
                logger.warning("无法获取 account_id: %s, 将用 username 过滤", e)
        else:
            self._client = None
            logger.info("使用 MOCK 模式")

        # 持久线程池，评审任务异步提交不等结果
        self._executor = ThreadPoolExecutor(
            max_workers=self.config.auto_review.max_concurrent_reviews,
            thread_name_prefix="review",
        )

        # 信号量限制同时运行的 Claude 子进程总数
        self._claude_semaphore = threading.Semaphore(
            self.config.auto_review.max_concurrent_claude,
        )

        # 预加载评审模板（避免每次 Claude 调用时重复读文件）
        self._prompt_template = ""
        prompt_path = os.path.join(
            PROJECT_DIR, "review_prompts", "c_review_prompt_fast.md",
        )
        try:
            with open(prompt_path, encoding="utf-8") as f:
                self._prompt_template = f.read()
        except FileNotFoundError:
            logger.warning("评审模板未找到: %s", prompt_path)

        # Build skill registry (scan .claude/skills/*.md)
        self._skill_registry = self._build_skill_registry()
        ar = self.config.auto_review
        if ar.use_multi_dimension and ar.dimensions:
            loaded = sum(
                1 for d in ar.dimensions
                for s in d.skills if s in self._skill_registry
            )
            logger.info("多维度评审: %d 维度, %d skills 已注册",
                         len(ar.dimensions), loaded)

    REVIEW_TAG = f"autogenerated:gerrit:auto-review:{REVIEWER_VERSION}"

    def _already_reviewed(self, change_id: str, rev_num: str) -> bool:
        """Check Gerrit if current user already reviewed THIS revision with current version."""
        if self.mock:
            return False
        try:
            detail = self._client.get_change_detail(change_id)
            messages = detail.get("messages", [])
            for msg in messages:
                author = msg.get("author", {})
                is_me = author.get("_account_id") == self._my_account_id
                same_tag = msg.get("tag") == self.REVIEW_TAG
                same_rev = str(msg.get("_revision_number", "")) == rev_num
                if is_me and same_tag and same_rev:
                    return True
            return False
        except Exception:
            return False  # API failure: don't block

    def _review_task(self, change: dict) -> None:
        """异步评审单个变更（由线程池调用）。"""
        cid = change.get("id", "")
        cnum = str(change.get("_number", ""))
        subj = change.get("subject", "")
        proj = change.get("project", "?")
        owner = change.get("owner", {})
        oname = owner.get("username", owner.get("email", "?"))
        revisions = change.get("revisions", {})
        if revisions:
            rid = list(revisions.keys())[0]
            rnum = revisions[rid].get("_number", 1)
        else:
            rid = "current"
            rnum = 1

        logger.info("=" * 55)
        logger.info("[%s] 评审变更: %s", cnum, cid)
        logger.info("[%s]   项目: %s, 标题: %s", cnum, proj, subj)
        logger.info("[%s]   作者: %s, 版本: %d", cnum, oname, rnum)
        logger.info("=" * 55)

        try:
            self._review_change(cid, cnum, str(rnum), proj, rid, subj)
            logger.info("[%s] 处理完成", cid)
        except Exception as e:
            logger.error("[%s] 评审失败: %s", cid, e, exc_info=True)
        finally:
            self._in_flight.discard(cid)

    def poll_and_submit(self) -> int:
        """查询 Gerrit 并提交新变更到评审线程池，返回提交数。"""
        ar = self.config.auto_review

        if self.mock:
            logger.info("[Mock] 查询变更...")
            changes = self._mock_changes()
        else:
            logger.info("查询变更: %s", ar.query)
            changes = self._client.list_changes(
                query=ar.query,
                limit=ar.max_changes_per_poll,
            )

        reviewer_username = ar.reviewer
        submitted = 0
        skipped_own = 0
        skipped_reviewed = 0
        skipped_in_flight = 0

        for ch in changes:
            cid = ch.get("id", "?")
            owner = ch.get("owner", {})
            owner_name = owner.get("username") or owner.get("name") or owner.get("email", "?")
            is_my_own = (
                (self._my_account_id is not None
                 and owner.get("_account_id") == self._my_account_id)
                or owner.get("username") == reviewer_username
                or (owner.get("email", "") or "").startswith(reviewer_username + "@")
            )
            if is_my_own:
                skipped_own += 1
                continue

            # 正在评审中（已投递但未完成）
            if cid in self._in_flight:
                skipped_in_flight += 1
                continue

            # 已评审过（Gerrit 端已有当前版本 + 当前 revision 的评审）
            revisions = ch.get("revisions", {})
            cur_rev = "1"
            if revisions:
                rev_info = list(revisions.values())[0]
                cur_rev = str(rev_info.get("_number", "1"))
            if not self.mock and self._already_reviewed(cid, cur_rev):
                skipped_reviewed += 1
                continue

            # 投递到线程池异步评审
            self._in_flight.add(cid)
            self._executor.submit(self._review_task, ch)
            submitted += 1

        logger.info(
            "查询 %d 变更 → 提交 %d, 自己=%d, 已评审=%d, 进行中=%d",
            len(changes), submitted, skipped_own, skipped_reviewed, skipped_in_flight,
        )
        return submitted

    def _review_change(
        self, change_id: str, change_number: str, rev_num: str,
        project: str, rev_id: str, subject: str = "",
    ) -> None:
        """评审单个变更。"""
        # 1. 获取 diff
        logger.info("  [1/3] 获取 diff...")
        repo = None
        base_branch = "master"
        commit_msg = subject
        sha = ""
        if self.mock:
            diff_text = self._mock_diff()
            files = self._mock_files()
        else:
            repo = self._ensure_local_repo(project)
            try:
                change_info = self._client.get_change(change_id)
                base_branch = change_info.get("branch", base_branch)
                # Get full commit message for business logic review
                revisions = change_info.get("revisions", {})
                if revisions:
                    rev_data = list(revisions.values())[0]
                    commit = rev_data.get("commit", {})
                    commit_msg = commit.get("message", subject)
            except Exception:
                pass
            # 先 fetch base branch（确保 base 存在），再 fetch change
            repo.ensure_branch(base_branch)
            sha = repo.fetch_change(change_number, rev_num)
            diff_text = repo.get_diff(base_branch, head_ref=sha)
            files = repo.list_changed_files(base_branch, head_ref=sha)
            logger.info("  fetch SHA: %s", sha[:8] if sha else "N/A")

        logger.info("  diff: %d 行, %d 个文件", len(diff_text.splitlines()), len(files))
        for status, fpath in files:
            logger.info("    [%s] %s", status, fpath)

        if not diff_text.strip():
            logger.warning("  变更无 diff 内容，跳过")
            return

        # 2. 运行本地规则引擎（仅在启用时）
        if self.config.auto_review.use_rules_engine:
            logger.info("  [2/4] 运行规则引擎...")
            issues = self._engine.run(diff_text)
            logger.info(
                "  发现 %d 个问题: %d errors, %d warnings, %d info",
                len(issues),
                sum(1 for i in issues if i.severity == "error"),
                sum(1 for i in issues if i.severity == "warning"),
                sum(1 for i in issues if i.severity == "info"),
            )
            for issue in issues[:20]:
                logger.info(
                    "    [%s] %s:%d %s",
                    issue.severity.upper(), issue.file, issue.line, issue.message,
                )
        else:
            issues = []

        # 3. Claude Code AI 评审
        repo_dir = str(repo.repo_path) if hasattr(repo, 'repo_path') else None
        changed_files = [f for _, f in files]
        ar = self.config.auto_review

        if ar.use_multi_dimension and ar.dimensions:
            logger.info("  [3/4] 多维度并行评审 (%d 维度)...", len(ar.dimensions))
            dimension_results = self._invoke_claude_dimensions(
                change_id, commit_msg, repo_dir, base_branch, changed_files, sha,
            )
            valid = {k: v for k, v in dimension_results.items() if v}
            if not valid:
                logger.error("  所有维度 Claude 评审均失败，跳过上报 (变更: %s)", change_id)
                return
            logger.info("  Claude 评审: %d/%d 维度完成", len(valid), len(ar.dimensions))
            for dn, dr in dimension_results.items():
                status = "OK" if dr else "FAIL"
                logger.info("    [%s] %s (%d 字符)", status, dn, len(dr or ""))
        else:
            logger.info("  [3/4] Claude Code 评审...")
            claude_review = self._invoke_claude(
                change_id, commit_msg, repo_dir, base_branch, changed_files, sha,
            )
            if claude_review:
                logger.info("  Claude 评审: %d 字符", len(claude_review))
            else:
                logger.error("  Claude Code 评审失败，跳过上报 (变更: %s)", change_id)
                return
            dimension_results = {"Claude Code 评审": claude_review}

        # 4. 构造评审消息
        logger.info("  [4/4] 构造评审意见...")
        message = self._build_review_message(
            change_id, subject, issues, dimension_results,
        )
        logger.info("  评审意见: %d 字符", len(message))

        # 5. 提交评审
        if not self.mock:
            result = self._client.post_review(
                change_id, rev_id,
                message=message,
                score=0,
                tag=self.REVIEW_TAG,
            )
            logger.info("  评审已提交")
        else:
            logger.info("  [Mock] 评审已提交")

        # 清理本次 fetch 创建的 review ref，避免长期堆积
        if not self.mock and repo:
            try:
                repo.cleanup_review_ref(change_number, rev_num)
            except Exception:
                pass

        logger.info("  [OK] 评审完成")

    def _invoke_claude(
        self, change_id: str, commit_msg: str = "",
        repo_dir: Optional[str] = None, base_branch: str = "master",
        changed_files: Optional[List[str]] = None,
        head_sha: str = "",
        prompt: Optional[str] = None,
        dimension: str = "",
    ) -> Optional[str]:
        """Invoke Claude Code to review the diff in the local repo.

        If *prompt* is given it is used directly; otherwise the prompt is
        built from the template + commit context (backward-compatible path).

        When *dimension* is set, dimension-specific timeout/max-turns are
        used and log lines are tagged with the dimension name.
        """
        claude_bin = shutil.which("claude") or shutil.which("claude-code")
        if not claude_bin:
            logger.warning("  未找到 claude/claude-code 命令，跳过 AI 评审")
            return None

        if prompt is None:
            problem, solution = _parse_commit_msg(commit_msg)
            files_str = ", ".join(changed_files[:20]) if changed_files else "?"
            prompt = (
                f"{self._prompt_template}\n\n"
                f"问题: {problem}\n方案: {solution}\n"
                f"文件: {files_str}\n"
                f"diff: git diff {base_branch}...{head_sha}"
            )

        ar = self.config.auto_review
        if dimension:
            timeout = ar.dimension_timeout
            max_turns = str(ar.dimension_max_turns)
        else:
            timeout = ar.claude_timeout
            max_turns = "10"

        dim_tag = f"[{dimension}] " if dimension else ""
        logger.info("  %s调用 Claude Code (cwd=%s, prompt=%d chars, timeout=%ds)...",
                    dim_tag, repo_dir, len(prompt), timeout)

        env = os.environ.copy()
        env["CLAUDE_CODE_SKIP_AUTH"] = "1"

        cmd = [
            claude_bin, "-p", prompt,
            "--dangerously-skip-permissions",
            "--max-turns", max_turns,
        ]
        if repo_dir and os.path.isdir(repo_dir):
            cwd = repo_dir
        else:
            cwd = None

        started = time.time()
        stderr_lines = []

        # 专用 logger：Claude stderr 只写文件，不打印到控制台
        claude_logger = logging.getLogger("auto-review-poller.claude")
        claude_logger.propagate = False
        if not claude_logger.handlers:
            log_dir = os.path.join(PROJECT_DIR, "logs")
            fh = logging.FileHandler(
                os.path.join(log_dir, "claude_debug.log"), encoding="utf-8",
            )
            fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            claude_logger.addHandler(fh)
            claude_logger.setLevel(logging.DEBUG)

        claude_logger.debug("[start] change=%s prompt=%d chars timeout=%ds",
                            change_id, len(prompt), timeout)

        def _read_stderr(pipe):
            """实时读取 Claude stderr，写入文件日志。"""
            for line in iter(pipe.readline, ""):
                line = line.strip()
                if line:
                    stderr_lines.append(line)
                    claude_logger.debug("%s", line[:300])
            claude_logger.debug("[stderr eof]")

        self._claude_semaphore.acquire()
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                cwd=cwd,
                env=env,
            )
            # 启动后台线程实时读取 stderr
            stderr_thread = threading.Thread(
                target=_read_stderr, args=(proc.stderr,), daemon=True,
            )
            stderr_thread.start()

            try:
                stdout, _ = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, _ = proc.communicate()
                elapsed = time.time() - started
                logger.error("  Claude 评审超时 (%ds), 已终止", timeout)
                logger.error("  stderr (最后 10 行):\n%s",
                             "\n".join(stderr_lines[-10:]) if stderr_lines else "(无输出)")
                return None

            elapsed = time.time() - started
            stderr_thread.join(timeout=2)

            if proc.returncode != 0:
                logger.error("  Claude 返回 %d (%.0fs)", proc.returncode, elapsed)
                logger.error("  stderr: %d 行, 最后:\n%s",
                             len(stderr_lines),
                             "\n".join(stderr_lines[-20:]) if stderr_lines else "(无输出)")
                return None

            output = (stdout or "").strip()
            if not output:
                logger.warning("  Claude 返回空输出 (%.0fs, stderr=%d行)",
                               elapsed, len(stderr_lines))
                return None

            # Check if Claude hit --max-turns limit (>=10 turns = failure)
            _MAX_TURN_PATTERNS = [
                r"(max|maximum|超?过|达到|超出).*(turn|轮次|次数|限制)",
                r"(turn|轮次|回合).*(limit|限制).*(reach|达到|超出|超过)",
                r"limit.*(reach|hit|exceeded|耗尽|用完)",
            ]
            combined = "|".join(f"(?:{p})" for p in _MAX_TURN_PATTERNS)
            max_turns_hit = any(
                re.search(combined, line, re.IGNORECASE)
                for line in stderr_lines
            )
            if not max_turns_hit:
                max_turns_hit = bool(re.search(combined, output, re.IGNORECASE))
            if max_turns_hit:
                logger.error("  Claude 达到最大 turns 限制 (--max-turns 10), 视为失败，跳过上报")
                return None

            claude_logger.debug("[done] rc=0 elapsed=%.0fs stdout=%d stderr=%d",
                                elapsed, len(output), len(stderr_lines))
            logger.info("  Claude 完成 (%.0fs, %d 字符)", elapsed, len(output))
            return output

        except Exception as e:
            logger.error("  Claude 调用失败: %s", e)
            return None
        finally:
            self._claude_semaphore.release()

    # ---- Skill-based multi-dimension review -------------------------------

    def _parse_skill_frontmatter(self, content: str) -> tuple:
        """Parse YAML frontmatter from a skill .md file.

        Returns (frontmatter_dict, body_text).  Frontmatter expects flat
        ``key: value`` lines between ``---`` markers.  Only the leading
        ``---`` / ``---`` pair is parsed; ``---`` inside the body is safe.
        """
        if not content.startswith("---\n"):
            return {}, content
        # Find the closing --- on its own line (must have leading newline)
        idx = content.find("\n---\n", 4)
        if idx == -1:
            # Closing --- at end of file without trailing newline
            if content.endswith("\n---"):
                idx = len(content) - 4
            else:
                return {}, content
        fm_block = content[4:idx].strip()
        body = content[idx + 5:].strip()  # skip \n---\n
        fm = {}
        for line in fm_block.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                fm[key.strip()] = val.strip().strip("\"'")
        return fm, body

    def _build_skill_registry(self) -> dict:
        """Scan .claude/skills/*.md and build name->{path,frontmatter,body} map."""
        skills_dir = Path(PROJECT_DIR) / ".claude" / "skills"
        registry = {}
        if not skills_dir.is_dir():
            logger.warning("Skills 目录不存在: %s", skills_dir)
            return registry
        for sf in sorted(skills_dir.glob("*.md")):
            try:
                content = sf.read_text(encoding="utf-8")
                fm, body = self._parse_skill_frontmatter(content)
                name = fm.get("name", sf.stem)
                registry[name] = {
                    "path": str(sf),
                    "frontmatter": fm,
                    "body": body,
                }
            except Exception:
                logger.warning("无法加载 skill 文件: %s", sf, exc_info=True)
        return registry

    def _build_dimension_prompt(
        self, dimension, problem: str, solution: str,
        files_str: str, diff_cmd: str,
    ) -> str:
        """Build a prompt for one dimension by merging its skills + commit context."""
        skill_bodies = []
        for skill_name in dimension.skills:
            skill = self._skill_registry.get(skill_name)
            if skill:
                skill_bodies.append(skill["body"])
            else:
                logger.warning("  Skill 未注册: %s", skill_name)

        if not skill_bodies:
            return ""

        merged = "\n\n---\n\n".join(skill_bodies)
        return (
            f"{merged}\n\n"
            f"## 变更上下文\n"
            f"问题: {problem}\n"
            f"方案: {solution}\n"
            f"变更文件: {files_str}\n"
            f"获取diff: git diff {diff_cmd}\n\n"
            f"## 输出约束（必须严格遵守）\n"
            f"- 只输出问题列表，每行一条: 文件:行号 [error/warning] 简短原因\n"
            f"- 禁止输出任何思考过程、分析推理、检查步骤\n"
            f"- 禁止输出修复建议、代码示例、修改方案\n"
            f"- 禁止输出总结段落或统计数字\n"
            f"- 无问题时只输出一行简短的“无问题”结论"
        )

    def _ensure_skill_files(self, repo_dir: str, dimensions) -> None:
        """Copy skill .md files to the local-repo so Claude Code auto-discovers them."""
        dst_skills = Path(repo_dir) / ".claude" / "skills"
        try:
            dst_skills.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        copied = set()
        for dim in dimensions:
            for skill_name in dim.skills:
                if skill_name in copied:
                    continue
                skill = self._skill_registry.get(skill_name)
                if not skill:
                    continue
                src = Path(skill["path"])
                dst = dst_skills / f"{skill_name}.md"
                try:
                    if not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime:
                        shutil.copy2(src, dst)
                except OSError:
                    pass
                copied.add(skill_name)

    def _invoke_claude_dimensions(
        self, change_id: str, commit_msg: str,
        repo_dir: str, base_branch: str,
        changed_files: list, head_sha: str,
    ) -> dict:
        """Run all configured dimensions in parallel, return {dim_name: result}."""
        ar = self.config.auto_review
        problem, solution = _parse_commit_msg(commit_msg)
        files_str = ", ".join(changed_files[:20]) if changed_files else "?"
        diff_cmd = f"{base_branch}...{head_sha}"

        # Copy skills to repo so Claude Code can load them
        self._ensure_skill_files(repo_dir, ar.dimensions)

        results = {}
        lock = threading.Lock()

        def _run(dim):
            prompt = self._build_dimension_prompt(
                dim, problem, solution, files_str, diff_cmd,
            )
            if not prompt:
                with lock:
                    results[dim.name] = None
                return
            result = self._invoke_claude(
                change_id,
                repo_dir=repo_dir,
                prompt=prompt,
                dimension=dim.name,
            )
            with lock:
                results[dim.name] = result

        with ThreadPoolExecutor(max_workers=len(ar.dimensions)) as executor:
            futures = [executor.submit(_run, d) for d in ar.dimensions]
            for f in futures:
                f.result()  # wait for all dimensions

        return results

    def _ensure_local_repo(self, project: str) -> "LocalRepo":
        """获取或创建项目本地仓库（优先 HTTP 凭证，兜底本地 git）。"""
        rc = self.config.repo
        repo_path = os.path.join(rc.local_path, project)
        base_url = self.config.gerrit.base_url.rstrip("/")
        plain_url = f"{base_url}/{project}"

        # 优先: 用 Gerrit HTTP 密码构建 git URL
        gc = self.config.gerrit
        if gc.username and gc.password:
            from urllib.parse import urlparse
            parsed = urlparse(base_url)
            pwd = quote(gc.password, safe="")
            auth_url = (
                f"{parsed.scheme}://{gc.username}:{pwd}"
                f"@{parsed.netloc}{parsed.path}/{project}"
            )
        else:
            auth_url = plain_url

        def _make_repo(clone_url, push_url):
            return LocalRepo(
                repo_path=repo_path,
                remote_url=clone_url,
                gerrit_remote=rc.gerrit_remote,
                gerrit_push_url=push_url,
                initial_depth=rc.initial_clone_depth,
            )

        # 认证 URL 为主；gerrit remote 也用它
        repo = _make_repo(auth_url, auth_url)
        try:
            repo.ensure_clone()
        except Exception:
            if auth_url == plain_url:
                raise
            logger.warning("HTTP 凭证 clone 失败，尝试用本地 git 凭证...")
            repo = _make_repo(plain_url, plain_url)
            repo.ensure_clone()
        return repo

    def _strip_thinking(self, text: str) -> str:
        """Remove verbose thinking/analysis lines from Claude output."""
        lines = text.splitlines()
        cleaned = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            # Always keep lines with file:line pattern (e.g. src/main.c:15)
            if re.search(r"\S+\.\w+:\d+", stripped):
                cleaned.append(stripped)
                continue
            # Keep conclusion lines
            if stripped.startswith("[已解决") or stripped.startswith("[部分解决") or stripped.startswith("[未解决"):
                cleaned.append(stripped)
                continue
            # Skip long prose lines (no file:line, probably thinking/analysis)
            if len(stripped) > 60:
                continue
            cleaned.append(stripped)
        return "\n".join(cleaned) if cleaned else (text or "")

    def _build_review_message(
        self, change_id: str, subject: str, issues,
        claude_review = None,
    ) -> str:
        """构造评审消息。

        *claude_review* can be a str (single-dimension, backward-compat),
        a dict of ``{dim_name: result_text}`` (multi-dimension), or None.
        """
        errors = [i for i in issues if i.severity == "error"]
        warnings = [i for i in issues if i.severity == "warning"]
        infos = [i for i in issues if i.severity == "info"]

        lines = [
            "## 自动代码评审结果",
            "",
            f"**变更**: {change_id}",
            f"**标题**: {subject}",
            "",
        ]

        if isinstance(claude_review, dict):
            # Multi-dimension mode: one section per dimension
            for dim_name, dim_result in claude_review.items():
                lines.append(f"### {dim_name}")
                lines.append("")
                if dim_result:
                    dim_result = self._strip_thinking(dim_result)
                    dim_result = re.sub(
                        r"\n?\s*No issues found\.?\s*$", "",
                        dim_result, flags=re.IGNORECASE,
                    )
                    lines.append(dim_result)
                else:
                    lines.append("*该维度评审未完成*")
                lines.append("")
        elif isinstance(claude_review, str) and claude_review:
            # Single-dimension mode (backward-compat)
            claude_review = self._strip_thinking(claude_review)
            normalized = re.sub(
                r"\n?\s*No issues found\.?\s*$", "",
                claude_review, flags=re.IGNORECASE,
            )
            lines.append("### Claude Code 评审")
            lines.append("")
            lines.append(normalized)
            lines.append("")

        # 规则引擎结果（补充）
        if issues:
            lines.append("### 规则引擎检查")
            lines.append("")
            lines.append(
                f"**摘要**: {len(issues)} 个问题 "
                f"({len(errors)} errors, {len(warnings)} warnings, {len(infos)} info)"
            )
            lines.append("")

            if errors:
                lines.append("#### 错误")
                for i in errors:
                    lines.append(f"- **{i.rule_id}** `{i.file}:{i.line}` {i.message}")
                lines.append("")

            if warnings:
                lines.append("#### 警告")
                for i in warnings:
                    lines.append(f"- **{i.rule_id}** `{i.file}:{i.line}` {i.message}")
                lines.append("")

            if infos:
                lines.append("#### 提示")
                for i in infos:
                    lines.append(f"- **{i.rule_id}** `{i.file}:{i.line}` {i.message}")
                lines.append("")

        lines.append("---")
        lines.append("*由 Gerrit Auto-Review + Claude Code 自动生成*")
        return "\n".join(lines)

    # ---- Mock 支持 ----

    def _mock_changes(self) -> List[dict]:
        """Mock 变更列表。"""
        return [
            {
                "id": "Ideadbeef1234",
                "change_id": "Ideadbeef1234",
                "subject": "Fix buffer overflow in config parser",
                "project": "test-project",
                "branch": "main",
                "status": "OPEN",
                "owner": {"username": "developer"},
                "labels": {
                    "Code-Review": {
                        "all": [{"username": self.config.auto_review.reviewer}],
                    },
                },
                "revisions": {
                    "deadbeef1234": {
                        "_number": 1,
                        "ref": "refs/changes/01/1/1",
                    },
                },
            },
        ]

    def _mock_diff(self) -> str:
        """Mock diff 内容。"""
        return (
            "diff --git a/src/config.c b/src/config.c\n"
            "index abc123..def456 100644\n"
            "--- a/src/config.c\n"
            "+++ b/src/config.c\n"
            "@@ -10,17 +10,22 @@ static char config_buf[256];\n"
            " \n"
            "-void read_config(const char *filename)\n"
            "+void read_config(const char *filename)\n"
            " {\n"
            "     FILE *fp;\n"
            " \n"
            "     fp = fopen(filename, \"r\");\n"
            "     if (!fp) {\n"
            "-        sprintf(config_buf, \"config not found\");\n"
            "+        snprintf(config_buf, sizeof(config_buf), \"config not found\");\n"
            "         return;\n"
            "     }\n"
            "+    strcpy(dst, src);\n"
            "     fclose(fp);\n"
            " }\n"
        )

    def _mock_files(self) -> List[tuple]:
        return [("M", "src/config.c")]


def setup_logging(verbose: bool = False):
    """配置日志输出到控制台和文件。"""
    log_dir = os.path.join(PROJECT_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(log_dir, f"auto_review_{timestamp}.log")

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 文件日志 (始终 INFO 级别)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh.setFormatter(fmt)

    # 控制台日志
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(fmt)

    root = logging.getLogger("auto-review-poller")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(fh)
    root.addHandler(ch)

    logger.info("日志文件: %s", log_file)
    return log_file


def main():
    parser = argparse.ArgumentParser(
        description="Gerrit Auto-Review Poller - 自动轮询并评审代码",
    )
    parser.add_argument("--once", action="store_true", help="只执行一轮，不循环")
    parser.add_argument("--interval", type=int, default=0, help="轮询间隔(秒)，默认使用配置")
    parser.add_argument("--mock", action="store_true", help="Mock 模式测试")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")
    parser.add_argument("--config", type=str, help="config.yaml 路径")
    args = parser.parse_args()

    log_file = setup_logging(args.verbose)

    poller = AutoReviewPoller(mock=args.mock, config_path=args.config)
    interval = args.interval or poller.config.auto_review.poll_interval
    max_concurrent = poller.config.auto_review.max_concurrent_reviews

    logger.info("=" * 55)
    logger.info("  Gerrit Auto-Review Poller 启动")
    logger.info("  Mock:     %s", args.mock)
    logger.info("  间隔:     %d 秒", interval)
    logger.info("  并发:     %d 线程", max_concurrent)
    logger.info("  模式:     %s", "一次性" if args.once else "持续轮询 (异步)")
    logger.info("=" * 55)
    logger.info("")

    if args.once:
        submitted = poller.poll_and_submit()
        logger.info("提交 %d 个任务到评审线程池", submitted)
        poller._executor.shutdown(wait=True)
        logger.info("所有评审任务已完成")
        return

    # 持续轮询 — 查询和提交不阻塞，评审线程池异步执行
    try:
        while True:
            try:
                submitted = poller.poll_and_submit()
                if submitted > 0:
                    in_flight = len(poller._in_flight)
                    logger.info("提交 %d 个新任务, 进行中 %d, 等待 %ds 后下次查询...",
                                submitted, in_flight, interval)
                else:
                    logger.info("无新变更, 等待 %d 秒...", interval)
            except Exception as e:
                logger.error("轮询异常: %s", e, exc_info=True)

            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                raise

    except KeyboardInterrupt:
        logger.info("收到退出信号, 等待进行中评审完成...")
        # Note: Python 3.8 不支持 shutdown(timeout=)，Claude 已通过
        # --max-turns + claude_timeout 保证不会无限等待
        poller._executor.shutdown(wait=True)
        logger.info("Auto-Review Poller 已停止")


if __name__ == "__main__":
    main()
