"""Configuration loader with .env file support."""

import os
from pathlib import Path
from typing import Optional
import yaml

_CONFIG = None


def _load_dotenv(path: Optional[str] = None):
    """Load .env file if it exists (no external dependency)."""
    env_file = Path(path) if path else Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip("\"'")
            if key not in os.environ:  # env var takes precedence
                os.environ[key] = val


class Config:
    def __init__(self, path: Optional[str] = None):
        _load_dotenv()

        path = path or os.environ.get("CONFIG_PATH", "")
        if not path:
            path = str(Path(__file__).resolve().parent.parent / "config.yaml")
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        self.mode = data.get("mode", "local_test")
        self.gerrit = GerritConfig(data.get("gerrit", {}))
        self.auto_review = AutoReviewConfig(data.get("auto_review", {}))
        self.repo = RepoConfig(data.get("repo", {}))
        self.local_test = LocalTestConfig(data.get("local_test", {}))
        self.webhook = WebhookConfig(data.get("webhook", {}))
        self.review = ReviewConfig(data.get("review", {}))
        self.rules = RulesConfig(data.get("rules", {}))
        self.cache = CacheConfig(data.get("cache", {}))
        self.svn = SvnConfig(data.get("svn", {}))

    @property
    def is_mock(self) -> bool:
        return self.mode == "local_test"


class GerritConfig:
    def __init__(self, data: dict):
        self.base_url = data.get("base_url", "")
        self.username = data.get("auth", {}).get("username", "")
        self._password = os.environ.get("GERRIT_PASSWORD", os.environ.get("gerrit_password", ""))
        self.use_a_prefix = data.get("auth", {}).get("use_a_prefix", True)

    @property
    def password(self) -> str:
        return self._password


class DimensionConfig:
    def __init__(self, data: dict):
        self.name = data.get("name", "")
        self.folder = data.get("folder", "")


class MetisConfig:
    """Metis 内网 LLM 审查器配置 (替代 Claude Code)。

    review_mode:
      native      - 原生模式: 每个变更一次 Metis 审查 (覆盖安全/内存/逻辑类漏洞,
                    推荐用于 C/C++ Gerrit 评审; Metis 自带多维度安全审查)
      dimensions  - 维度模式: 按 config.auto_review.dimensions 并行逐维度审查
                    (仅对非 C/C++ 语言的 simple_llm_review 路径生效)
    """

    def __init__(self, data: dict):
        self.executable = data.get("executable", "")
        self.config = data.get("config", "")
        self.tiktoken_cache_dir = data.get("tiktoken_cache_dir", "")
        self.timeout = data.get("timeout", 1800)
        self.max_concurrent = data.get("max_concurrent", 4)
        self.review_mode = data.get("review_mode", "native")


class AutoReviewConfig:
    def __init__(self, data: dict):
        self.enabled = data.get("enabled", True)
        self.poll_interval = data.get("poll_interval", 120)
        self.reviewer = data.get("reviewer", "code-reviewer")
        self.query = data.get("query", "reviewer:code-reviewer+status:open")
        self.max_changes_per_poll = data.get("max_changes_per_poll", 50)
        self.use_rules_engine = data.get("use_rules_engine", False)
        self.max_concurrent_reviews = data.get("max_concurrent_reviews", 2)
        self.claude_timeout = data.get("claude_timeout", 1800)
        self.use_multi_dimension = data.get("use_multi_dimension", True)
        self.dimension_timeout = data.get("dimension_timeout", 1800)
        self.max_concurrent_claude = data.get("max_concurrent_claude", 4)
        self.reviewer_version = data.get("reviewer_version", "v6")
        self.use_metis = data.get("use_metis", False)
        self.metis = MetisConfig(data.get("metis", {}))
        self.dimensions = [
            DimensionConfig(d) for d in data.get("dimensions", [])
        ]


class RepoConfig:
    def __init__(self, data: dict):
        self.local_path = data.get("local_path", "./local-repo")
        self.remote_url = data.get("remote_url", "")
        self.gerrit_remote = data.get("gerrit_remote", "gerrit")
        self.gerrit_push_url = data.get("gerrit_push_url", "")
        self.fallback_project = data.get("fallback_project", "default")
        self.initial_clone_depth = data.get("initial_clone_depth", 10)
        self.auto_deepen = data.get("auto_deepen", True)
        self.deepen_step = data.get("deepen_step", 100)


class LocalTestConfig:
    def __init__(self, data: dict):
        self.repo_path = data.get("repo_path", "./test-data/test-repo")
        self.base_branch = data.get("base_branch", "base")
        self.feature_branch = data.get("feature_branch", "feature")


class WebhookConfig:
    def __init__(self, data: dict):
        self.enabled = data.get("enabled", True)
        self.host = data.get("host", "0.0.0.0")
        self.port = data.get("port", 8081)
        self.max_concurrent_reviews = data.get("max_concurrent_reviews", 3)


class ReviewConfig:
    def __init__(self, data: dict):
        self.skip_binary_files = data.get("skip_binary_files", True)
        self.skip_file_patterns = data.get("skip_file_patterns", ["*.o", "*.bin", "*.a"])
        self.min_review_score = data.get("min_review_score", -2)
        self.max_review_score = data.get("max_review_score", 2)


class RulesConfig:
    def __init__(self, data: dict):
        self.enabled = data.get("enabled", True)
        self.custom_rules_dir = data.get("custom_rules_dir", "./review_rules/custom")


class CacheConfig:
    def __init__(self, data: dict):
        self.enabled = data.get("enabled", True)
        self.dir = data.get("dir", "./.cache/review-cache")
        self.max_size_mb = data.get("max_size_mb", 500)
        self.ttl_hours = data.get("ttl_hours", 24)


class SvnConfig:
    def __init__(self, data: dict):
        self.enabled = data.get("enabled", False)
        self.username = data.get("username", "")
        self.password = data.get("password", "")
        self.archive_base = data.get("archive_base", "")
        self.check_commit_material = data.get("check_commit_material", True)
        self.check_realm_archive = data.get("check_realm_archive", True)
        self.check_requirement_archive = data.get("check_requirement_archive", True)


def load_config(path: Optional[str] = None) -> Config:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = Config(path)
    return _CONFIG


def reload_config(path: Optional[str] = None) -> Config:
    global _CONFIG
    _CONFIG = Config(path)
    return _CONFIG
