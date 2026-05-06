"""Tests for the local repository management module."""

import subprocess
import tempfile
from pathlib import Path

import pytest

from mcp_gerrit_server.local_repo import LocalRepo, GitCommandError


def _git(cwd, *args):
    """Run a git command in the given working directory."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


@pytest.fixture
def test_repo():
    """Create a temporary git repo with base and feature branches."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir) / "test-repo"
        repo_path.mkdir()

        _git(repo_path, "init")
        _git(repo_path, "config", "user.email", "test@test.com")
        _git(repo_path, "config", "user.name", "Test")

        # Create base branch with a file
        base_file = repo_path / "main.c"
        base_file.write_text("#include <stdio.h>\nint main() { return 0; }\n")
        _git(repo_path, "add", "main.c")
        _git(repo_path, "commit", "-m", "base")

        # Create feature branch
        _git(repo_path, "checkout", "-b", "feature")
        feature_file = repo_path / "main.c"
        feature_file.write_text("#include <stdio.h>\nint main() { printf(\"hello\"); return 0; }\n")
        _git(repo_path, "add", "main.c")
        _git(repo_path, "commit", "-m", "add printf")

        yield str(repo_path), "master", "feature"


class TestLocalRepo:
    def test_init(self):
        repo = LocalRepo(repo_path="/some/path")
        assert str(repo.repo_path).endswith("some/path") or str(repo.repo_path).endswith("some\\path")

    def test_get_diff(self, test_repo):
        repo_path, base, feature = test_repo
        repo = LocalRepo(repo_path=repo_path)
        diff = repo.get_diff(base, feature)
        assert isinstance(diff, str)
        assert len(diff) > 0

    def test_list_changed_files(self, test_repo):
        repo_path, base, feature = test_repo
        repo = LocalRepo(repo_path=repo_path)
        files = repo.list_changed_files(base, feature)
        assert isinstance(files, list)
        assert len(files) > 0
        assert any("main.c" in f[1] for f in files)

    def test_get_file_content(self, test_repo):
        repo_path, base, feature = test_repo
        repo = LocalRepo(repo_path=repo_path)
        content = repo.get_file_content(feature, "main.c")
        assert isinstance(content, str)
        assert "printf" in content

    def test_get_file_content_with_lines(self, test_repo):
        repo_path, base, feature = test_repo
        repo = LocalRepo(repo_path=repo_path)
        content = repo.get_file_content(feature, "main.c", lines=(1, 2))
        assert isinstance(content, str)
        assert len(content.splitlines()) <= 2

    def test_get_commit_message(self, test_repo):
        repo_path, base, feature = test_repo
        repo = LocalRepo(repo_path=repo_path)
        msg = repo.get_commit_message(feature)
        assert isinstance(msg, str)
        assert len(msg) > 0

    def test_is_shallow(self, test_repo):
        repo_path, base, feature = test_repo
        repo = LocalRepo(repo_path=repo_path)
        assert not repo.is_shallow()

    def test_ensure_clone_existing(self, test_repo):
        repo_path, base, feature = test_repo
        repo = LocalRepo(repo_path=repo_path)
        result = repo.ensure_clone()
        assert result is False

    def test_gc(self, test_repo):
        repo_path, base, feature = test_repo
        repo = LocalRepo(repo_path=repo_path)
        repo.gc()

    def test_disk_usage(self, test_repo):
        repo_path, base, feature = test_repo
        repo = LocalRepo(repo_path=repo_path)
        usage = repo.disk_usage_mb()
        assert isinstance(usage, float)
        assert usage >= 0

    def test_nonexistent_repo(self):
        repo = LocalRepo(repo_path="/tmp/nonexistent-path-12345")
        repo.gc()
