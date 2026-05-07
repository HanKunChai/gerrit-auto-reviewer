"""CLI entry point for auto-review.

Replaces the shell script's Python -c invocations to eliminate
command injection vectors. Usage:

    python scripts/auto_review.py --mock --change-id 12345 --revision 1
"""

import argparse
import asyncio
import json
import os
import sys

# Ensure project root is on path
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from mcp_gerrit_server.server import GerritReviewServer


def main():
    parser = argparse.ArgumentParser(description="Gerrit auto-review CLI")
    parser.add_argument("--mock", action="store_true", help="Mock mode")
    parser.add_argument("--change-id", required=True, help="Gerrit change ID")
    parser.add_argument("--revision", default="1", help="Patch set number")
    parser.add_argument("--base-branch", default="master", help="Base branch")
    parser.add_argument("--output", help="Output file path")
    args = parser.parse_args()

    output_dir = os.path.join(PROJECT_DIR, "reviews")
    os.makedirs(output_dir, exist_ok=True)

    timestamp = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = args.output or os.path.join(
        output_dir,
        f"review_{args.change_id}_r{args.revision}_{timestamp}.json",
    )

    print(f"=== Gerrit Auto Review ===")
    print(f"Change ID:   {args.change_id}")
    print(f"Revision:    {args.revision}")
    print(f"Base Branch: {args.base_branch}")
    print(f"Mode:        {'mock' if args.mock else 'production'}")
    print(f"Output:      {output_file}")
    print()

    # Run review
    server = GerritReviewServer(mock=args.mock)
    result = asyncio.run(_run_review(server, args))

    # Save output
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(f"Review saved to {output_file}")
    return result


async def _run_review(server, args):
    result = {"change_id": args.change_id, "revision": args.revision}

    # Step 1: Fetch diff
    print("[1/4] Fetching diff...")
    try:
        diff_result = await server._handle_fetch_diff({
            "change_id": args.change_id,
            "revision": args.revision,
            "base_branch": args.base_branch,
        })
        result["diff"] = diff_result
        diff_text = diff_result.get("diff", "")
        files = diff_result.get("files", [])
        print(f"  Diff lines: {len(diff_text.splitlines())}")
        print(f"  Files: {len(files)}")
    except Exception as e:
        result["error"] = f"Fetch diff failed: {e}"
        print(f"  ERROR: {e}")
        return result

    # Step 2: Run rules
    print("[2/4] Running rules engine...")
    try:
        rules_result = await server._handle_run_rules({
            "diff_text": diff_text,
        })
        result["rules"] = rules_result
        print(f"  Issues: {rules_result.get('total_issues', 0)}")
    except Exception as e:
        result["rules_error"] = str(e)
        print(f"  Rules ERROR: {e}")

    # Step 3: Get review prompt
    print("[3/4] Loading review prompt...")
    try:
        prompt = await server._handle_c_review_prompt({})
        result["review_prompt"] = prompt
    except Exception as e:
        result["prompt_error"] = str(e)

    print("[4/4] Review data ready")
    return result


if __name__ == "__main__":
    main()
