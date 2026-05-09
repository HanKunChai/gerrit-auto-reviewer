"""
Local git repository manager for Gerrit MCP Auto-Review.

Manages a shallow clone of the codebase under review, fetches Gerrit change
refs, and computes diffs locally. This avoids re-cloning for every review
while keeping the local clone size bounded.
"""

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_URL_CREDS_RE = __import__("re").compile(r"(://)[^@]+(@)")


def _sanitize_url(url: str) -> str:
    """Strip credentials from a URL for safe logging."""
    return _URL_CREDS_RE.sub(r"\1***:***\2", url)


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class GitCommandError(Exception):
    """Raised when a git subprocess command exits with a non-zero status."""

    def __init__(
        self,
        cmd: list,
        returncode: int,
        stdout: str = "",
        stderr: str = "",
    ):
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        cmd_str = " ".join(str(a) for a in cmd)
        super().__init__(
            f"'git {cmd_str}' failed with exit code {returncode}"
        )


# ---------------------------------------------------------------------------
# LocalRepo
# ---------------------------------------------------------------------------


def _setup_gerrit_remote(repo: "LocalRepo") -> None:
    """Ensure the gerrit fetch remote exists and has the correct URL."""
    if not repo.gerrit_push_url or repo.gerrit_remote == "origin":
        return
    try:
        existing = repo._run_git(
            ["remote", "get-url", repo.gerrit_remote], check=True,
        ).stdout.strip()
        if existing != repo.gerrit_push_url:
            logger.info(
                "updating remote %s from %s to %s",
                repo.gerrit_remote, _sanitize_url(existing),
                _sanitize_url(repo.gerrit_push_url),
            )
            repo._run_git([
                "remote", "set-url",
                repo.gerrit_remote, repo.gerrit_push_url,
            ])
    except GitCommandError:
        logger.info("adding remote %s -> %s",
                    repo.gerrit_remote, _sanitize_url(repo.gerrit_push_url))
        repo._run_git([
            "remote", "add",
            repo.gerrit_remote, repo.gerrit_push_url,
        ])


