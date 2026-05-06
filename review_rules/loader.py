"""Rule loader that reads YAML rule files from a directory.

Each YAML rule file must contain a top-level ``rules`` key whose value is a
list of rule dictionaries.  Every rule must have the following fields:

    id          -- unique rule identifier (e.g. ``C-STYLE-001``)
    severity    -- one of ``error``, ``warning``, ``info``
    category    -- category name (e.g. ``coding_style``)
    description -- human-readable description of what the rule checks
    pattern     -- dict with ``type`` (currently only ``regex``) and ``value``
    message     -- message attached to issues raised by this rule

Optional fields:

    include     -- regex (or list of regexes); a file must match at least one
                   for the rule to be applied
    exclude     -- regex (or list of regexes); files matching any are skipped
"""

from __future__ import annotations

import warnings
from pathlib import Path

import yaml

VALID_SEVERITIES: set[str] = {"error", "warning", "info"}
VALID_PATTERN_TYPES: set[str] = {"regex"}

REQUIRED_RULE_FIELDS: list[str] = [
    "id",
    "severity",
    "category",
    "description",
    "pattern",
    "message",
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_rule_file(file_path: str) -> list[dict]:
    """Load and validate rules from a single YAML file.

    Args:
        file_path: Absolute or relative path to the ``.yaml`` / ``.yml`` file.

    Returns:
        List of validated rule dictionaries.

    Raises:
        FileNotFoundError: If *file_path* does not exist.
        ValueError:       If the file is not valid YAML or a rule fails
                          validation.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Rule file not found: {file_path}")
    if path.suffix not in (".yaml", ".yml"):
        raise ValueError(f"Not a YAML file: {file_path}")

    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    if not data or "rules" not in data:
        return []

    rules = data["rules"]
    if not isinstance(rules, list):
        raise ValueError(
            f"Invalid rule file format: 'rules' must be a list in {file_path}"
        )

    validated: list[dict] = []
    for rule in rules:
        _validate_rule(rule, file_path)
        validated.append(rule)

    return validated


def scan_directory(dir_path: str) -> list[dict]:
    """Scan a directory for ``.yaml`` / ``.yml`` rule files and load all rules.

    Files are processed in sorted order for deterministic results.  Files that
    fail to load are skipped with a warning instead of aborting the scan.

    Args:
        dir_path: Directory path to scan.

    Returns:
        Combined list of every rule dict found in every YAML file.

    Raises:
        NotADirectoryError: If *dir_path* is not a directory.
    """
    path = Path(dir_path)
    if not path.is_dir():
        raise NotADirectoryError(f"Not a directory: {dir_path}")

    all_rules: list[dict] = []
    # glob returns files in arbitrary order -- sort for determinism
    yaml_files = sorted(path.glob("*.yaml")) + sorted(path.glob("*.yml"))

    for yaml_file in yaml_files:
        try:
            rules = load_rule_file(str(yaml_file))
            all_rules.extend(rules)
        except (ValueError, yaml.YAMLError) as exc:
            warnings.warn(f"Skipping {yaml_file.name}: {exc}")

    return all_rules


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_rule(rule: dict, file_path: str) -> None:
    """Validate a single rule dictionary; raise ValueError on failure."""
    rule_id = rule.get("id", "<unknown>")

    for field in REQUIRED_RULE_FIELDS:
        if field not in rule:
            raise ValueError(
                f"Rule '{rule_id}' in {file_path} missing required field: '{field}'"
            )

    if rule["severity"] not in VALID_SEVERITIES:
        raise ValueError(
            f"Rule '{rule['id']}' in {file_path} has invalid severity: "
            f"'{rule['severity']}'. Must be one of {sorted(VALID_SEVERITIES)}"
        )

    pattern = rule.get("pattern")
    if not isinstance(pattern, dict):
        raise ValueError(
            f"Rule '{rule['id']}' in {file_path} has invalid pattern: "
            f"must be a dict with 'type' and 'value'"
        )

    if "type" not in pattern or "value" not in pattern:
        raise ValueError(
            f"Rule '{rule['id']}' in {file_path} pattern missing 'type' or 'value'"
        )

    if pattern["type"] not in VALID_PATTERN_TYPES:
        raise ValueError(
            f"Rule '{rule['id']}' in {file_path} has unsupported pattern type: "
            f"'{pattern['type']}'. Supported types: {sorted(VALID_PATTERN_TYPES)}"
        )
