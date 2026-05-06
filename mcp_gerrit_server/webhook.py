"""Webhook listener for Gerrit patchset-created events.

Listens for Gerrit event system notifications and triggers
automatic code reviews on new patch sets.

Supports:
- Gerrit Event System webhook POST /webhook/gerrit
- Backup polling mode via Gerrit REST API
- Configurable concurrency limiting
"""

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from queue import Queue
from typing import Any, Optional, Set

import flask
import requests

from mcp_gerrit_server.config import WebhookConfig

logger = logging.getLogger(__name__)


@dataclass
class ReviewEvent:
    """Represents a Gerrit event that triggers a review."""
    change_id: str
    revision: str
    project: str
    event_type: str
    raw_data: dict = field(default_factory=dict)


class WebhookServer:
    """Webhook receiver for Gerrit events."""

    def __init__(self, config: WebhookConfig, review_callback=None):
        self.config = config
        self.review_callback = review_callback
        self.app = flask.Flask(__name__)
        self._queue: Queue = Queue()
        self._active_reviews = 0
        self._lock = threading.Lock()
        self._worker_thread: Optional[threading.Thread] = None
        self._running = False

        @self.app.route("/webhook/gerrit", methods=["POST"])
        def handle_webhook():
            return self._handle_event(flask.request)

        @self.app.route("/webhook/health", methods=["GET"])
        def health():
            return {"status": "ok", "active_reviews": self._active_reviews}

    def _handle_event(self, request) -> flask.Response:
        """Process incoming webhook event."""
        try:
            data = request.get_json(force=True)
        except Exception:
            return flask.jsonify({"error": "invalid JSON"}), 400

        event_type = data.get("type", "")
        if event_type != "patchset-created":
            return flask.jsonify({"status": "ignored", "reason": f"unhandled event: {event_type}"})

        change = data.get("change", {})
        patch_set = data.get("patchSet", {})

        event = ReviewEvent(
            change_id=change.get("id", ""),
            revision=str(patch_set.get("number", 1)),
            project=change.get("project", ""),
            event_type=event_type,
            raw_data=data,
        )

        logger.info(
            "Queueing review: change=%s revision=%s project=%s",
            event.change_id, event.revision, event.project,
        )
        self._queue.put(event)
        return flask.jsonify({"status": "queued"})

    def _worker_loop(self):
        """Background worker: process queued review events."""
        while self._running:
            try:
                event = self._queue.get(timeout=1)
            except Exception:
                continue

            self._lock.acquire()
            if self._active_reviews >= self.config.max_concurrent_reviews:
                self._lock.release()
                self._queue.put(event)
                time.sleep(5)
                continue
            self._active_reviews += 1
            self._lock.release()

            try:
                if self.review_callback:
                    logger.info(
                        "Starting review: change=%s rev=%s",
                        event.change_id, event.revision,
                    )
                    self.review_callback(event)
            except Exception as e:
                logger.error("Review failed: %s", e)
            finally:
                with self._lock:
                    self._active_reviews -= 1

    def start(self):
        """Start the webhook server in background threads."""
        self._running = True
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()

        host = self.config.host
        port = self.config.port
        logger.info("Webhook server starting on %s:%s", host, port)
        threading.Thread(
            target=lambda: self.app.run(host=host, port=port, debug=False, use_reloader=False),
            daemon=True,
        ).start()

    def stop(self):
        """Stop the webhook server."""
        self._running = False
        logger.info("Webhook server stopped")

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()


class ReviewPoller:
    """Backup polling mode: periodically checks Gerrit for open changes."""

    def __init__(self, gerrit_client, poll_interval=120, review_callback=None):
        self.client = gerrit_client
        self.interval = poll_interval
        self.review_callback = review_callback
        self._seen: Set[str] = set()
        self._running = False

    def start(self):
        """Start polling in background."""
        self._running = True
        threading.Thread(target=self._poll_loop, daemon=True).start()
        logger.info("Review poller started (interval=%ss)", self.interval)

    def _poll_loop(self):
        while self._running:
            try:
                changes = self.client.list_changes(status="open", limit=25)
                for change in changes:
                    change_id = change.get("id", "")
                    if change_id and change_id not in self._seen:
                        self._seen.add(change_id)
                        revision = change.get("revisions", {})
                        rev_num = "current"
                        if revision:
                            rev_num = list(revision.keys())[0]
                        logger.info("Poll found new change: %s (rev=%s)", change_id, rev_num)
                        if self.review_callback:
                            event = ReviewEvent(
                                change_id=change_id,
                                revision=rev_num,
                                project=change.get("project", ""),
                                event_type="poll-discovered",
                                raw_data=change,
                            )
                            try:
                                self.review_callback(event)
                            except Exception as e:
                                logger.error("Poll review failed: %s", e)
            except Exception as e:
                logger.error("Poll error: %s", e)

            time.sleep(self.interval)

    def stop(self):
        self._running = False
