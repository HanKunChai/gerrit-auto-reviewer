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
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import List, Optional
from urllib.parse import quote

# 确保项目根目录在 sys.path 中
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from mcp_gerrit_server.config import load_config
from mcp_gerrit_server.gerrit_client import GerritClient, parse_gerrit_response
from mcp_gerrit_server.local_repo import LocalRepo
from review_rules.engine import ReviewEngine

# Metis 适配器 (scripts 目录加入 sys.path)
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
from metis_reviewer import MetisReviewer

logger = logging.getLogger("auto-review-poller")

# 领域问题修改表缓存: {month: {"file_path": str, "j_column_values": list, "download_time": float}}
_real_archive_cache: dict = {}
_real_archive_cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".archive_cache")
os.makedirs(_real_archive_cache_dir, exist_ok=True)

# SVN凭据（通过命令行参数或环境变量设置）
_svn_username: Optional[str] = None
_svn_password: Optional[str] = None
# SVN 归档表根路径（由 config.yaml 的 svn.archive_base 覆盖）
_svn_archive_base: str = "svn://svn.example.com:3690/repo/archive"


def _get_svn_revision(svn_path: str, username: str, password: str) -> Optional[str]:
    """获取SVN路径的最新修订版本号"""
    try:
        cmd = ["svn", "info", "--non-interactive", "--username", username, "--password", password, svn_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, encoding='utf-8', errors='replace')
        if result.returncode != 0:
            cmd = ["svn", "info", "--non-interactive", svn_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, encoding='utf-8', errors='replace')
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("Revision:"):
                    return line.split(":")[1].strip()
        else:
            logger.warning("SVN info 失败 (rc=%d): %s", result.returncode, svn_path)
    except subprocess.TimeoutExpired:
        logger.warning("SVN info 超时: %s", svn_path)
    except Exception as e:
        logger.warning("SVN info 异常 %s: %s", e, svn_path)
    return None


