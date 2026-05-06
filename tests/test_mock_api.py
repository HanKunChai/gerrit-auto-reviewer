"""Tests for the mock Gerrit REST API."""

import json
import pytest
from mcp_gerrit_server import mock_api


@pytest.fixture
def client():
    return mock_api.app.test_client()


GERRIT_MAGIC = ")]}'\n"


def strip_prefix(raw: str) -> str:
    if raw.startswith(GERRIT_MAGIC):
        return raw[len(GERRIT_MAGIC):]
    return raw


class TestListChanges:
    def test_returns_list(self, client):
        resp = client.get("/changes/")
        assert resp.status_code == 200
        data = json.loads(strip_prefix(resp.get_data(as_text=True)))
        assert isinstance(data, list)
        assert len(data) == 2

    def test_change_has_required_fields(self, client):
        resp = client.get("/changes/")
        data = json.loads(strip_prefix(resp.get_data(as_text=True)))
        change = data[0]
        assert "id" in change
        assert "change_id" in change
        assert "subject" in change
        assert "status" in change
        assert "project" in change

    def test_magic_prefix(self, client):
        raw = client.get("/changes/").get_data(as_text=True)
        assert raw.startswith(GERRIT_MAGIC)

    def test_cors_headers(self, client):
        resp = client.options("/changes/")
        assert resp.status_code == 204
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"


class TestPostReview:
    def test_post_review_success(self, client):
        resp = client.post(
            "/changes/Ideadbeef1234/revisions/current/review",
            json={"message": "LGTM", "labels": {"Code-Review": 2}, "comments": {}},
        )
        assert resp.status_code == 200
        data = json.loads(strip_prefix(resp.get_data(as_text=True)))
        assert "message" in data
        assert data["labels"]["Code-Review"] == 2

    def test_post_review_unknown_change(self, client):
        resp = client.post(
            "/changes/unknown/revisions/current/review",
            json={"message": "test", "labels": {"Code-Review": 1}, "comments": {}},
        )
        assert resp.status_code == 404

    def test_cors_post_review(self, client):
        resp = client.options(
            "/changes/Ideadbeef1234/revisions/current/review"
        )
        assert resp.status_code == 204


class TestGetFiles:
    def test_list_files(self, client):
        resp = client.get("/changes/Ideadbeef1234/revisions/current/files")
        assert resp.status_code == 200
        data = json.loads(strip_prefix(resp.get_data(as_text=True)))
        assert isinstance(data, dict)
        assert len(data) > 0

    def test_files_unknown_change(self, client):
        resp = client.get("/changes/unknown/revisions/current/files")
        assert resp.status_code == 404
