"""Tests for the review cache module."""

import os
import tempfile
import time
from pathlib import Path

import pytest

from mcp_gerrit_server.cache import ReviewCache
from mcp_gerrit_server.config import CacheConfig


@pytest.fixture
def cache_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


def make_config(tmpdir, ttl=24):
    return CacheConfig({
        "enabled": True,
        "dir": os.path.join(tmpdir, "cache"),
        "max_size_mb": 100,
        "ttl_hours": ttl,
    })


class TestReviewCache:
    def test_put_and_get(self, cache_dir):
        cache = ReviewCache(make_config(cache_dir))
        cache.put("12345", "1", {"score": 1, "message": "LGTM"})
        result = cache.get("12345", "1")
        assert result is not None
        assert result["score"] == 1
        assert result["message"] == "LGTM"

    def test_get_missing(self, cache_dir):
        cache = ReviewCache(make_config(cache_dir))
        result = cache.get("does_not_exist", "1")
        assert result is None

    def test_invalidate(self, cache_dir):
        cache = ReviewCache(make_config(cache_dir))
        cache.put("12345", "1", {"score": 1})
        cache.invalidate("12345", "1")
        assert cache.get("12345", "1") is None

    def test_invalidate_all_revisions(self, cache_dir):
        cache = ReviewCache(make_config(cache_dir))
        cache.put("12345", "1", {"score": 1})
        cache.put("12345", "2", {"score": 2})
        cache.invalidate("12345")
        assert cache.get("12345", "1") is None
        assert cache.get("12345", "2") is None

    def test_ttl_expiry(self, cache_dir):
        cache = ReviewCache(make_config(cache_dir, ttl=0))
        cache.put("12345", "1", {"score": 1})
        time.sleep(0.1)
        result = cache.get("12345", "1")
        assert result is None

    def test_cache_disabled(self, cache_dir):
        cfg = CacheConfig({
            "enabled": False,
            "dir": os.path.join(cache_dir, "cache"),
            "max_size_mb": 100,
            "ttl_hours": 24,
        })
        cache = ReviewCache(cfg)
        cache.put("12345", "1", {"score": 1})
        assert cache.get("12345", "1") is None

    def test_cleanup(self, cache_dir):
        cache = ReviewCache(make_config(cache_dir, ttl=0))
        cache.put("12345", "1", {"score": 1})
        time.sleep(0.1)
        removed = cache.cleanup()
        assert removed == 1

    def test_persistence(self, cache_dir):
        cache = ReviewCache(make_config(cache_dir))
        cache.put("12345", "1", {"score": 1})
        cache2 = ReviewCache(make_config(cache_dir))
        result = cache2.get("12345", "1")
        assert result is not None
        assert result["score"] == 1
