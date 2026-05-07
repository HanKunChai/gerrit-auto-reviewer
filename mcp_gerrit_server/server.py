"""MCP Gerrit Review Server."""

import argparse
import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse

from mcp_gerrit_server.mcp_compat import (
    InitializationOptions,
    Server,
    TextContent,
    Tool,
)

from mcp_gerrit_server.cache import ReviewCache
from mcp_gerrit_server.config import load_config

logger = logging.getLogger("mcp-gerrit-review")

_SERVER: Optional["GerritReviewServer"] = None


class GerritReviewServer:
    """MCP server for Gerrit code review."""

    def __init__(self, mock: bool = False, config_path: Optional[str] = None):
        self.config = load_config(config_path)
        self.mock = mock
        self.server = Server("gerrit-review")
        self.cache = ReviewCache(self.config.cache)
        self._gerrit_client = None
        self._local_repo = None
        self._mock_client = None
        self._mock_thread = None

        if self.mock:
            self._setup_mock()
        else:
            self._setup_production()

        self._register_tools()

    def _setup_mock(self):
        """Initialize mock components."""
        logger.info("Starting in MOCK mode")
        from mcp_gerrit_server import mock_api

        self._mock_app = mock_api.app
        self._mock_client = mock_api.app.test_client()

    def _setup_production(self):
        """Initialize production components."""
        logger.info("Starting in PRODUCTION mode")
        from mcp_gerrit_server.gerrit_client import GerritClient

        gc = self.config.gerrit
        if gc.password:
            logger.info("Auth configured: user=%s password_len=%d", gc.username, len(gc.password))
        else:
            logger.warning("No GERRIT_PASSWORD set! Authentication will fail. Create .env or set env var.")
        auth = (gc.username, gc.password) if gc.username and gc.password else None
        self._gerrit_client = GerritClient(
            gc.base_url, auth=auth,
            use_a_prefix=gc.use_a_prefix,
        )

    def _ensure_local_repo(self, project: str = "default"):
        """Get or create local repo for the given Gerrit project."""
        from mcp_gerrit_server.local_repo import LocalRepo

        rc = self.config.repo
        repo_path = os.path.join(rc.local_path, project)
        base_url = self.config.gerrit.base_url.rstrip("/")
        plain_url = f"{base_url}/{project}"

        # 优先: 用 Gerrit HTTP 密码构建 git URL
        gc = self.config.gerrit
        if gc.username and gc.password:
            parsed = urlparse(base_url)
            pwd = quote(gc.password, safe="")
            auth_url = (
                f"{parsed.scheme}://{gc.username}:{pwd}"
                f"@{parsed.netloc}{parsed.path}/{project}"
            )
        else:
            auth_url = plain_url

        key = f"local_repo_{project}"
        cached = getattr(self, key, None)
        if cached:
            return cached

        def _make_repo(url):
            return LocalRepo(
                repo_path=repo_path,
                remote_url=url,
                gerrit_remote=rc.gerrit_remote,
                gerrit_push_url=plain_url,
                initial_depth=rc.initial_clone_depth,
            )

        repo = _make_repo(auth_url)
        try:
            repo.ensure_clone()
        except Exception:
            if auth_url == plain_url:
                raise
            logger.warning("HTTP 凭证 clone 失败，尝试用本地 git 凭证...")
            repo = _make_repo(plain_url)
            repo.ensure_clone()

        setattr(self, key, repo)
        if project == "default":
            self._local_repo = repo
        return repo

    GERRIT_MAGIC = ")]}'\n"

    def _mock_changes(self, status="open", limit=10):
        """Get changes from mock API via test client."""
        resp = self._mock_client.get(f"/changes/?q=status:{status}&n={limit}")
        raw = resp.get_data(as_text=True)
        if raw.startswith(self.GERRIT_MAGIC):
            raw = raw[len(self.GERRIT_MAGIC):]
        return json.loads(raw)

    def _mock_post_review(self, change_id, revision, message, score, comments):
        """Post review to mock API via test client."""
        body = {
            "message": message,
            "labels": {"Code-Review": score},
            "comments": comments,
        }
        resp = self._mock_client.post(
            f"/changes/{change_id}/revisions/{revision}/review",
            json=body,
        )
        raw = resp.get_data(as_text=True)
        if raw.startswith(self.GERRIT_MAGIC):
            raw = raw[len(self.GERRIT_MAGIC):]
        return {"status": resp.status_code, "body": json.loads(raw)}

    def _register_tools(self):
        """Register all MCP tools."""

        @self.server.list_tools()
        async def list_tools() -> List[Tool]:
            return [
                Tool(
                    name="list_changes",
                    description="List pending changes from Gerrit (or mock data)",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "status": {"type": "string", "default": "open"},
                            "limit": {"type": "integer", "default": 10},
                        },
                    },
                ),
                Tool(
                    name="fetch_diff",
                    description="Fetch the diff of a specific change from local repo",
                    inputSchema={
                        "type": "object",
                        "required": ["change_id"],
                        "properties": {
                            "change_id": {"type": "string", "description": "Gerrit change ID"},
                            "revision": {"type": "string", "default": "1"},
                            "base_branch": {"type": "string", "default": "master"},
                        },
                    },
                ),
                Tool(
                    name="get_file_context",
                    description="Get file content from local repo at a specific commit",
                    inputSchema={
                        "type": "object",
                        "required": ["ref", "file_path"],
                        "properties": {
                            "ref": {"type": "string"},
                            "file_path": {"type": "string"},
                            "start_line": {"type": "integer"},
                            "end_line": {"type": "integer"},
                        },
                    },
                ),
                Tool(
                    name="post_review",
                    description="Post a review to Gerrit (or mock)",
                    inputSchema={
                        "type": "object",
                        "required": ["change_id", "message", "score"],
                        "properties": {
                            "change_id": {"type": "string"},
                            "revision": {"type": "string", "default": "current"},
                            "message": {"type": "string"},
                            "score": {"type": "integer", "description": "-2 to +2"},
                            "comments": {
                                "type": "object",
                                "description": "{file_path: [{line, message}]}",
                            },
                        },
                    },
                ),
                Tool(
                    name="repo_status",
                    description="Show local repository sync status",
                    inputSchema={"type": "object", "properties": {}},
                ),
                Tool(
                    name="run_rules",
                    description="Run review rules on a diff",
                    inputSchema={
                        "type": "object",
                        "required": ["diff_text"],
                        "properties": {
                            "diff_text": {"type": "string"},
                            "file_path": {"type": "string"},
                        },
                    },
                ),
                Tool(
                    name="c_review_prompt",
                    description="Get the C code review prompt template",
                    inputSchema={"type": "object", "properties": {}},
                ),
            ]

        @self.server.call_tool()
        async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
            handlers = {
                "list_changes": self._handle_list_changes,
                "fetch_diff": self._handle_fetch_diff,
                "get_file_context": self._handle_get_file_context,
                "post_review": self._handle_post_review,
                "repo_status": self._handle_repo_status,
                "run_rules": self._handle_run_rules,
                "c_review_prompt": self._handle_c_review_prompt,
            }
            handler = handlers.get(name)
            if not handler:
                raise ValueError(f"Unknown tool: {name}")
            try:
                result = await handler(arguments)
                return [TextContent(type="text", text=json.dumps(result, indent=2))]
            except Exception as e:
                logger.exception("Tool %s failed", name)
                return [TextContent(type="text", text=json.dumps({"error": str(e)}, indent=2))]

    async def _handle_list_changes(self, args: dict) -> list:
        status = args.get("status", "open")
        limit = args.get("limit", 10)
        if self.mock:
            return self._mock_changes(status=status, limit=limit)
        return self._gerrit_client.list_changes(status=status, limit=limit)

    async def _handle_fetch_diff(self, args: dict) -> dict:
        change_id = args["change_id"]
        revision = args.get("revision", "1")
        base_branch = args.get("base_branch", "master")

        if self.mock:
            test_config = self.config.local_test
            from mcp_gerrit_server.local_repo import LocalRepo
            repo = LocalRepo(repo_path=test_config.repo_path)
            repo.ensure_clone()
            base = test_config.base_branch
            feature = test_config.feature_branch
            diff = repo.get_diff(base, feature)
            files = repo.list_changed_files(base, feature)
            return {
                "change_id": change_id,
                "diff": diff,
                "files": [{"status": s, "file_path": f} for s, f in files],
                "mode": "local_test",
            }

        # Get project from Gerrit change info for auto URL construction
        project = "default"
        change_number = change_id  # fallback: use raw input
        try:
            change_info = self._gerrit_client.get_change(change_id)
            project = change_info.get("project", "default")
            # 提取数字编号用于 refs/changes 路径
            change_number = str(change_info.get("_number", change_id))
        except Exception:
            project = self.config.repo.fallback_project

        repo = self._ensure_local_repo(project=project)
        repo.ensure_branch(base_branch)
        sha = repo.fetch_change(change_number, revision)
        diff = repo.get_diff(base_branch, head_ref=sha)
        files = repo.list_changed_files(base_branch, head_ref=sha)
        return {
            "change_id": change_id,
            "revision": revision,
            "project": project,
            "fetch_sha": sha,
            "diff": diff,
            "files": [{"status": s, "file_path": f} for s, f in files],
        }

    async def _handle_get_file_context(self, args: dict) -> dict:
        repo = self._ensure_local_repo(project=args.get("project", "default"))
        ref = args["ref"]
        file_path = args["file_path"]
        lines = None
        if args.get("start_line") and args.get("end_line"):
            lines = (args["start_line"], args["end_line"])
        try:
            content = repo.get_file_content(ref, file_path, lines=lines)
            return {"ref": ref, "file_path": file_path, "content": content}
        except Exception as e:
            return {"error": f"Failed to get file context: {e}"}

    async def _handle_post_review(self, args: dict) -> dict:
        change_id = args["change_id"]
        revision = args.get("revision", "current")
        message = args["message"]
        score = args["score"]
        comments = args.get("comments", {})

        if self.mock:
            result = self._mock_post_review(change_id, revision, message, score, comments)
            return {"result": "mock_review_posted", "data": result}

        result = self._gerrit_client.post_review(
            change_id, revision, message=message, score=score, comments=comments,
        )
        self.cache.put(change_id, revision, {
            "score": score, "message": message, "cached_at": 0,
        })
        return {"result": "review_posted", "gerrit_response": result}

    async def _handle_repo_status(self, args: dict) -> dict:
        repo = self._ensure_local_repo()
        return {
            "repo_path": str(repo.repo_path),
            "is_shallow": repo.is_shallow(),
            "disk_usage_mb": repo.disk_usage_mb(),
            "mode": "mock" if self.mock else "production",
        }

    async def _handle_run_rules(self, args: dict) -> dict:
        diff_text = args["diff_text"]
        file_path = args.get("file_path")

        from review_rules.engine import ReviewEngine

        engine = ReviewEngine()
        engine.load_builtin_rules()
        custom_dir = self.config.rules.custom_rules_dir
        if os.path.isdir(custom_dir):
            engine.load_rules(custom_dir)

        issues = engine.run(diff_text, file_path=file_path)

        return {
            "issues": [
                {
                    "file": i.file,
                    "line": i.line,
                    "severity": i.severity,
                    "message": i.message,
                    "rule_id": i.rule_id,
                    "category": i.category,
                }
                for i in issues
            ],
            "total_issues": len(issues),
            "errors": sum(1 for i in issues if i.severity == "error"),
            "warnings": sum(1 for i in issues if i.severity == "warning"),
        }

    async def _handle_c_review_prompt(self, args: dict) -> dict:
        prompt_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "review_prompts", "c_review_prompt.md",
        )
        try:
            with open(prompt_path, encoding="utf-8") as f:
                content = f.read()
            return {"prompt": content, "source": "c_review_prompt.md"}
        except FileNotFoundError:
            return {"error": "c_review_prompt.md not found"}

    async def run(self):
        async with self.server.run(
            InitializationOptions(
                server_name="gerrit-review",
                server_version="0.1.0",
            )
        ) as server:
            await server.wait_closed()


def main():
    parser = argparse.ArgumentParser(description="Gerrit MCP Review Server")
    parser.add_argument("--mock", action="store_true", help="Run in mock mode")
    parser.add_argument("--config", type=str, help="Path to config.yaml")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    global _SERVER
    _SERVER = GerritReviewServer(mock=args.mock, config_path=args.config)
    asyncio.run(_SERVER.run())


if __name__ == "__main__":
    main()