def _download_realm_archive_table(month: str, table_path: str, username: str, password: str) -> Optional[tuple]:
    """下载领域问题修改表，返回(j列值列表, 下载时间)或None"""
    temp_dir = tempfile.mkdtemp()
    try:
        cmd = ["svn", "export", "--non-interactive", "--username", username, "--password", password, table_path, temp_dir]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, encoding='utf-8', errors='replace')
        if result.returncode != 0:
            cmd = ["svn", "export", "--non-interactive", table_path, temp_dir]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, encoding='utf-8', errors='replace')
        if result.returncode != 0:
            logger.warning("无法导出领域问题修改表: %s", result.stderr)
            return None

        xls_files = [f for f in os.listdir(temp_dir) if f.endswith(('.xls', '.xlsx'))]
        if not xls_files:
            return None

        xls_path = os.path.join(temp_dir, xls_files[0])
        import xlrd

        workbook = xlrd.open_workbook(xls_path)
        sheet = workbook.sheet_by_index(0)

        j_column_values = []
        for row_idx in range(sheet.nrows):
            try:
                cell_value = sheet.cell_value(row_idx, 9)
                if cell_value:
                    j_column_values.append(str(cell_value))
            except:
                continue

        # 缓存到本地文件
        cache_file = os.path.join(_real_archive_cache_dir, f"realm_archive_{month}.txt")
        with open(cache_file, 'w', encoding='utf-8') as f:
            f.write(str(int(time.time())) + "\n")
            for val in j_column_values:
                f.write(val + "\n")

        return (j_column_values, time.time())
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _load_cached_archive_table(month: str) -> Optional[tuple]:
    """从本地缓存加载领域问题修改表"""
    cache_file = os.path.join(_real_archive_cache_dir, f"realm_archive_{month}.txt")
    if not os.path.exists(cache_file):
        return None

    try:
        with open(cache_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            if len(lines) < 2:
                return None
            download_time = float(lines[0].strip())
            j_column_values = [line.strip() for line in lines[1:] if line.strip()]
            return (j_column_values, download_time)
    except Exception:
        return None


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


def _parse_svn_path(msg: str) -> Optional[str]:
    """从 commit message 中提取 SVN 路径。

    搜索整个commit message中第一个以svn://开头的路径，
    可能出现在"报告相关路径"、"需求/问题归档"等字段中。
    支持中文路径的匹配。
    """
    if not msg:
        return None
    # 匹配svn://开头到行尾的所有内容（支持中文路径和空格）
    match = re.search(r'(svn://[^\n]+)', msg, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def _check_commit_material(svn_path: str) -> tuple:
    """检查 SVN 路径下是否包含必需的三个文档。

    返回: (is_complete: bool, message: str, missing_docs: list)
    """
    if not svn_path:
        return (False, "未找到报告相关路径", ["报告相关路径为空"])

    # SVN 认证信息
    svn_username = _svn_username or ""
    svn_password = _svn_password or ""

    try:
        # 使用 svn list 获取文件列表
        cmd = [
            "svn", "list", "--non-interactive", "--username", svn_username, "--password", svn_password,
            svn_path
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            encoding='utf-8', errors='replace'
        )

        if result.returncode != 0:
            # SVN 命令失败，尝试不带认证的匿名访问
            cmd = ["svn", "list", "--non-interactive", svn_path]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
                encoding='utf-8', errors='replace'
            )
            if result.returncode != 0:
                return (False, f"SVN路径无法访问: {svn_path}", ["SVN访问失败"])

        file_list = result.stdout
        logger.debug("SVN路径 %s 文件列表:\n%s", svn_path, file_list)

        # 检查必需的三个文档
        required_docs = {
            "方案设计": False,  # 方案设计文档
            "checklist": False,  # checklist 自检表
            "自测报告": False,  # 自测报告文档
        }

        for line in file_list.splitlines():
            line_lower = line.lower()
            if "方案" in line and "设计" in line:
                required_docs["方案设计"] = True
            elif "checklist" in line_lower or "自检表" in line:
                required_docs["checklist"] = True
            elif "自测报告" in line:
                required_docs["自测报告"] = True

        missing = [k for k, v in required_docs.items() if not v]
        if missing:
            return (False, f"缺少提交材料: {', '.join(missing)}", missing)
        else:
            return (True, "提交材料齐全", [])

    except subprocess.TimeoutExpired:
        return (False, "SVN查询超时", ["SVN访问超时"])
    except Exception as e:
        logger.warning("检查提交材料失败: %s", e)
        return (False, f"检查提交材料失败: {str(e)}", ["检查失败"])


def _check_realm_problem_archive(svn_path: str, commit_msg: str) -> tuple:
    """检查领域问题修改源是否已归档到领域问题修改表。

    当"报告相关路径"包含"/领域问题修改源"时，需要检查该提交是否记录在
    对应月份的"领域问题修改表.xls"中。支持缓存机制，检查是否为最新版本。

    返回: (is_archived: bool, message: str)
    """
    if not svn_path or "领域问题修改源" not in svn_path:
        return (True, "")  # 不需要检查

    # SVN 认证信息
    svn_username = _svn_username or ""
    svn_password = _svn_password or ""

    try:
        # 从SVN路径中提取年份和月份
        # 路径格式: .../2026/202604/领域问题修改源/... 或 .../2026/202605/领域问题修改源/...
        match = re.search(r'/(\d{4})/(\d{4,6})/', svn_path)
        if not match:
            return (False, "无法从报告相关路径中提取年份月份")

        year = match.group(1)  # 2026
        month = match.group(2)  # 202604

        # 构造领域问题修改表的SVN路径 (根路径由 config.yaml 的 svn.archive_base 配置)
        table_path = (
            f"{_svn_archive_base.rstrip('/')}/{year}/{month}/"
            f"领域问题修改表.xls"
        )

        # 获取SVN最新修订版本号
        table_dir = os.path.dirname(table_path)
        current_revision = _get_svn_revision(table_dir, svn_username, svn_password)

        # 检查缓存是否有效
        cache_file = os.path.join(_real_archive_cache_dir, f"realm_archive_{month}.txt")
        cache_rev_file = os.path.join(_real_archive_cache_dir, f"realm_archive_{month}.rev")

        cached_data = _load_cached_archive_table(month)
        need_download = True
        is_cache_valid = False

        if cached_data:
            # 检查SVN版本是否变化
            if current_revision and os.path.exists(cache_rev_file):
                try:
                    with open(cache_rev_file, 'r') as f:
                        cached_rev = f.read().strip()
                    if cached_rev == current_revision:
                        is_cache_valid = True
                        need_download = False
                        logger.info("领域问题修改表缓存有效(Revision: %s)", current_revision)
                except Exception:
                    pass

        if need_download:
            logger.info("下载领域问题修改表: %s (当前SVN Revision: %s)", table_path, current_revision)
            result = _download_realm_archive_table(month, table_path, svn_username, svn_password)
            if not result:
                return (False, f"无法读取领域问题修改表: {month}")

            # 保存SVN版本号
            if current_revision:
                with open(cache_rev_file, 'w') as f:
                    f.write(current_revision)

            j_column_values, _ = result
        else:
            j_column_values, _ = cached_data

        # 从commit message中提取"功能说明/问题现象"或"功能说明"
        report_name = ""
        for line in commit_msg.splitlines():
            line = line.strip()
            m = re.match(r'^(功能说明/问题现象|功能说明|问题现象)[:：]\s*(.*)', line, re.IGNORECASE)
            if m:
                report_name = m.group(2).strip()
                break

        if not report_name:
            return (False, "无法从提交中提取报告名称")

        logger.info("待检查报告名称: %s", report_name)

        # 检查J列中是否包含报告名称
        found = False
        for cell_value in j_column_values:
            if report_name in cell_value or cell_value in report_name:
                found = True
                break

        if found:
            return (True, f"✓ 已归档到领域问题修改表({month})")
        else:
            return (False, f"⚠ 该笔提交尚未归档到领域问题修改表({month})")

    except subprocess.TimeoutExpired:
        return (False, "领域问题修改表查询超时")
    except Exception as e:
        logger.warning("检查领域问题修改表失败: %s", e)
        return (False, f"检查领域问题修改表失败: {str(e)}")


def _check_requirement_archive(svn_path: str, commit_msg: str) -> tuple:
    """检查需求源是否已归档到产品需求表。

    当"报告相关路径"包含"/需求源"时，需要检查该提交是否记录在
    对应月份的"产品需求表.xls"中。

    返回: (is_archived: bool, message: str)
    """
    if not svn_path or "需求源" not in svn_path:
        return (True, "")  # 不需要检查

    # SVN 认证信息
    svn_username = _svn_username or ""
    svn_password = _svn_password or ""

    try:
        # 从SVN路径中提取年份和月份
        # 路径格式: .../2026/202604/需求源/... 或 .../2027/202705/需求源/...
        match = re.search(r'/(\d{4})/(\d{4,6})/', svn_path)
        if not match:
            return (False, "无法从报告相关路径中提取年份月份")

        year = match.group(1)
        month = match.group(2)

        # 构造产品需求表的SVN路径 (根路径由 config.yaml 的 svn.archive_base 配置)
        table_path = (
            f"{_svn_archive_base.rstrip('/')}/{year}/{month}/"
            f"产品需求表.xls"
        )

        logger.info("检查产品需求表: %s", table_path)

        # 创建临时目录下载表格
        import tempfile
        import shutil

        temp_dir = tempfile.mkdtemp()
        try:
            # 使用svn导出表格文件
            cmd = [
                "svn", "export", "--non-interactive", "--username", svn_username, "--password", svn_password,
                table_path, temp_dir
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
                encoding='utf-8', errors='replace'
            )

            if result.returncode != 0:
                # 尝试不带认证
                cmd = ["svn", "export", "--non-interactive", table_path, temp_dir]
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=120,
                    encoding='utf-8', errors='replace'
                )
                if result.returncode != 0:
                    logger.warning("无法导出产品需求表: %s", result.stderr)
                    return (False, f"无法读取产品需求表: {month}")

            # 查找下载的xls文件
            xls_files = [f for f in os.listdir(temp_dir) if f.endswith(('.xls', '.xlsx'))]
            if not xls_files:
                return (False, f"产品需求表文件不存在: {month}")

            xls_path = os.path.join(temp_dir, xls_files[0])

            # 读取Excel文件的J列
            try:
                import xlrd
            except ImportError:
                logger.warning("xlrd未安装，尝试安装...")
                subprocess.run(["pip", "install", "xlrd"], check=False)
                try:
                    import xlrd
                except ImportError:
                    return (False, "无法读取Excel文件(xlrd未安装)")

            workbook = xlrd.open_workbook(xls_path)
            sheet = workbook.sheet_by_index(0)

            # J列是第9列（索引从0开始）
            j_column_values = []
            for row_idx in range(sheet.nrows):
                try:
                    cell_value = sheet.cell_value(row_idx, 9)  # J列索引为9
                    if cell_value:
                        j_column_values.append(str(cell_value))
                except:
                    continue

            # 从commit message中提取"功能说明/问题现象"或"功能说明"
            report_name = ""
            for line in commit_msg.splitlines():
                line = line.strip()
                m = re.match(r'^(功能说明/问题现象|功能说明|问题现象)[:：]\s*(.*)', line, re.IGNORECASE)
                if m:
                    report_name = m.group(2).strip()
                    break

            if not report_name:
                return (False, "无法从提交中提取报告名称")

            logger.info("待检查报告名称: %s", report_name)

            # 检查J列中是否包含报告名称
            found = False
            for cell_value in j_column_values:
                if report_name in cell_value or cell_value in report_name:
                    found = True
                    break

            if found:
                return (True, f"✓ 已归档到产品需求表({month})")
            else:
                return (False, f"⚠ 该笔提交尚未归档到产品需求表({month})")

        finally:
            # 清理临时目录
            shutil.rmtree(temp_dir, ignore_errors=True)

    except subprocess.TimeoutExpired:
        return (False, "产品需求表查询超时")
    except Exception as e:
        logger.warning("检查产品需求表失败: %s", e)
        return (False, f"检查产品需求表失败: {str(e)}")


