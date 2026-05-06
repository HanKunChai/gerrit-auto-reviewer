"""Tests for the Gerrit REST API client."""

from unittest.mock import Mock, PropertyMock, patch

import pytest
import requests

from mcp_gerrit_server.gerrit_client import (
    GerritClient,
    GerritApiError,
    GerritAuthError,
    GerritConnectionError,
    parse_gerrit_response,
)


class TestParseGerritResponse:
    def test_with_magic_prefix(self):
        raw = ")]}'\n" + '{"key": "value"}'
        result = parse_gerrit_response(raw)
        assert result == {"key": "value"}

    def test_without_magic_prefix(self):
        raw = '{"key": "value"}'
        result = parse_gerrit_response(raw)
        assert result == {"key": "value"}

    def test_list_response(self):
        raw = ")]}'\n" + '[{"id": 1}, {"id": 2}]'
        result = parse_gerrit_response(raw)
        assert len(result) == 2

    def test_empty_response(self):
        with pytest.raises(Exception):
            parse_gerrit_response(")]}'\n")


class TestGerritClient:
    def test_init_with_auth(self):
        client = GerritClient("https://gerrit.example.com", auth=("user", "pass"))
        assert client._base_url == "https://gerrit.example.com/a"
        assert client._auth == ("user", "pass")

    def test_init_without_auth(self):
        client = GerritClient("https://gerrit.example.com")
        assert client._auth is None

    def test_init_trailing_slash(self):
        client = GerritClient("https://gerrit.example.com/", auth=("u", "p"))
        assert client._base_url == "https://gerrit.example.com/a"

    def test_init_trailing_slash_a(self):
        client = GerritClient("https://gerrit.example.com/a/", auth=("u", "p"))
        assert client._base_url == "https://gerrit.example.com/a"

    @patch.object(requests.Session, "request")
    def test_list_changes(self, mock_request):
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.text = ")]}'\n" + '[{"id": "test~main~I123", "subject": "test"}]'
        mock_request.return_value = mock_resp

        client = GerritClient("https://gerrit.example.com")
        changes = client.list_changes(status="open", limit=5)
        assert len(changes) == 1
        assert changes[0]["subject"] == "test"

    @patch.object(requests.Session, "request")
    def test_list_changes_auth_error(self, mock_request):
        mock_resp = Mock()
        mock_resp.status_code = 401
        mock_request.return_value = mock_resp

        client = GerritClient("https://gerrit.example.com", auth=("u", "p"))
        with pytest.raises(GerritAuthError):
            client.list_changes()

    @patch.object(requests.Session, "request")
    def test_fetch_patch(self, mock_request):
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.text = ")]}'\n" + "diff --git a/test.c b/test.c\nindex abc..def\n--- a/test.c\n+++ b/test.c\n@@ -1 +1,2 @@\n old\n+new\n"
        mock_request.return_value = mock_resp

        client = GerritClient("https://gerrit.example.com")
        patch = client.fetch_patch("I123", "current")
        assert "diff --git" in patch
        assert "+new" in patch

    @patch.object(requests.Session, "request")
    def test_post_review(self, mock_request):
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.text = ")]}'\n" + '{"labels": {"Code-Review": 1}}'
        mock_request.return_value = mock_resp

        client = GerritClient("https://gerrit.example.com")
        result = client.post_review(
            "I123",
            "current",
            message="Looks good",
            score=1,
            comments={"file.c": [{"line": 10, "message": "fix this"}]},
        )
        assert result["labels"]["Code-Review"] == 1

    @patch.object(requests.Session, "request")
    def test_connection_error(self, mock_request):
        mock_request.side_effect = requests.exceptions.ConnectionError()

        client = GerritClient("https://gerrit.example.com")
        with pytest.raises(GerritConnectionError):
            client.list_changes()

    @patch.object(requests.Session, "request")
    def test_fetch_files(self, mock_request):
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.text = ")]}'\n" + '{"file.c": {"status": "M"}}'
        mock_request.return_value = mock_resp

        client = GerritClient("https://gerrit.example.com")
        files = client.fetch_files("I123")
        assert files == {"file.c": {"status": "M"}}
