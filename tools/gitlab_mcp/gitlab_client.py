"""
gitlab_client.py
~~~~~~~~~~~~~~~~
Thin, stateless wrapper around the GitLab REST API v4.

Add new API surface here — the MCP tools in server.py stay untouched.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx


# ---------------------------------------------------------------------------
# Configuration (read once at import time; override via env vars)
# ---------------------------------------------------------------------------
def _strip_quotes(value: str) -> str:
    """Strip accidental shell quoting from env var values.

    On Windows cmd.exe, ``set VAR="value"`` stores the literal quote
    characters as part of the value.  This helper removes them so that
    e.g. ``'"https://..."'`` becomes ``'https://...'``.
    """
    return value.strip().strip("\"'")


GITLAB_URL = _strip_quotes(os.environ.get("GITLAB_URL", "")).rstrip("/")
GITLAB_TOKEN = _strip_quotes(os.environ.get("GITLAB_TOKEN", ""))
GITLAB_USER = _strip_quotes(os.environ.get("GITLAB_USER", ""))  # optional: your username

# SSL verification:
#   GITLAB_SSL_VERIFY=0 or GITLAB_SSL_VERIFY=false  → disable (for internal CAs)
#   GITLAB_SSL_VERIFY=/path/to/ca-bundle.pem        → custom CA bundle
#   unset or GITLAB_SSL_VERIFY=1                    → system default (verify)
_ssl_verify_raw = os.environ.get("GITLAB_SSL_VERIFY", "1")
if _ssl_verify_raw.lower() in ("0", "false", "no"):
    GITLAB_SSL_VERIFY: bool | str = False
elif _ssl_verify_raw.lower() not in ("1", "true", "yes"):
    GITLAB_SSL_VERIFY = _ssl_verify_raw  # treat as a CA bundle path
else:
    GITLAB_SSL_VERIFY = True


class GitLabError(Exception):
    """Raised when the GitLab API returns a non-2xx response."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitLab API error {status}: {message}")
        self.status = status


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------


def _headers() -> dict[str, str]:
    if not GITLAB_TOKEN:
        raise RuntimeError("GITLAB_TOKEN environment variable is not set.")
    return {"PRIVATE-TOKEN": GITLAB_TOKEN, "Content-Type": "application/json"}


def _encode_project(project_id: str | int) -> str:
    """URL-encode a project identifier for use in API paths.

    The GitLab REST API requires that ``namespace/repo`` style project IDs
    are percent-encoded (``namespace%2Frepo``) when used as a path segment.
    Numeric IDs are returned unchanged.
    """
    return quote(str(project_id), safe="")


def _get(path: str, params: dict | None = None) -> Any:
    url = f"{GITLAB_URL}/api/v4{path}"
    with httpx.Client(timeout=30, verify=GITLAB_SSL_VERIFY) as client:
        r = client.get(url, headers=_headers(), params=params or {})
    if not r.is_success:
        raise GitLabError(r.status_code, r.text)
    return r.json()


def _post(path: str, body: dict) -> Any:
    url = f"{GITLAB_URL}/api/v4{path}"
    with httpx.Client(timeout=30, verify=GITLAB_SSL_VERIFY) as client:
        r = client.post(url, headers=_headers(), json=body)
    if not r.is_success:
        raise GitLabError(r.status_code, r.text)
    return r.json()


def _put(path: str, body: dict) -> Any:
    url = f"{GITLAB_URL}/api/v4{path}"
    with httpx.Client(timeout=30, verify=GITLAB_SSL_VERIFY) as client:
        r = client.put(url, headers=_headers(), json=body)
    if not r.is_success:
        raise GitLabError(r.status_code, r.text)
    return r.json()


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------


def list_assigned_issues(
    state: str = "opened",
    labels: str = "",
    project_id: str | int | None = None,
    per_page: int = 20,
) -> list[dict]:
    """Return issues assigned to the authenticated user (or GITLAB_USER)."""
    params: dict[str, Any] = {
        "scope": "assigned_to_me",
        "state": state,
        "per_page": per_page,
    }
    if labels:
        params["labels"] = labels

    if project_id:
        path = f"/projects/{project_id}/issues"
    else:
        path = "/issues"

    return _get(path, params)


def get_issue(project_id: str | int, issue_iid: int) -> dict:
    """Return a single issue by project + internal IID."""
    return _get(f"/projects/{_encode_project(project_id)}/issues/{issue_iid}")


def update_issue(
    project_id: str | int,
    issue_iid: int,
    *,
    state_event: str | None = None,  # "close" | "reopen"
    labels: str | None = None,
    assignee_ids: list[int] | None = None,
    title: str | None = None,
    description: str | None = None,
) -> dict:
    """Update mutable fields of an issue."""
    body: dict[str, Any] = {}
    if state_event:
        body["state_event"] = state_event
    if labels is not None:
        body["labels"] = labels
    if assignee_ids:
        body["assignee_ids"] = assignee_ids
    if title:
        body["title"] = title
    if description is not None:
        body["description"] = description
    return _put(f"/projects/{_encode_project(project_id)}/issues/{issue_iid}", body)


# ---------------------------------------------------------------------------
# Notes (comments)
# ---------------------------------------------------------------------------


def list_issue_notes(
    project_id: str | int,
    issue_iid: int,
    per_page: int = 50,
) -> list[dict]:
    """Return all notes (comments) on an issue, oldest first."""
    return _get(
        f"/projects/{_encode_project(project_id)}/issues/{issue_iid}/notes",
        {"sort": "asc", "per_page": per_page},
    )


def create_issue_note(
    project_id: str | int,
    issue_iid: int,
    body: str,
    *,
    internal: bool = False,  # true = confidential note (GitLab EE)
) -> dict:
    """Post a comment on an issue."""
    payload: dict[str, Any] = {"body": body}
    if internal:
        payload["internal"] = True
    return _post(f"/projects/{_encode_project(project_id)}/issues/{issue_iid}/notes", payload)


# ---------------------------------------------------------------------------
# Projects  (useful for resolving "project path" → numeric ID)
# ---------------------------------------------------------------------------


def list_projects(search: str = "", per_page: int = 20) -> list[dict]:
    """List projects accessible to the token (optionally filtered by name)."""
    params: dict[str, Any] = {"membership": True, "per_page": per_page}
    if search:
        params["search"] = search
    return _get("/projects", params)


def get_project(project_id: str | int) -> dict:
    """Fetch a single project by numeric ID or URL-encoded path."""
    return _get(f"/projects/{_encode_project(project_id)}")


# ---------------------------------------------------------------------------
# Current user
# ---------------------------------------------------------------------------


def get_current_user() -> dict:
    """Return the authenticated user's profile."""
    return _get("/user")