class AutoReviewPoller:
    """Gerrit 自动评审轮询器。

    周期性查询 Gerrit 中等待评审的变更，自动执行代码评审并提交结果。
    """

    def __init__(self, mock: bool = False, config_path: Optional[str] = None, target_url: Optional[str] = None):
        self.config = load_config(config_path)
        self.mock = mock
        self._target_url = target_url  # 指定评审的 Gerrit URL
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
        self.REVIEW_TAG = f"autogenerated:gerrit:auto-review:{self.config.auto_review.reviewer_version}"
        prompt_path = os.path.join(
            PROJECT_DIR, "review_prompts", "c_review_prompt_fast.md",
        )
        try:
            with open(prompt_path, encoding="utf-8") as f:
                self._prompt_template = f.read()
        except FileNotFoundError:
            logger.warning("评审模板未找到: %s", prompt_path)

        # Build skill registry (scan .claude/skills/{folder}/*.md)
        self._skill_registry = self._build_skill_registry()
        ar = self.config.auto_review
        if ar.use_multi_dimension and ar.dimensions:
            total = sum(
                len(self._skill_registry.get(d.folder, []))
                for d in ar.dimensions
            )
            logger.info("多维度评审: %d 维度, %d skills 已注册",
                         len(ar.dimensions), total)

        # Metis 内网审查器 (替代 Claude Code)
        self._metis_reviewer = None
        if ar.use_metis:
            skills_dir = os.path.join(PROJECT_DIR, ".claude", "skills")
            self._metis_reviewer = MetisReviewer(
                executable=ar.metis.executable,
                config_path=ar.metis.config,
                tiktoken_cache_dir=ar.metis.tiktoken_cache_dir,
                timeout=ar.metis.timeout,
                max_concurrent=ar.metis.max_concurrent,
                skills_dir=skills_dir,
                work_dir=os.path.join(PROJECT_DIR, "logs"),
            )
            logger.info("使用 Metis 内网 LLM 审查: %s", ar.metis.executable)

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

        # 如果指定了 URL，则直接评审该变更
        if self._target_url:
            return self._poll_single_url()

        if self.mock:
            logger.info("[Mock] 查询变更...")
            changes = self._mock_changes()
        else:
            # 直接使用配置的查询，过滤已评审变更由客户端 _already_reviewed 方法处理
            # 注意: Gerrit 不支持 tag: 查询操作符，因此改用客户端过滤
            query = ar.query
            logger.info("查询变更: %s", query)
            changes = self._client.list_changes(
                query=query,
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

    def _poll_single_url(self) -> int:
        """根据指定的 Gerrit URL 评审单个变更。"""
        url = self._target_url
        logger.info("指定评审 URL: %s", url)

        # 解析 URL: http://gerrit/c/project/+/12345 或 /c/project/+/12345
        # 格式: /c/{project}/+/{change_number}
        import re
        # 匹配 /c/PROJECT/+/{number}
        match = re.search(r'/c/([^/]+)/\+/(\d+)', url)
        if not match:
            logger.error("无法解析 Gerrit URL (格式: /c/project/+/change_number): %s", url)
            return 0

        project = match.group(1)
        change_number = match.group(2)

        logger.info("解析到项目: %s, 变更号: %s", project, change_number)

        try:
            # 使用 list_changes 按 project + change 查询
            query = f"project:{project} change:{change_number}"
            logger.info("查询变更: %s", query)
            changes = self._client.list_changes(query=query, limit=1)

            if not changes:
                logger.error("未找到变更: %s", query)
                return 0

            change_info = changes[0]
            # 获取 change_id 格式: project~branch~Ixxxxx
            change_id = change_info.get("id", "")
            subject = change_info.get("subject", "")

            # 获取最新 revision
            revisions = change_info.get("revisions", {})
            if not revisions:
                logger.error("变更无 revisions: %s", change_id)
                return 0

            rev_info = list(revisions.values())[0]
            rev_num = str(rev_info.get("_number", "1"))

            logger.info("评审变更: %s", change_id)
            logger.info("  项目: %s, 标题: %s", project, subject)

            # 直接调用评审任务
            self._review_task({
                "id": change_id,
                "_number": change_number,  # 注意是 _number 不是 number
                "subject": subject,
                "project": project,
                "revisions": revisions,
            })
            return 1
        except Exception as e:
            logger.error("评审指定变更失败: %s", e, exc_info=True)
            return 0

    def _review_change(
        self, change_id: str, change_number: str, rev_num: str,
        project: str, rev_id: str, subject: str = "",
    ) -> None:
        """评审单个变更。"""
        # 1. 获取 diff
        logger.info("  [1/3] 获取 diff...")
        repo = None
        worktree_dir = None
        base_branch = "master"
        commit_msg = subject
        sha = ""
        if self.mock:
            diff_text = self._mock_diff()
            files = self._mock_files()
            repo_dir = None
        else:
            repo = self._ensure_local_repo(project)
            try:
                change_info = self._client.get_change(change_id)
                logger.info("  [DEBUG] get_change返回keys: %s", list(change_info.keys()))
                base_branch = change_info.get("branch", base_branch)
                subject = change_info.get("subject", "")
                revisions = change_info.get("revisions", {})
                logger.info("  [DEBUG] revisions keys: %s", list(revisions.keys()) if revisions else "empty")
                if revisions:
                    rev_data = list(revisions.values())[0]
                    logger.info("  [DEBUG] rev_data keys: %s", list(rev_data.keys()))
                    commit = rev_data.get("commit", {})
                    logger.info("  [DEBUG] commit keys: %s", list(commit.keys()) if commit else "empty")

                    # 如果commit对象为空，尝试通过Gerrit API获取完整的commit message
                    if not commit or not commit.get("message"):
                        logger.info("  [DEBUG] commit对象为空，通过revision API获取完整commit message")
                        try:
                            # 使用GET /changes/{change_id}/revisions/{revision_id}/commit获取完整commit
                            revision_id = rev_data.get("_number", "current")
                            commit_info = self._client._get(
                                f"/changes/{change_id}/revisions/{revision_id}/commit"
                            )
                            commit_data = parse_gerrit_response(commit_info.text)
                            commit_msg = commit_data.get("message", subject) if commit_data else subject
                            logger.info("  [DEBUG] 从revision API获取commit message: %d 字符", len(commit_msg) if commit_msg else 0)
                        except Exception as api_err:
                            logger.warning("获取commit message失败: %s", api_err)
                            commit_msg = subject
                    else:
                        commit_msg = commit.get("message", subject)

                    logger.debug("commit message length: %d", len(commit_msg) if commit_msg else 0)
                    logger.info("  [DEBUG] 完整commit message: %s", commit_msg[:500] if commit_msg else "None")
            except Exception as e:
                logger.warning("获取完整commit message失败: %s", e)
            repo.ensure_branch(base_branch)
            sha = repo.fetch_change(change_number, rev_num)
            diff_text = repo.get_diff(base_branch, head_ref=sha, context_lines=3)
            files = repo.list_changed_files(base_branch, head_ref=sha)
            logger.info("  fetch SHA: %s", sha[:8] if sha else "N/A")

            # 创建独立 worktree，Claude 在里面 cat 源文件读到的是变更后版本
            worktree_dir = repo.create_worktree(sha, change_number, rev_num)
            repo_dir = worktree_dir

        # finally 中会用到, 必须在第一个 return 之前初始化
        diff_file = None
        try:
            logger.info("  diff: %d 行, %d 个文件", len(diff_text.splitlines()), len(files))
            for status, fpath in files:
                logger.info("    [%s] %s", status, fpath)

            if not diff_text.strip():
                logger.warning("  变更无 diff 内容，跳过")
                return

            # 将 diff 写入临时文件，Claude 通过 cat 读取（避免注入 prompt 撑爆上下文）
            if not self.mock and repo_dir and diff_text:
                diff_file = os.path.join(repo_dir, "_review_diff.txt")
                try:
                    with open(diff_file, "w", encoding="utf-8") as f:
                        f.write(diff_text)
                except OSError:
                    diff_file = None

            material_check_result = ""
            material_issues = []

            # SVN 提交材料检查（仅在启用时）
            if getattr(self.config, "svn", None) and self.config.svn.enabled:
                svn_path = _parse_svn_path(commit_msg)
                # 1.5 检查提交材料是否齐全（报告相关路径中的三个必需文档）
                if self.config.svn.check_commit_material:
                    logger.info("  [1.5] 检查提交材料完整性...")
                    if svn_path:
                        is_complete, msg, missing = _check_commit_material(svn_path)
                        if is_complete:
                            logger.info("  提交材料检查通过: %s", msg)
                            material_check_result = f"✓ 提交材料齐全"
                        else:
                            logger.warning("  提交材料检查未通过: %s", msg)
                            material_check_result = f"⚠ {msg}"
                            material_issues = [{"file": "commit-material-check", "line": 0, "severity": "warning", "message": msg}]
                    else:
                        logger.warning("  未找到报告相关路径")
                        material_check_result = "⚠ 未在提交日志中找到报告相关路径"
                        material_issues = [{"file": "commit-material-check", "line": 0, "severity": "warning", "message": "未找到报告相关路径"}]

                # 1.6 检查领域问题修改表归档情况
                if self.config.svn.check_realm_archive and svn_path and "领域问题修改源" in svn_path:
                    logger.info("  [1.6] 检查领域问题修改表归档...")
                    is_archived, archive_msg = _check_realm_problem_archive(svn_path, commit_msg)
                    if is_archived:
                        logger.info("  领域问题修改表: %s", archive_msg)
                        material_check_result = (material_check_result + "\n" if material_check_result else "") + archive_msg
                    else:
                        logger.warning("  领域问题修改表: %s", archive_msg)
                        material_check_result = (material_check_result + "\n" if material_check_result else "") + archive_msg
                        material_issues.append({"file": "realm-problem-archive-check", "line": 0, "severity": "warning", "message": archive_msg})

                # 1.7 检查需求源归档情况
                if self.config.svn.check_requirement_archive and svn_path and "需求源" in svn_path:
                    logger.info("  [1.7] 检查产品需求表归档...")
                    is_archived, archive_msg = _check_requirement_archive(svn_path, commit_msg)
                    if is_archived:
                        logger.info("  产品需求表: %s", archive_msg)
                        material_check_result = (material_check_result + "\n" if material_check_result else "") + archive_msg
                    else:
                        logger.warning("  产品需求表: %s", archive_msg)
                        material_check_result = (material_check_result + "\n" if material_check_result else "") + archive_msg
                        material_issues.append({"file": "requirement-archive-check", "line": 0, "severity": "warning", "message": archive_msg})

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

            # 3. Metis / Claude Code AI 评审
            changed_files = [f for _, f in files]
            ar = self.config.auto_review

            if ar.use_metis and self._metis_reviewer is not None:
                if ar.metis.review_mode == "native":
                    logger.info("  [3/4] Metis 原生安全审查 (变更文件 %d 个)...",
                                len([f for _, f in files if f]))
                    result = self._metis_reviewer.review_native(
                        change_id, repo_dir, files, commit_msg,
                    )
                    if not result:
                        logger.error("  Metis 评审失败，跳过上报 (变更: %s)", change_id)
                        return
                    dimension_results = {"Metis 安全审查": result}
                    logger.info("  Metis 评审: %d 个发现",
                                len(result.get("issues", [])))
                else:
                    logger.info("  [3/4] Metis 多维度并行评审 (%d 维度)...", len(ar.dimensions))
                    dimension_results = self._metis_reviewer.review_dimensions(
                        change_id, commit_msg, repo_dir, base_branch, files, sha,
                        ar.dimensions,
                    )
                    valid = {k: v for k, v in dimension_results.items() if v}
                    if not valid:
                        logger.error("  所有维度 Metis 评审均失败，跳过上报 (变更: %s)", change_id)
                        return
                    logger.info("  Metis 评审: %d/%d 维度完成", len(valid), len(ar.dimensions))
                    for dn, dr in dimension_results.items():
                        status = "OK" if dr else "FAIL"
                        logger.info("    [%s] %s", status, dn)
            elif ar.use_multi_dimension and ar.dimensions:
                logger.info("  [3/4] 多维度并行评审 (%d 维度)...", len(ar.dimensions))
                dimension_results = self._invoke_claude_dimensions(
                    change_id, commit_msg, repo_dir, base_branch, files, sha,
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
                    files_with_status=files,
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
                material_check=material_check_result,
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

        finally:
            # 清理临时 diff 文件
            if diff_file:
                try:
                    os.remove(diff_file)
                except OSError:
                    pass

            # 清理 worktree
            if worktree_dir and repo:
                try:
                    repo.remove_worktree(worktree_dir)
                except Exception:
                    pass

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
        files_with_status: Optional[List[tuple]] = None,
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
            status_table = self._build_status_table(files_with_status or []) if files_with_status else ""
            prompt = (
                f"{self._prompt_template}\n\n"
                f"问题: {problem}\n方案: {solution}\n"
                f"{status_table}\n"
                f"第一步必须执行: cat _review_diff.txt 读取diff内容"
            )

        ar = self.config.auto_review
        timeout = ar.dimension_timeout if dimension else ar.claude_timeout

        dim_tag = f"[{dimension}] " if dimension else ""
        logger.info("  %s调用 Claude Code (cwd=%s, prompt=%d chars, timeout=%ds)...",
                    dim_tag, repo_dir, len(prompt), timeout)

        env = os.environ.copy()
        env["CLAUDE_CODE_SKIP_AUTH"] = "1"

        cmd = [
            claude_bin, "-p", prompt,
            "--output-format", "json",
            "--dangerously-skip-permissions",
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
            fh = RotatingFileHandler(
                os.path.join(log_dir, "claude_debug.log"),
                encoding="utf-8", maxBytes=10*1024*1024, backupCount=3,
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

            # Extract review JSON from Claude Code's wrapper output
            parsed = self._extract_review_json(output)

            claude_logger.debug("[done] rc=0 elapsed=%.0fs stdout=%d stderr=%d",
                                elapsed, len(output), len(stderr_lines))
            logger.info("  Claude 完成 (%.0fs, %d 字符)", elapsed, len(output))
            return parsed

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
        content = content.replace("\r\n", "\n")
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
        """Scan .claude/skills/{folder}/*.md recursively, build folder->[skills] map."""
        skills_dir = Path(PROJECT_DIR) / ".claude" / "skills"
        registry = {}
        if not skills_dir.is_dir():
            logger.warning("Skills 目录不存在: %s", skills_dir)
            return registry
        for folder in sorted(skills_dir.iterdir()):
            if not folder.is_dir():
                continue
            folder_skills = []
            for sf in sorted(folder.rglob("*.md")):
                try:
                    content = sf.read_text(encoding="utf-8")
                    fm, body = self._parse_skill_frontmatter(content)
                    fm["name"] = fm.get("name", sf.stem)
                    folder_skills.append({
                        "path": str(sf),
                        "frontmatter": fm,
                        "body": body,
                        "rel_path": str(sf.relative_to(folder)),
                    })
                except Exception:
                    logger.warning("无法加载 skill 文件: %s", sf, exc_info=True)
            if folder_skills:
                registry[folder.name] = folder_skills
        return registry

    def _build_status_table(self, files_with_status) -> str:
        """Build a file status table so Claude knows what was added/modified/deleted."""
        label = {"A": "[新增]", "M": "[修改]", "D": "[删除]", "R": "[重命名]", "C": "[复制]"}
        lines = ["## 变更文件清单"]
        for status, fpath in files_with_status:
            lines.append(f"  {label.get(status, status)} {fpath}")
        lines.append("")
        lines.append("diff 格式: `-` 开头=删除的旧代码, `+` 开头=新增代码, 空格开头=上下文（未变更的行）。")
        lines.append("评审时需结合上下文理解变更意图，不要只看 `+` 行。")
        return "\n".join(lines)

    def _build_dimension_prompt(
        self, dimension, problem: str, solution: str,
        files_with_status: list, base_branch: str, head_sha: str,
    ) -> str:
        """Build a prompt referencing skills (loaded natively by Claude Code from worktree)."""
        folder_skills = self._skill_registry.get(dimension.folder, [])
        if not folder_skills:
            logger.warning("  维度 %s 文件夹 %s 无 skill", dimension.name, dimension.folder)
            return ""

        status_table = self._build_status_table(files_with_status)

        needs_ctx = any(
            s.get("frontmatter", {}).get("needs_context") for s in folder_skills
        )
        ctx_instruction = ""
        if needs_ctx:
            ctx_instruction = (
                f"发现任何潜在问题时，必须 cat 源文件阅读完整函数上下文后再下结论，\n"
                f"不能仅凭 diff 的 3 行片断定论。\n\n"
            )

        return (
            f"你是嵌入式 C 代码评审专家。请使用 CLAUDE.md 中加载的 skills 进行评审。\n\n"
            f"## 变更上下文\n"
            f"问题: {problem}\n"
            f"方案: {solution}\n"
            f"{status_table}\n"
            f"第一步必须执行: cat _review_diff.txt 读取diff内容\n\n"
            f"{ctx_instruction}"
            f"## 输出格式（--output-format json，必须输出纯JSON，禁止任何其他文本）\n"
            + (
                f"```json\n"
                f'{{"verdict": "已解决|部分解决|未解决", "reason": "一句话原因", "gaps": ["遗漏项"], "side_effects": ["副作用"]}}\n'
                f"```\n"
                f"gaps/side_effects 为空时输出 []。\n"
                if "评估" in dimension.name else
                f"```json\n"
                f'{{"issues": [{{"file": "src/x.c", "line": 15, "severity": "error", "message": "简短原因"}}]}}\n'
                f"```\n"
                f"无问题时 issues 为空数组。severity 只取 error 或 warning。\n"
            )
        )

    def _ensure_skill_files(self, repo_dir: str, dimensions) -> None:
        """Copy skill folder trees to worktree for Claude Code native loading."""
        if not repo_dir:
            return
        dst_base = Path(repo_dir) / ".claude" / "skills"
        try:
            dst_base.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        for dim in dimensions:
            folder = dim.folder
            src_dir = Path(PROJECT_DIR) / ".claude" / "skills" / folder
            if not src_dir.is_dir():
                continue
            dst_dir = dst_base / folder
            try:
                if dst_dir.exists():
                    shutil.rmtree(dst_dir)
                shutil.copytree(str(src_dir), str(dst_dir))
            except OSError:
                pass

    def _generate_claude_md(self, repo_dir: str, dimensions) -> None:
        """Write CLAUDE.md at worktree root with all dimension skills referenced."""
        lines = ["# Review Skills", ""]
        for dim in dimensions:
            folder_skills = self._skill_registry.get(dim.folder, [])
            for skill in folder_skills:
                rel = skill.get("rel_path", "")
                lines.append(f"@.claude/skills/{dim.folder}/{rel}")
        lines.append("")
        try:
            cm_path = os.path.join(repo_dir, "CLAUDE.md")
            with open(cm_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except OSError:
            pass

    def _invoke_claude_dimensions(
        self, change_id: str, commit_msg: str,
        repo_dir: str, base_branch: str,
        files_with_status: list, head_sha: str,
    ) -> dict:
        """Run all configured dimensions in parallel, return {dim_name: result}."""
        ar = self.config.auto_review
        problem, solution = _parse_commit_msg(commit_msg)

        # Copy skills to worktree + generate CLAUDE.md
        self._ensure_skill_files(repo_dir, ar.dimensions)
        self._generate_claude_md(repo_dir, ar.dimensions)

        results = {}
        lock = threading.Lock()

        def _run(dim):
            prompt = self._build_dimension_prompt(
                dim, problem, solution, files_with_status, base_branch, head_sha,
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

        # Strip thinking XML blocks (line-based, only complete pairs)
        in_thinking = False
        has_close = False
        result_lines = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if re.match(r"</?thinking", stripped, re.IGNORECASE):
                if stripped.startswith("<thinking"):
                    in_thinking = True
                    has_close = any(
                        re.match(r"</thinking", lines[j].strip(), re.IGNORECASE)
                        for j in range(i + 1, len(lines))
                    )
                else:
                    in_thinking = False
                continue
            if in_thinking and has_close:
                continue
            result_lines.append(line)
        lines = result_lines

        cleaned = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            # Always keep lines with file:line pattern
            if re.search(r"\S+:\d+", stripped):
                cleaned.append(stripped)
                continue
            # Keep conclusion lines
            if any(stripped.startswith(p) for p in ("[已解决", "[部分解决", "[未解决")):
                cleaned.append(stripped)
                continue
            # Skip lines that look like thinking/analysis
            if re.match(r"^(思考|分析|推理|评估过程|检查步骤|第一步|第二步|首先|然后|接下来|最后|综上|总结)", stripped):
                continue
            # Skip long prose lines
            if len(stripped) > 60:
                continue
            cleaned.append(stripped)
        return "\n".join(cleaned) if cleaned else (text or "")

    def _extract_review_json(self, raw: str):
        """Extract review JSON from Claude Code --output-format json wrapper.

        The outer wrapper is: {type, result: "<think>...</think>\\n```json\\n{...}\\n```", ...}
        We need to strip thinking blocks and extract the actual review JSON.
        Returns parsed dict or raw str on failure.
        """
        # Parse outer JSON wrapper
        try:
            wrapper = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw  # not JSON, return raw text

        # Get the actual Claude response text
        result_text = ""
        if isinstance(wrapper, dict):
            result_text = wrapper.get("result", "")
        if not result_text:
            return raw  # no result field, return wrapper itself (might be direct JSON)

        # Strip thinking XML blocks
        text = result_text
        text = re.sub(r"<think[^>]*>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
        # Handle incomplete thinking block (no closing tag)
        text = re.sub(r"<think[^>]*>.*", "", text, flags=re.DOTALL | re.IGNORECASE)

        # Extract JSON from ```json code block
        m = re.search(r"```(?:json)?\s*\n?(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except (json.JSONDecodeError, ValueError):
                pass

        # Try parsing the cleaned text directly as JSON
        try:
            return json.loads(text.strip())
        except (json.JSONDecodeError, ValueError):
            pass

        # Fallback: return text without thinking blocks
        cleaned = re.sub(r"```[^`]*```", "", text).strip()
        return cleaned or raw

    def _format_verdict(self, d: dict) -> str:
        """Format a problem-solving verdict dict into clean text."""
        v = d.get("verdict", "?")
        r = d.get("reason", "")
        text = f"[{v}] {r}"
        for g in d.get("gaps", []) or []:
            text += f"\n  遗漏: {g}"
        for s in d.get("side_effects", []) or []:
            text += f"\n  副作用: {s}"
        return text

    def _format_issues(self, d: dict) -> str:
        """Format issues list into clean text."""
        items = d.get("issues", []) or []
        if not items:
            return "无问题。"
        lines = []
        for item in items:
            f = item.get("file", "?")
            ln = item.get("line", 0)
            sev = item.get("severity", "warning")
            msg = item.get("message", "")
            lines.append(f"{f}:{ln} [{sev}] {msg}")
        return "\n".join(lines)

    def _build_review_message(
        self, change_id: str, subject: str, issues,
        claude_review = None, material_check: str = None,
    ) -> str:
        """构造评审消息。

        *claude_review* can be a str (single-dimension, backward-compat),
        a dict of ``{dim_name: result}`` (multi-dimension), or None.
        Each result can be a parsed JSON dict or a raw str (fallback).
        *material_check* is the commit material check result string.
        """
        errors = [i for i in issues if i.severity == "error"]
        warnings = [i for i in issues if i.severity == "warning"]
        infos = [i for i in issues if i.severity == "info"]

        lines = [
            "========== 自动代码评审结果 ==========",
            "",
            f"变更: {change_id}",
            f"标题: {subject}",
            "",
        ]

        # 添加提交材料检查结果
        if material_check:
            lines.append("### 提交材料检查")
            lines.append("")
            lines.append(material_check)
            lines.append("")

        if isinstance(claude_review, dict):
            for dim_name, dim_result in claude_review.items():
                lines.append(f"--- {dim_name} ---")
                lines.append("")
                if isinstance(dim_result, dict):
                    if "verdict" in dim_result:
                        lines.append(self._format_verdict(dim_result))
                    elif "issues" in dim_result:
                        lines.append(self._format_issues(dim_result))
                    else:
                        lines.append(str(dim_result))
                elif isinstance(dim_result, str) and dim_result:
                    lines.append(self._strip_thinking(dim_result))
                else:
                    lines.append("(该维度评审未完成)")
                lines.append("")
        elif claude_review:
            if isinstance(claude_review, dict):
                if "verdict" in claude_review:
                    lines.append(self._format_verdict(claude_review))
                elif "issues" in claude_review:
                    lines.append(self._format_issues(claude_review))
                else:
                    lines.append(str(claude_review))
            elif isinstance(claude_review, str):
                lines.append("--- Claude Code 评审 ---")
                lines.append("")
                lines.append(self._strip_thinking(claude_review))
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

        lines.append("----------")
        lines.append(f"(Gerrit Auto-Review {self.config.auto_review.reviewer_version})")
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
    parser.add_argument("--url", type=str, help="指定评审的 Gerrit URL (如 http://gerrit/c/project/+/12345)")
    parser.add_argument("--svn-user", type=str, help="SVN用户名")
    parser.add_argument("--svn-password", type=str, help="SVN密码")
    args = parser.parse_args()

    # 初始化日志系统
    log_file = setup_logging(args.verbose)

    poller = AutoReviewPoller(mock=args.mock, config_path=args.config, target_url=args.url)

    # 收集SVN凭据（CLI参数 > 环境变量 > config.yaml）
    global _svn_username, _svn_password, _svn_archive_base
    if poller.config.svn.enabled:
        _svn_username = args.svn_user or os.environ.get("SVN_USER") or poller.config.svn.username
        _svn_password = args.svn_password or os.environ.get("SVN_PASSWORD") or poller.config.svn.password
        if poller.config.svn.archive_base:
            _svn_archive_base = poller.config.svn.archive_base
        if _svn_username:
            logger.info("SVN集成已启用 (username=%s)", _svn_username)
    else:
        logger.info("SVN集成未启用 (svn.enabled=false)")
    if args.url:
        logger.info("指定评审 URL: %s", args.url)

    # 启动时清理上次未清除的孤儿 worktree（进程崩溃/被kill后残留）
    review_base = os.path.join(poller.config.repo.local_path, "_review")
    if os.path.isdir(review_base):
        removed = 0
        for wt in list(os.scandir(review_base)):
            try:
                shutil.rmtree(wt.path)
                removed += 1
            except OSError:
                pass
        if removed:
            logger.info("清理 %d 个孤儿 worktree", removed)

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
        logger.info("收到退出信号, 立即停止...")
        poller._executor.shutdown(wait=False)
        logger.info("Auto-Review Poller 已停止")


if __name__ == "__main__":
    main()
