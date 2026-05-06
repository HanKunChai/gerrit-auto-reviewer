"""Review result caching layer.

Caches review results by change_id/revision to avoid re-reviewing
unchanged patch sets. Also provides disk usage monitoring.
"""

import json
import os
import time
from pathlib import Path
from threading import Lock
from typing import Dict, Optional

from mcp_gerrit_server.config import CacheConfig


class ReviewCache:
    """Thread-safe cache for review results."""

    def __init__(self, config: CacheConfig):
        self.config = config
        self._lock = Lock()
        self._cache: Dict[str, dict] = {}
        self._dirty = False

        if config.enabled:
            self._cache_dir = Path(config.dir)
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            self._load_index()

    def _cache_path(self) -> Path:
        return self._cache_dir / "index.json"

    def _load_index(self):
        path = self._cache_path()
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                now = time.time()
                ttl = self.config.ttl_hours * 3600
                self._cache = {
                    k: v for k, v in data.items()
                    if now - v.get("cached_at", 0) < ttl
                }
            except (json.JSONDecodeError, OSError):
                self._cache = {}

    def _save_index(self):
        if not self.config.enabled:
            return
        try:
            with open(self._cache_path(), "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2)
        except OSError:
            pass

    def _key(self, change_id: str, revision: str) -> str:
        return f"{change_id}:{revision}"

    def get(self, change_id: str, revision: str = "current") -> Optional[dict]:
        """Get cached review result, or None if not cached/expired."""
        if not self.config.enabled:
            return None
        with self._lock:
            entry = self._cache.get(self._key(change_id, revision))
            if entry is None:
                return None
            now = time.time()
            ttl = self.config.ttl_hours * 3600
            if now - entry.get("cached_at", 0) >= ttl:
                del self._cache[self._key(change_id, revision)]
                self._save_index()
                return None
            return entry.get("result")

    def put(self, change_id: str, revision: str, result: dict) -> None:
        """Cache a review result."""
        if not self.config.enabled:
            return
        with self._lock:
            entry = {
                "cached_at": time.time(),
                "change_id": change_id,
                "revision": revision,
                "result": result,
            }
            entry["result"]["cached_at"] = entry["cached_at"]
            self._cache[self._key(change_id, revision)] = entry
            self._save_index()

    def invalidate(self, change_id: str, revision: Optional[str] = None) -> None:
        """Remove cached entry for a change/revision."""
        with self._lock:
            if revision:
                self._cache.pop(self._key(change_id, revision), None)
            else:
                prefix = f"{change_id}:"
                self._cache = {
                    k: v for k, v in self._cache.items() if not k.startswith(prefix)
                }
            self._save_index()

    def disk_usage_mb(self) -> float:
        """Estimate cache disk usage in MB."""
        total = 0.0
        if not self._cache_dir.exists():
            return 0.0
        for f in self._cache_dir.iterdir():
            if f.is_file():
                total += f.stat().st_size
        return total / (1024 * 1024)

    def cleanup(self) -> int:
        """Remove expired entries. Returns count of removed entries."""
        if not self.config.enabled:
            return 0
        removed = 0
        now = time.time()
        ttl = self.config.ttl_hours * 3600
        with self._lock:
            expired = [
                k for k, v in self._cache.items()
                if now - v.get("cached_at", 0) >= ttl
            ]
            for k in expired:
                del self._cache[k]
                removed += 1
            if removed:
                self._save_index()
        return removed

    @property
    def size(self) -> int:
        """Number of cached entries."""
        with self._lock:
            return len(self._cache)
