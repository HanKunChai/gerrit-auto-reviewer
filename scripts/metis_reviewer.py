#!/usr/bin/env python3
"""
Metis 内网 LLM 审查适配器 — 替代 Claude Code 调用。

在 Gerrit Auto-Review Poller 中, 将 ``subprocess claude -p ...`` 替换为
Metis CLI (``metis --codebase-path ... --command review_file ...``),
LLM 后端为内网 OpenAI 兼容网关 (见部署包 metis-intranet 的 metis.yaml)。

与 Claude 路径保持相同的返回契约:
    review_dimensions() -> {dim_name: result_dict}
    result_dict = {"issues": [{"file", "line", "severity", "message"}]}
    或 (问题解决评估维度) {"verdict", "reason", "gaps", "side_effects"}
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

logger = logging.getLogger("auto-review-poller.metis")


def _quote(path: str) -> str:
    """简单路径引用 (含空格时加双引号)。"""
    p = str(path)
    return f'"{p}"' if " " in p else p

# severity 映射: Metis 严重级别 -> Gerrit 评审 severity
_SEVERITY_MAP = {
    "critical": "error",
    "high": "error",
    "medium": "warning",
    "low": "warning",
    "unknown": "warning",
}


def _parse_commit_msg(msg: str) -> tuple:
    """从 commit message 中提取 问题/方案 两段 (与 auto_review_poller 同款逻辑)。"""
    problem = ""
    solution = ""
    for line in (msg or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(问题现象|功能说明|问题描述|Bug|缺陷)[:：]\s*(.*)", line, re.IGNORECASE)
        if m:
            problem = m.group(2) or line
            continue
        m = re.match(r"^(方案|解决办法|修复方案|解决方案|Fix|Solution)[:：]\s*(.*)", line, re.IGNORECASE)
        if m:
            solution = m.group(2) or line
            continue
        if problem and not solution:
            problem += " " + line
        elif solution:
            solution += " " + line
    return problem, solution


class MetisReviewer:
    """调用 Metis CLI 对内网代码进行 LLM 审查。"""

    def __init__(
        self,
        executable: str,
        config_path: str,
        tiktoken_cache_dir: str = "",
        timeout: int = 1800,
        max_concurrent: int = 4,
        skills_dir: str = "",
        work_dir: str = "",
    ) -> None:
        self.executable = executable
        self.config_path = config_path
        self.tiktoken_cache_dir = tiktoken_cache_dir
        self.timeout = int(timeout)
        self.max_concurrent = int(max_concurrent)
        self.skills_dir = skills_dir
        self.work_dir = work_dir or tempfile.gettempdir()
        self._semaphore = threading.Semaphore(self.max_concurrent)
        self._run_dir = os.path.join(self.work_dir, "metis-runs")
        os.makedirs(self._run_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    def review_native(
        self,
        change_id: str,
        repo_dir: str,
        files_with_status: list,
        commit_msg: str = "",
    ) -> Optional[dict]:
        """原生模式: 对变更文件做一次 Metis 安全审查, 返回 {"issues": [...]}。

        Metis 自带多维度安全审查 (内存/注入/越界/资源/竞态等透镜 + 验证 + 去重),
        无需按维度重复调用。失败返回 None。
        """
        target_files = self._changed_files(files_with_status)
        if not target_files:
            return None
        prompt_path = self._build_native_prompt_file(commit_msg, files_with_status)
        try:
            findings = self._review_files(
                change_id, repo_dir, target_files,
                prompt_path=prompt_path, dimension="native",
            )
            if not findings:
                return {"issues": []}
            return {"issues": self._to_issues(findings)}
        finally:
            if os.path.exists(prompt_path):
                os.remove(prompt_path)

    def review_dimensions(
        self,
        change_id: str,
        commit_msg: str,
        repo_dir: str,
        base_branch: str,
        files_with_status: list,
        head_sha: str,
        dimensions,
    ) -> dict:
        """并行运行各维度审查, 返回 {dim_name: result_dict}。

        result_dict 为 None 表示该维度失败 (与 Claude 路径语义一致)。
        """
        problem, solution = _parse_commit_msg(commit_msg)
        results: dict = {}
        lock = threading.Lock()

        def _run(dim):
            target_files = self._changed_files(files_with_status)
            if not target_files:
                with lock:
                    results[dim.name] = None
                return
            prompt_path = self._build_dimension_prompt_file(
                dim, problem, solution, files_with_status,
            )
            findings = self._review_files(
                change_id, repo_dir, target_files,
                prompt_path=prompt_path, dimension=dim.name,
            )
            with lock:
                results[dim.name] = self._convert_findings(findings, dim.name)
            if os.path.exists(prompt_path):
                os.remove(prompt_path)

        with ThreadPoolExecutor(max_workers=len(dimensions) or 1) as executor:
            futures = [executor.submit(_run, d) for d in dimensions]
            for f in futures:
                f.result()

        return results

    # ------------------------------------------------------------------
    # 文件范围与目录分组
    # ------------------------------------------------------------------

    @staticmethod
    def _changed_files(files_with_status: list) -> list:
        """取变更文件相对路径列表 (跳过删除的二进制文件)。"""
        target = []
        for status, fpath in files_with_status or []:
            if status == "D":
                continue
            p = str(fpath).replace("\\", "/").lstrip("./")
            if p:
                target.append(p)
        return target

    @staticmethod
    def _group_by_dir(target_files: list) -> list:
        """按父目录分组, 同一目录多个文件合并为一次 review_dir。"""
        groups: dict = {}
        for f in target_files:
            d = str(Path(f).parent) if Path(f).parent != Path(".") else "."
            groups.setdefault(d, []).append(f)
        return list(groups.values())

    # ------------------------------------------------------------------
    # Metis 调用
    # ------------------------------------------------------------------

    def _review_files(
        self,
        change_id: str,
        repo_dir: str,
        target_files: list,
        *,
        prompt_path: str,
        dimension: str,
    ) -> list:
        """对一组文件运行 Metis 审查, 返回扁平化 findings 列表。

        findings: [{file, line, severity, cwe, issue, reasoning, mitigation}]
        任一文件失败返回空列表 (该维度整体失败语义由调用方处理)。
        """
        all_findings = []
        groups = self._group_by_dir(target_files)
        for group in groups:
            # review_file/review_dir 按 CWD 解析路径, 统一传绝对路径
            if len(group) == 1:
                target = os.path.abspath(os.path.join(repo_dir, group[0]))
                command = f"review_file {_quote(target)}"
            else:
                target = os.path.abspath(os.path.join(repo_dir, Path(group[0]).parent))
                command = f"review_dir {_quote(target)}"
            out_path = os.path.join(
                self._run_dir,
                f"review_{dimension}_{len(all_findings)}_{int(time.time())}.json",
            )
            ok, raw = self._invoke_metis(
                change_id, repo_dir, command, prompt_path, out_path, dimension,
            )
            if not ok:
                logger.warning("[%s] Metis 审查失败: %s", dimension, command)
                continue
            try:
                with open(out_path, encoding="utf-8") as f:
                    payload = json.load(f)
                all_findings.extend(self._flatten_review(payload))
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("[%s] 解析 Metis 输出失败: %s", dimension, e)
                continue
        return all_findings

    def _invoke_metis(
        self,
        change_id: str,
        repo_dir: str,
        command: str,
        prompt_path: str,
        out_path: str,
        dimension: str,
    ) -> tuple:
        """执行一次 metis 调用 (信号量限流 + 超时 + stderr 日志)。"""
        if not self.executable or not os.path.isfile(self.executable):
            logger.warning("  Metis 可执行文件不存在: %s", self.executable)
            return False, ""
        if not os.path.isfile(self.config_path):
            logger.warning("  Metis 配置不存在: %s", self.config_path)
            return False, ""

        cmd = [
            self.executable,
            "--codebase-path", repo_dir,
            "--config", self.config_path,
            "--custom-prompt", prompt_path,
            "--non-interactive",
            "--command", command,
            "--output-file", out_path,
        ]

        env = os.environ.copy()
        if self.tiktoken_cache_dir:
            env["TIKTOKEN_CACHE_DIR"] = self.tiktoken_cache_dir
        if os.name == "nt":
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"

        dim_tag = f"[{dimension}] " if dimension else ""
        logger.info("  %sMetis 审查 (cwd=%s, cmd=%s, timeout=%ds)...",
                    dim_tag, repo_dir, command, self.timeout)

        started = time.time()
        self._semaphore.acquire()
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                cwd=self._run_dir,
                env=env,
            )
            try:
                stdout, stderr = proc.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                logger.error("  %sMetis 超时 (%ds), 已终止", dim_tag, self.timeout)
                return False, stderr
            elapsed = time.time() - started
            if proc.returncode != 0:
                logger.error("  %sMetis 返回 %d (%.0fs), stderr: %s",
                             dim_tag, proc.returncode, elapsed, (stderr or "")[-2000:])
                return False, stderr
            if stdout.strip():
                logger.debug("  %sMetis stdout: %s", dim_tag, stdout[:500])
            logger.info("  %sMetis 完成 (%.0fs)", dim_tag, elapsed)
            return True, stdout
        except Exception as e:
            logger.error("  %sMetis 调用异常: %s", dim_tag, e)
            return False, str(e)
        finally:
            self._semaphore.release()

    # ------------------------------------------------------------------
    # 结果转换
    # ------------------------------------------------------------------

    @staticmethod
    def _flatten_review(payload: dict) -> list:
        """把 metis review JSON 扁平化为 findings 列表。"""
        findings = []
        for file_entry in payload.get("reviews", []) or []:
            fname = file_entry.get("file") or file_entry.get("file_path") or ""
            for item in file_entry.get("reviews", []) or []:
                findings.append({
                    "file": item.get("primary_file") or fname,
                    "line": item.get("line_number") or 0,
                    "severity": (item.get("severity") or "unknown").lower(),
                    "cwe": item.get("cwe") or "",
                    "issue": item.get("issue") or "",
                    "reasoning": item.get("reasoning") or "",
                    "mitigation": item.get("mitigation") or "",
                })
        return findings

    def _convert_findings(self, findings: list, dim_name: str) -> Optional[dict]:
        """findings -> Claude 兼容结果 dict。"""
        if not findings:
            return {"issues": []}
        issues = self._to_issues(findings)
        if "评估" in dim_name:
            return self._build_verdict(issues)
        return {"issues": issues}

    @staticmethod
    def _to_issues(findings: list) -> list:
        """findings -> [{file, line, severity, message}]。"""
        issues = []
        for f in findings:
            sev = _SEVERITY_MAP.get(f["severity"], "warning")
            msg = f["issue"]
            if f.get("cwe"):
                msg += f" (CWE: {f['cwe']})"
            if f.get("mitigation"):
                msg += f" 建议: {f['mitigation']}"
            issues.append({
                "file": f["file"],
                "line": f["line"],
                "severity": sev,
                "message": msg,
            })
        return issues

    @staticmethod
    def _build_verdict(issues: list) -> dict:
        """问题解决评估维度: 由 findings 推导 verdict。"""
        errors = [i for i in issues if i["severity"] == "error"]
        warnings = [i for i in issues if i["severity"] == "warning"]
        if errors:
            return {
                "verdict": "未解决",
                "reason": f"发现 {len(errors)} 个严重问题, {len(warnings)} 个一般问题",
                "gaps": [i["message"] for i in errors],
                "side_effects": [],
            }
        if warnings:
            return {
                "verdict": "部分解决",
                "reason": f"发现 {len(warnings)} 个一般问题",
                "gaps": [i["message"] for i in warnings],
                "side_effects": [],
            }
        return {"verdict": "已解决", "reason": "未发现安全问题", "gaps": [], "side_effects": []}

    # ------------------------------------------------------------------
    # 维度提示词生成
    # ------------------------------------------------------------------

    def _build_native_prompt_file(self, commit_msg: str, files_with_status: list) -> str:
        """原生模式 custom-prompt: 变更上下文 (Metis 自带安全审查指令)。"""
        problem, solution = _parse_commit_msg(commit_msg)
        status_table = self._build_status_table(files_with_status)
        text = (
            "# Gerrit 变更安全审查\n\n"
            f"## 变更上下文\n"
            f"问题: {problem}\n方案: {solution}\n"
            f"{status_table}\n\n"
            "仅评审变更涉及的文件与代码, 优先关注新增/修改行及其与整个文件的交互。"
        )
        fd, path = tempfile.mkstemp(
            suffix=".md", prefix="metis_prompt_native_", dir=self._run_dir,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def _build_dimension_prompt_file(
        self, dim, problem: str, solution: str, files_with_status: list,
    ) -> str:
        """把维度 skills 内容 + 变更上下文写成 custom-prompt 文件。"""
        body = self._collect_skills(dim.folder)
        status_table = self._build_status_table(files_with_status)
        text = (
            f"# {dim.name} 评审指南\n\n"
            f"{body}\n\n"
            f"## 变更上下文\n"
            f"问题: {problem}\n方案: {solution}\n"
            f"{status_table}\n\n"
            "仅评审变更涉及的文件与代码。"
        )
        fd, path = tempfile.mkstemp(
            suffix=".md", prefix=f"metis_prompt_{dim.folder}_", dir=self._run_dir,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def _collect_skills(self, folder: str) -> str:
        """读取 .claude/skills/{folder}/*.md 的正文 (跳过 YAML frontmatter)。"""
        if not self.skills_dir:
            return ""
        base = Path(self.skills_dir) / folder
        if not base.is_dir():
            return ""
        parts = []
        for sf in sorted(base.rglob("*.md")):
            try:
                content = sf.read_text(encoding="utf-8")
            except OSError:
                continue
            _, body = self._parse_frontmatter(content)
            if body:
                parts.append(body.strip())
        return "\n\n".join(parts)

    @staticmethod
    def _parse_frontmatter(content: str) -> tuple:
        """解析 YAML frontmatter, 返回 (frontmatter, body)。"""
        content = content.replace("\r\n", "\n")
        if not content.startswith("---\n"):
            return {}, content
        idx = content.find("\n---\n")
        if idx == -1:
            return {}, content
        return content[4:idx].strip(), content[idx + 5:].strip()

    @staticmethod
    def _build_status_table(files_with_status) -> str:
        """生成变更文件清单 (与 poller 相同格式)。"""
        label = {"A": "[新增]", "M": "[修改]", "D": "[删除]", "R": "[重命名]", "C": "[复制]"}
        lines = ["## 变更文件清单"]
        for status, fpath in files_with_status or []:
            lines.append(f"  {label.get(status, status)} {fpath}")
        return "\n".join(lines)
