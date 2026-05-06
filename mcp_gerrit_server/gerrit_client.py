"""
Client for the real Gerrit REST API.

Provides the ``GerritClient`` class whose interface mirrors the mock API in
``mock_api.py`` so that callers can switch between mock and real back-ends
with minimal code changes.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import requests


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class GerritAuthError(Exception):
    """Raised when authentication fails (HTTP 401)."""


class GerritConnectionError(Exception):
    """Raised when a connection cannot be established."""


class GerritApiError(Exception):
    """Raised when the Gerrit API returns a non-success status (other than 401)."""


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

GERRIT_MAGIC_PREFIX = ")]}'\n"


def parse_gerrit_response(response_text: str) -> Any:
    """Remove the Gerrit magic JSON prefix and parse the remainder as JSON.

    Parameters
    ----------
    response_text : str
        Raw response body from the Gerrit REST API.

    Returns
    -------
    Any
        The parsed JSON value (typically a ``dict`` or ``list``).
    """
    text = response_text
    if text.startswith(GERRIT_MAGIC_PREFIX):
        text = text[len(GERRIT_MAGIC_PREFIX) :]
    return json.loads(text)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GerritClient:
    """A lightweight HTTP client for the Gerrit REST API.

    Parameters
    ----------
    base_url : str
        The base URL of the Gerrit server (e.g. ``https://gerrit.example.com``).
        The trailing ``/a/`` authentication prefix is appended automatically when
        credentials are supplied.
    auth : tuple[str, str] or None
        Optional ``(username, password)`` tuple used for HTTP Basic
        authentication.  When provided the client uses the ``/a/`` URL prefix
        that Gerrit reserves for authenticated requests.
    timeout : float
        Request timeout in seconds (default 30).
    """

    def __init__(
        self,
        base_url: str,
        auth: Optional[Tuple[str, str]] = None,
        timeout: float = 30.0,
        use_a_prefix: bool = True,
    ):
        self._base_url = base_url.rstrip("/")
        self._auth = auth
        self._timeout = timeout
        self._use_a_prefix = use_a_prefix
        self._session = requests.Session()

        if auth is not None:
            self._session.auth = auth
            # Gerrit requires authenticated requests to use the /a/ prefix
            # (unless behind a reverse proxy that strips it)
            if use_a_prefix and not self._base_url.endswith("/a"):
                self._base_url += "/a"

    # -- Internal helpers -----------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Perform an HTTP request with error handling."""
        kwargs.setdefault("timeout", self._timeout)
        try:
            response = self._session.request(method, self._url(path), **kwargs)
        except requests.ConnectionError as exc:
            raise GerritConnectionError(
                f"Unable to connect to Gerrit at {self._base_url}"
            ) from exc
        except requests.Timeout as exc:
            raise GerritConnectionError(
                f"Request timed out after {self._timeout}s "
                f"for {method.upper()} {path}"
            ) from exc
        except requests.RequestException as exc:
            raise GerritConnectionError(
                f"Request failed for {method.upper()} {path}: {exc}"
            ) from exc

        if response.status_code == 401:
            raise GerritAuthError(
                "Authentication failed (HTTP 401) -- check your username "
                "and password/token."
            )

        if not response.ok:
            raise GerritApiError(
                f"Gerrit API returned HTTP {response.status_code} "
                f"for {method.upper()} {path}: {response.text}"
            )

        return response

    def _get(self, path: str, **kwargs) -> requests.Response:
        return self._request("GET", path, **kwargs)

    def _post(self, path: str, **kwargs) -> requests.Response:
        return self._request("POST", path, **kwargs)

    # -- Public API -----------------------------------------------------------

    def list_changes(
        self, status: str = "open", limit: int = 10, query: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve a list of changes from Gerrit.

        Parameters
        ----------
        status : str
            Change status filter (e.g. ``"open"``, ``"merged"``, ``"abandoned"``).
            Only used when *query* is ``None``.
        limit : int
            Maximum number of changes to return.
        query : str or None
            Full Gerrit query string (overrides *status* when provided).
            Example: ``"reviewer:code-reviewer+status:open"``.

        Returns
        -------
        list[dict]
            A list of change info dictionaries as returned by the Gerrit API.
        """
        if query:
            q = query
        else:
            q = f"status:{status}"
        params = {"q": q, "n": str(limit), "o": "CURRENT_REVISION"}
        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        response = self._get(f"/changes/?{query_string}")
        return parse_gerrit_response(response.text)

    def get_change(
        self, change_id: str
    ) -> dict[str, Any]:
        """Retrieve a single change by its ID.

        Parameters
        ----------
        change_id : str
            The Gerrit change ID (may include the ``project~branch~`` prefix).

        Returns
        -------
        dict
            The change info dictionary.
        """
        response = self._get(f"/changes/{change_id}")
        return parse_gerrit_response(response.text)

    def fetch_files(
        self, change_id: str, revision_id: str = "current"
    ) -> dict[str, Any]:
        """Fetch the list of files touched by a revision.

        Parameters
        ----------
        change_id : str
            The Gerrit change ID.
        revision_id : str
            The revision ID or ``"current"`` (default).

        Returns
        -------
        dict
            A dictionary mapping file paths to file metadata dicts.
        """
        response = self._get(
            f"/changes/{change_id}/revisions/{revision_id}/files/"
        )
        return parse_gerrit_response(response.text)

    def fetch_patch(
        self, change_id: str, revision_id: str = "current"
    ) -> str:
        """Fetch the unified diff for a revision.

        Parameters
        ----------
        change_id : str
            The Gerrit change ID.
        revision_id : str
            The revision ID or ``"current"`` (default).

        Returns
        -------
        str
            The unified diff text.
        """
        response = self._get(
            f"/changes/{change_id}/revisions/{revision_id}/patch"
        )
        return response.text

    def get_change_detail(
        self, change_id: str
    ) -> Dict[str, Any]:
        """Get change detail including messages and labels.

        Used to detect whether the current user has already reviewed a change by
        checking the ``messages`` list for review comments from the current user.
        """
        response = self._get(
            f"/changes/{change_id}/detail?o=MESSAGES&o=LABELS"
        )
        return parse_gerrit_response(response.text)

    def post_review(
        self,
        change_id: str,
        revision_id: str,
        message: str,
        score: int,
        comments: Optional[dict[str, list[dict[str, Any]]]] = None,
        tag: Optional[str] = None,
    ) -> dict[str, Any]:
        """Post a review on a revision.

        Parameters
        ----------
        change_id : str
            The Gerrit change ID.
        revision_id : str
            The revision ID (use ``"current"`` for the latest patch set).
        message : str
            The review message (cover letter).
        score : int
            The ``Code-Review`` score (typically -2 .. +2).
        comments : dict or None
            Optional inline comments keyed by file path.  Each entry is a list
            of comment dicts with ``line`` and ``message`` keys.

        Returns
        -------
        dict
            The API response (typically ``{"labels": ..., "ready": true}``).
        """
        comments = comments or {}
        body: dict[str, Any] = {
            "message": message,
            "labels": {"Code-Review": score},
            "comments": comments,
            "tag": tag or "autogenerated:gerrit:auto-review",
            "notify": "OWNER",
        }
        response = self._post(
            f"/changes/{change_id}/revisions/{revision_id}/review",
            json=body,
        )
        return parse_gerrit_response(response.text)
