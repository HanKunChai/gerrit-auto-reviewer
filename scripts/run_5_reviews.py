"""Run 5 test reviews on local test repo branches and output results."""

import asyncio
import json
import os
import sys
from datetime import datetime

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from mcp_gerrit_server.server import GerritReviewServer
from mcp_gerrit_server.local_repo import LocalRepo

BRANCHES = [
    ("fix-strcpy", "Branch 1/5: Buffer overflow via strcpy"),
    ("fix-style",  "Branch 2/5: Style violations (braces, init, multi-var)"),
    ("fix-injection", "Branch 3/5: Command injection (sprintf + system)"),
    ("fix-null",   "Branch 4/5: Null pointer deref + memory leak"),
    ("fix-gets",   "Branch 5/5: Unsafe gets() usage"),
]

REPO_PATH = os.path.join(PROJECT_DIR, "test-data", "test-repo")
REVIEWS_DIR = os.path.join(PROJECT_DIR, "reviews")
os.makedirs(REVIEWS_DIR, exist_ok=True)

TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_PATH = os.path.join(REVIEWS_DIR, f"session_{TIMESTAMP}.log")
log_lines = []


def log(msg=""):
    print(msg)
    log_lines.append(msg)


async def run_review(branch, description):
    log()
    log(f"{'='*50}")
    log(f"  {description}")
    log(f"{'='*50}")
    log()

    # Use local repo directly
    repo = LocalRepo(repo_path=REPO_PATH)
    diff = repo.get_diff("base", branch)
    files = repo.list_changed_files("base", branch)

    log(f"  Diff: {len(diff.splitlines())} lines, {len(files)} files")
    for s, f in files:
        log(f"    [{s}] {f}")
    log()

    # Run rules engine
    from review_rules import ReviewEngine
    engine = ReviewEngine()
    engine.load_builtin_rules()
    issues = engine.run(diff)

    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]
    infos = [i for i in issues if i.severity == "info"]

    log(f"  Issues: {len(issues)} total ({len(errors)} errors, {len(warnings)} warnings, {len(infos)} info)")
    log()

    for issue in issues:
        log(f"    [{issue.severity.upper():7}] {issue.rule_id:15} {issue.file}:{issue.line:<4} {issue.message[:90]}")
    log()

    # Get review prompt for AI review
    svr = GerritReviewServer(mock=True)
    prompt_result = await svr._handle_c_review_prompt({})
    prompt_text = prompt_result.get("prompt", "")[:200]

    log(f"  Claude Review Prompt: {len(prompt_text)} chars loaded")
    log(f"  AI review ready for {len(issues)} issues")
    log()

    # Save review result
    result = {
        "branch": branch,
        "description": description,
        "timestamp": datetime.utcnow().isoformat(),
        "diff_lines": len(diff.splitlines()),
        "files": [{"status": s, "path": f} for s, f in files],
        "issues": [
            {"file": i.file, "line": i.line, "severity": i.severity,
             "rule_id": i.rule_id, "message": i.message, "category": i.category}
            for i in issues
        ],
        "summary": {
            "total": len(issues),
            "errors": len(errors),
            "warnings": len(warnings),
            "infos": len(infos),
        },
    }

    output_path = os.path.join(REVIEWS_DIR, f"{branch}_{TIMESTAMP}.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    return result


async def main():
    log(f"{'='*55}")
    log(f"  Gerrit Auto-Review Session: 5 Test Commits")
    log(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"{'='*55}")
    log()

    total_issues = 0
    total_errors = 0
    all_results = []

    for branch, desc in BRANCHES:
        result = await run_review(branch, desc)
        all_results.append(result)
        total_issues += result["summary"]["total"]
        total_errors += result["summary"]["errors"]

    log()
    log(f"{'='*55}")
    log(f"  SESSION SUMMARY")
    log(f"{'='*55}")
    log(f"  Total commits reviewed: {len(all_results)}")
    log(f"  Total issues found:     {total_issues}")
    log(f"  Total errors:           {total_errors}")

    for r in all_results:
        s = r["summary"]
        log(f"    {r['branch']:15} {s['total']:3} issues ({s['errors']} errors, {s['warnings']} warnings)")

    log()
    log(f"  Review files saved to: {REVIEWS_DIR}")
    log(f"  Session log: {LOG_PATH}")
    log(f"{'='*55}")

    # Write log file
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines))


if __name__ == "__main__":
    asyncio.run(main())
