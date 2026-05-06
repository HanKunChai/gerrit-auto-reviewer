"""Rule engine that applies rules to a code diff.

The :class:`ReviewEngine` parses unified-diff text, extracts added lines, and
runs every loaded rule against those lines.  Rules use regex patterns for
line-by-line matching, and may optionally specify ``include`` / ``exclude``
file-glob patterns to limit which files they apply to.

Typical usage::

    from review_rules import ReviewEngine

    engine = ReviewEngine()
    engine.load_builtin_rules()
    engine.load_rules("/path/to/custom/rules")

    with open("changes.diff") as f:
        diff_text = f.read()

    issues = engine.run(diff_text)
    for issue in issues:
        print(issue)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import loader

# ---------------------------------------------------------------------------
# Regex patterns for parsing unified diffs
# ---------------------------------------------------------------------------

# ``+++ b/<path>`` -- identifies the file being added/modified
_RE_FILE_HEADER = re.compile(r"^\+\+\+\s+b/(.*)")

# ``@@ -old,count +new,count @@`` -- hunk header
_RE_HUNK_HEADER = re.compile(r"^@@ -(\d+),?\d* \+(\d+),?\d* @@")


# ---------------------------------------------------------------------------
# Issue model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    """A single issue found during the review of a code diff.

    Attributes:
        file:     Source file path (relative to repository root).
        line:     1-based line number in the new file.
        column:   1-based column where the pattern was matched.
        severity: One of ``error``, ``warning``, ``info``.
        message:  Human-readable description of the issue.
        rule_id:  Unique rule identifier (e.g. ``C-STYLE-001``).
        category: Rule category (e.g. ``coding_style``).
    """

    file: str
    line: int
    column: int
    severity: str
    message: str
    rule_id: str
    category: str

    def __repr__(self) -> str:
        return (
            f"[{self.severity.upper()}] {self.file}:{self.line}:{self.column} "
            f"({self.rule_id}) {self.message}"
        )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class ReviewEngine:
    """Rule engine that applies rules to a code diff.

    Usage::

        engine = ReviewEngine()
        engine.load_builtin_rules()
        engine.load_rules("path/to/custom/rules")
        issues = engine.run(diff_text)
    """

    def __init__(self) -> None:
        self.rules: list[dict] = []

    # ------------------------------------------------------------------
    # Rule loading
    # ------------------------------------------------------------------

    def load_rules(self, rule_dir: str) -> None:
        """Load all rules from *rule_dir*, appending to the current ruleset.

        Args:
            rule_dir: Path to a directory containing ``.yaml`` / ``.yml``
                      rule files.
        """
        self.rules.extend(loader.scan_directory(rule_dir))

    def load_builtin_rules(self) -> None:
        """Load built-in rules shipped with the package.

        These live in the ``builtin/`` subdirectory next to this module.
        """
        builtin_dir = Path(__file__).parent / "builtin"
        if builtin_dir.is_dir():
            self.rules.extend(loader.scan_directory(str(builtin_dir)))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self, diff_text: str, file_path: Optional[str] = None
    ) -> list[Issue]:
        """Run all loaded rules against a unified-diff and return issues.

        Args:
            diff_text: Unified-diff content (may span multiple files).
            file_path: Optional filter -- only issues for this file are
                       returned.  When the diff text is not a valid unified
                       diff and *file_path* is given, the text is treated as
                       raw source code.

        Returns:
            Issues found, sorted by file / line / column.
        """
        if not diff_text or not diff_text.strip():
            return []

        file_sections = self._parse_diff_lines(diff_text)

        # Fallback: if the text does not look like a unified diff, treat it
        # as raw source code.
        if not file_sections:
            has_hunk_header = any(
                line.startswith("@@") for line in diff_text.splitlines()
            )
            if not has_hunk_header:
                effective_path = file_path or ""
                lines = diff_text.splitlines()
                file_sections[effective_path] = [
                    (idx + 1, line) for idx, line in enumerate(lines)
                ]

        issues: list[Issue] = []
        for section_file, added_lines in file_sections.items():
            actual_file = section_file or file_path or ""
            if file_path and actual_file != file_path:
                continue

            applicable = self._filter_rules_for_file(actual_file)
            for line_no, content in added_lines:
                issues.extend(
                    self._apply_rules_to_line(
                        actual_file, line_no, content, applicable
                    )
                )

        issues.sort(key=lambda x: (x.file, x.line, x.column))
        return issues

    def run_on_files(self, file_diffs: dict[str, str]) -> list[Issue]:
        """Run rules on a set of file-to-diff mappings.

        Args:
            file_diffs: Mapping of ``{file_path: diff_text_or_source_code}``.
                        Each value is passed to :meth:`run` along with its
                        key as the *file_path* argument.

        Returns:
            Combined list of issues for all files.
        """
        all_issues: list[Issue] = []
        for fpath, dtext in file_diffs.items():
            all_issues.extend(self.run(dtext, file_path=fpath))
        return all_issues

    # ------------------------------------------------------------------
    # Diff parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_diff_lines(
        diff_text: str,
    ) -> dict[str, list[tuple[int, str]]]:
        """Parse a unified diff into per-file added lines.

        Returns a dictionary mapping ``file_path -> [(line_number, content),
        ...]`` where each tuple describes one added line in the new file.
        """
        result: dict[str, list[tuple[int, str]]] = {}
        current_file: Optional[str] = None
        current_line: int = 0
        in_hunk: bool = False

        for line in diff_text.splitlines():
            # ---- file-level headers ----

            if line.startswith("diff --git "):
                current_file = None
                in_hunk = False
                continue

            if line.startswith("+++ b/"):
                # ``+++ b/<path>`` identifies the new-file path
                current_file = line[6:]
                in_hunk = False
                continue

            if line.startswith("--- "):
                continue  # old-file path, not needed

            if line.startswith("Binary files "):
                continue

            # ---- hunk header ----

            m = _RE_HUNK_HEADER.match(line)
            if m:
                current_line = int(m.group(2))  # new-file start line
                in_hunk = True
                continue

            if not in_hunk or current_file is None:
                continue

            # ---- lines within a hunk ----

            # Context line (single space prefix) -- present in the new file
            if line.startswith(" "):
                current_line += 1
            # Added line (``+`` prefix)
            elif line.startswith("+") and not line.startswith("+++"):
                content = line[1:]
                result.setdefault(current_file, []).append(
                    (current_line, content)
                )
                current_line += 1
            # Removed lines (``-`` prefix) are silently skipped

        return result

    # ------------------------------------------------------------------
    # Rule matching
    # ------------------------------------------------------------------

    def _filter_rules_for_file(self, file_path: str) -> list[dict]:
        """Return only the rules whose include/exclude selectors match.

        A rule with no ``include`` / ``exclude`` fields applies to every file.
        """
        if not self.rules:
            return []

        applicable: list[dict] = []
        for rule in self.rules:
            inc = rule.get("include")
            exc = rule.get("exclude")

            # ``include`` -- at least one pattern must match
            if inc:
                inc_list = inc if isinstance(inc, list) else [inc]
                if not any(re.search(p, file_path) for p in inc_list):
                    continue

            # ``exclude`` -- no pattern may match
            if exc:
                exc_list = exc if isinstance(exc, list) else [exc]
                if any(re.search(p, file_path) for p in exc_list):
                    continue

            applicable.append(rule)

        return applicable

    @staticmethod
    def _apply_rules_to_line(
        file_path: str,
        line_no: int,
        line_content: str,
        rules: list[dict],
    ) -> list[Issue]:
        """Apply every rule to a single source line.

        Currently only ``regex`` pattern types are supported.  Invalid
        patterns are silently skipped.
        """
        issues: list[Issue] = []

        for rule in rules:
            pattern = rule["pattern"]
            if pattern.get("type") != "regex":
                continue

            try:
                compiled = re.compile(pattern["value"])
            except re.error:
                continue

            for match in compiled.finditer(line_content):
                col = match.start() + 1  # 1-indexed column
                issues.append(
                    Issue(
                        file=file_path,
                        line=line_no,
                        column=col,
                        severity=rule["severity"],
                        message=rule["message"],
                        rule_id=rule["id"],
                        category=rule["category"],
                    )
                )

        return issues