class LocalRepo:
    """Manage a local git clone of the codebase under review.

    The repository is cloned once (shallow), and individual Gerrit change refs
    are fetched on demand.  Diffs are computed locally against the base ref,
    avoiding repeated cloning for every review.

    Parameters
    ----------
    repo_path : str
        Local filesystem path for the clone.
    remote_url : str or None
        Upstream git URL used for the initial clone.  Required when the local
        copy does not yet exist.
    gerrit_remote : str
        Name of the Gerrit remote (default ``"gerrit"``).  This remote is
        added after the initial clone if ``gerrit_push_url`` is provided.
    gerrit_push_url : str or None
        SSH URL for fetching Gerrit change refs.  If set, the gerrit remote
        will be configured with this URL.
    initial_depth : int
        Shallow clone depth used during ``ensure_clone()``.
    """

    def __init__(
        self,
        repo_path: str,
        remote_url: Optional[str] = None,
        gerrit_remote: str = "gerrit",
        gerrit_push_url: Optional[str] = None,
        initial_depth: int = 10,
    ) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.remote_url = remote_url
        self.gerrit_remote = gerrit_remote
        self.gerrit_push_url = gerrit_push_url
        self.initial_depth = initial_depth

    # ------------------------------------------------------------------
    # Internal helper
    # ------------------------------------------------------------------

    def _run_git(
        self,
        cmd_args: list,
        check: bool = True,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess:
        """Execute a git subprocess inside *repo_path*.

        Parameters
        ----------
        cmd_args : list of str
            Arguments passed to the ``git`` binary (e.g. ``["status"]``).
        check : bool
            If True (default), raise :class:`GitCommandError` on non-zero
            exit.
        timeout : int
            Maximum seconds to wait for the command.  Default is 30 s;
            callers that fetch over the network should pass a higher value
            (e.g. 120 s).

        Returns
        -------
        subprocess.CompletedProcess
            The completed process result.

        Raises
        ------
        GitCommandError
            If *check* is True and the process exits with a non-zero status,
            or if the process times out.
        """
        cmd = ["git"] + cmd_args
        env = os.environ.copy()

        # Git commands that create the repository (e.g. ``clone``) must not
        # have *cwd* pointing inside the yet-to-be-created directory.
        cwd = str(self.repo_path) if self.repo_path.exists() else None

        logger.debug("running git %s (cwd=%s, timeout=%d)",
                      " ".join(str(a) for a in cmd_args), cwd, timeout)

        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                env=env,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise GitCommandError(
                cmd_args,
                -1,
                stderr=f"git command timed out after {timeout}s",
            )

        if check and result.returncode != 0:
            logger.error("git command failed (exit %d): %s\nstderr: %s",
                         result.returncode,
                         " ".join(str(a) for a in cmd_args),
                         result.stderr.strip())
            raise GitCommandError(
                cmd_args, result.returncode, result.stdout, result.stderr,
            )

        return result

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ensure_clone(self) -> bool:
        """Ensure the local clone exists and is a valid git repository.

        If *repo_path* does not exist or is not a valid git repository, a
        shallow clone is created from *remote_url*.  After cloning, the
        gerrit remote is added if *gerrit_push_url* was provided.

        Returns
        -------
        bool
            True if a new clone was created; False if the repository already
            existed.

        Raises
        ------
        ValueError
            If *remote_url* is None and cloning is required.
        GitCommandError
            If any git command fails.
        """
        repo_dir = self.repo_path
        created = False

        if repo_dir.exists():
            # Verify the existing directory is a valid git repository.
            try:
                self._run_git(["rev-parse", "--git-dir"])
                # Repo exists — still ensure gerrit remote is configured
                _setup_gerrit_remote(self)
                return created  # Repository already exists and is valid
            except GitCommandError:
                # Exists but is not a git repository -- remove and re-clone.
                logger.warning(
                    "%s exists but is not a git repository; removing",
                    repo_dir,
                )
                shutil.rmtree(repo_dir)

        # -- Clone -------------------------------------------------------
        if self.remote_url is None:
            raise ValueError(
                "remote_url is required for initial clone"
            )

        logger.info("cloning %s into %s (depth=%d)",
                    _sanitize_url(self.remote_url), repo_dir, self.initial_depth)

        self._run_git(
            [
                "clone", "--depth", str(self.initial_depth),
                self.remote_url, str(repo_dir),
            ],
            timeout=120,
        )
        created = True

        # -- Set up gerrit remote ---------------------------------------
        _setup_gerrit_remote(self)

        return created

    def ensure_branch(self, branch: str) -> None:
        """Fetch a branch from origin if it doesn't exist locally.

        Shallow clones only contain the default branch; other branches
        (e.g. release branches) need to be fetched explicitly before
        ``git diff`` can reference them.
        """
        result = self._run_git(
            ["rev-parse", "--verify", f"refs/heads/{branch}"],
            check=False,
        )
        if result.returncode == 0:
            return  # Already exists locally
        logger.info("fetching base branch %s from origin", branch)
        self._run_git(
            ["fetch", "origin", f"{branch}:refs/heads/{branch}"],
            timeout=120,
        )

    def fetch_change(self, change_id: str, revision: str = "1") -> str:
        """Fetch a specific Gerrit change ref and return the commit SHA.

        Fetches to a named local ref (``refs/review/{change_id}/{revision}``)
        instead of relying on ``FETCH_HEAD``, so that concurrent fetch
        operations on the same repo do not race on a shared mutable ref.

        The Gerrit change ref is constructed as::

            refs/changes/{last2}/{change_id}/{revision}

        where ``last2`` is the last two digits of *change_id*, zero-padded
        to two characters.

        Parameters
        ----------
        change_id : str
            Gerrit numeric change ID (e.g. ``"12345"``).
        revision : str
            Patch-set revision number (default ``"1"``).

        Returns
        -------
        str
            The SHA of the fetched commit.

        Raises
        ------
        GitCommandError
            If the fetch fails (e.g. change does not exist, network error).
        """
        last2 = change_id[-2:].zfill(2)
        change_ref = f"refs/changes/{last2}/{change_id}/{revision}"
        local_ref = f"refs/review/{change_id}/{revision}"

        logger.info("fetching change ref %s -> %s from remote %s",
                    change_ref, local_ref, self.gerrit_remote)

        self._run_git(
            ["fetch", self.gerrit_remote, f"{change_ref}:{local_ref}"],
            timeout=120,
        )

        sha = self._run_git(
            ["rev-parse", local_ref],
        ).stdout.strip()

        logger.info("fetched change %s/%s at %s", change_id, revision, sha)
        return sha

    def get_diff(
        self,
        base_ref: str,
        head_ref: str = "FETCH_HEAD",
        context_lines: int = 10,
    ) -> str:
        """Compute the unified diff between *base_ref* and *head_ref*.

        Uses the three-dot symmetric difference notation
        (``base_ref...head_ref``) so that only changes on the head side
        are shown.

        Parameters
        ----------
        base_ref : str
            Base revision (branch, tag, or commit SHA).
        head_ref : str
            Head revision (default ``"FETCH_HEAD"``, which is updated by
            :meth:`fetch_change`).
        context_lines : int
            Number of context lines shown around each change hunk
            (default 10).

        Returns
        -------
        str
            Unified diff output.
        """
        result = self._run_git([
            "diff",
            f"-U{context_lines}",
            f"{base_ref}...{head_ref}",
        ])
        return result.stdout

    def get_file_content(
        self,
        ref: str,
        file_path: str,
        lines: Optional[Tuple[int, int]] = None,
    ) -> str:
        """Retrieve the contents of a file at a given revision.

        Parameters
        ----------
        ref : str
            Revision (branch, tag, commit SHA, or ``"FETCH_HEAD"``).
        file_path : str
            Path to the file within the repository (relative to repo root).
        lines : tuple of (int, int) or None
            Optional 1-indexed line range ``(start, end)`` inclusive.
            If None, the entire file is returned.

        Returns
        -------
        str
            File content, possibly restricted to the requested line range.

        Raises
        ------
        GitCommandError
            If the file does not exist at the given revision.
        """
        result = self._run_git(["show", f"{ref}:{file_path}"])
        content = result.stdout

        if lines is not None:
            start, end = lines
            all_lines = content.splitlines(keepends=True)
            # start/end are 1-indexed and inclusive; Python slicing is
            # 0-indexed with exclusive end -- so [start-1 : end] is correct.
            selected = all_lines[start - 1:end]
            content = "".join(selected)

        return content

    def list_changed_files(
        self,
        base_ref: str,
        head_ref: str = "FETCH_HEAD",
    ) -> List[Tuple[str, str]]:
        """List files changed between *base_ref* and *head_ref*.

        Uses ``git diff --name-status`` with the three-dot notation.

        Parameters
        ----------
        base_ref : str
            Base revision.
        head_ref : str
            Head revision (default ``"FETCH_HEAD"``).

        Returns
        -------
        list of (str, str)
            Each element is ``(status, file_path)``.  *status* is a
            single-letter code such as ``"M"`` (modified), ``"A"`` (added),
            ``"D"`` (deleted).  For renames/copies the destination path is
            returned.
        """
        result = self._run_git([
            "diff", "--name-status", f"{base_ref}...{head_ref}",
        ])
        files: List[Tuple[str, str]] = []
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            status = parts[0]
            # For renames (R) and copies (C) the last path is the
            # destination; use that as the canonical file path.
            file_path = parts[-1]
            files.append((status, file_path))
        return files

    def get_commit_message(self, ref: str = "FETCH_HEAD") -> str:
        """Return the full commit message of a given revision.

        Parameters
        ----------
        ref : str
            Revision (default ``"FETCH_HEAD"``).

        Returns
        -------
        str
            Commit message body (``%B`` format).
        """
        result = self._run_git([
            "log", "-1", "--pretty=format:%B", ref,
        ])
        return result.stdout

    def deepen(self, depth_step: int = 100) -> None:
        """Gradually deepen the shallow clone by *depth_step* commits.

        Parameters
        ----------
        depth_step : int
            Number of additional commits to fetch (default 100).
        """
        logger.info("deepening repository by %d commits", depth_step)
        self._run_git(
            ["fetch", "--deepen", str(depth_step)],
            timeout=120,
        )

    def gc(self) -> None:
        """Run periodic git housekeeping (``git gc --auto``)."""
        if not self.repo_path.exists():
            logger.debug("skipping gc — repo does not exist")
            return
        try:
            self._run_git(["rev-parse", "--git-dir"])
        except GitCommandError:
            logger.debug("skipping gc — not a git repository")
            return
        logger.info("running git gc --auto")
        self._run_git(["gc", "--auto"])

    def cleanup_review_ref(self, change_id: str, revision: str = "1") -> None:
        """Delete a local review ref created by :meth:`fetch_change`.

        Parameters
        ----------
        change_id : str
            Gerrit numeric change ID.
        revision : str
            Patch-set revision number (default ``"1"``).
        """
        local_ref = f"refs/review/{change_id}/{revision}"
        try:
            self._run_git(["update-ref", "-d", local_ref], check=False)
        except Exception:
            pass

    def create_worktree(self, sha: str, change_id: str, revision: str) -> str:
        """Create a detached worktree for review isolation.

        Parameters
        ----------
        sha : str
            Commit SHA to check out in the worktree.
        change_id : str
            Gerrit numeric change ID.
        revision : str
            Patch-set revision number.

        Returns
        -------
        str
            Absolute path to the new worktree.
        """
        wt_dir = self.repo_path.parent / "_review" / change_id / revision
        wt_dir.parent.mkdir(parents=True, exist_ok=True)
        # If an old worktree for this change+revision exists, remove it first
        if wt_dir.exists():
            try:
                self._run_git(
                    ["worktree", "remove", "--force", str(wt_dir)],
                    check=False, timeout=30,
                )
            except Exception:
                pass
        logger.info("creating worktree at %s (sha=%s)", wt_dir, sha[:8])
        self._run_git(
            ["worktree", "add", "--detach", str(wt_dir), sha],
            timeout=60,
        )
        return str(wt_dir)

    def remove_worktree(self, worktree_path: str) -> None:
        """Remove a review worktree and prune its administrative data."""
        try:
            self._run_git(
                ["worktree", "remove", "--force", worktree_path],
                check=False, timeout=30,
            )
        except Exception:
            pass

    def sync(self) -> None:
        """Update all remote tracking branches and prune deleted ones.

        Equivalent to ``git remote update --prune``.
        """
        logger.info("syncing remotes with prune")
        self._run_git(["remote", "update", "--prune"], timeout=120)

    def is_shallow(self) -> bool:
        """Check whether the repository is a shallow clone.

        Returns
        -------
        bool
            True if the repository was cloned with ``--depth``.
        """
        result = self._run_git(["rev-parse", "--is-shallow-repository"])
        return result.stdout.strip() == "true"

    def disk_usage_mb(self) -> float:
        """Estimate the on-disk size of the local repository.

        Parses ``git count-objects -v`` and sums both loose and packed
        object sizes.

        Returns
        -------
        float
            Estimated size in megabytes.
        """
        result = self._run_git(["count-objects", "-v"])
        size_kb = 0
        size_pack_kb = 0

        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("size:"):
                size_kb = int(line.split(":")[1].strip())
            elif line.startswith("size-pack:"):
                size_pack_kb = int(line.split(":")[1].strip())

        total_kb = size_kb + size_pack_kb
        return total_kb / 1024.0
