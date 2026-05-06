"""Event handler: routes webhook/poll events to the review pipeline."""

import json
import logging
import os
from datetime import datetime

from mcp_gerrit_server.webhook import ReviewEvent

logger = logging.getLogger(__name__)


class ReviewEventHandler:
    """Connects webhook events to the review pipeline.

    When a patchset-created event arrives, this handler:
    1. Logs the event
    2. Triggers the full review pipeline
    3. Saves the review result
    """

    def __init__(self, server, reviews_dir: str = "./reviews"):
        self.server = server
        self.reviews_dir = reviews_dir
        os.makedirs(reviews_dir, exist_ok=True)

    def __call__(self, event: ReviewEvent):
        return self.handle(event)

    def handle(self, event: ReviewEvent):
        """Process a review event."""
        logger.info(
            "Processing event: type=%s change=%s rev=%s project=%s",
            event.event_type, event.change_id, event.revision, event.project,
        )

        result = {
            "event": {
                "type": event.event_type,
                "change_id": event.change_id,
                "revision": event.revision,
                "project": event.project,
                "timestamp": datetime.utcnow().isoformat(),
            },
            "steps": [],
        }

        # Step 1: Fetch diff
        try:
            import asyncio
            diff_result = asyncio.run(
                self.server._handle_fetch_diff({
                    "change_id": event.change_id,
                    "revision": event.revision,
                })
            )
            result["steps"].append({
                "step": "fetch_diff",
                "status": "ok",
                "files_count": len(diff_result.get("files", [])),
            })
        except Exception as e:
            logger.error("Fetch diff failed: %s", e)
            result["steps"].append({"step": "fetch_diff", "status": "error", "error": str(e)})
            self._save_result(event, result)
            return result

        diff_text = diff_result.get("diff", "")
        if not diff_text:
            logger.warning("Empty diff for change %s", event.change_id)
            result["steps"].append({"step": "fetch_diff", "status": "empty_diff"})
            self._save_result(event, result)
            return result

        # Step 2: Run rules engine
        try:
            from review_rules.engine import ReviewEngine
            engine = ReviewEngine()
            engine.load_builtin_rules()
            issues = engine.run(diff_text)
            rules_result = [
                {
                    "file": i.file, "line": i.line, "severity": i.severity,
                    "message": i.message, "rule_id": i.rule_id, "category": i.category,
                }
                for i in issues
            ]
            result["steps"].append({
                "step": "run_rules",
                "status": "ok",
                "issues_count": len(rules_result),
            })
            result["rules_issues"] = rules_result
        except Exception as e:
            logger.error("Rules engine failed: %s", e)
            result["steps"].append({"step": "run_rules", "status": "error", "error": str(e)})

        self._save_result(event, result)
        logger.info("Event processed: %s", event.change_id)
        return result

    def _save_result(self, event: ReviewEvent, result: dict):
        """Save review result to disk."""
        filename = f"review_{event.change_id}_r{event.revision}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
        filepath = os.path.join(self.reviews_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        logger.info("Review result saved: %s", filepath)
