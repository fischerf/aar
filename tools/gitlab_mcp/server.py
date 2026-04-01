"""
server.py
~~~~~~~~~
Minimal, extendable GitLab Issue MCP server.

Run:
    python server.py                   # stdio transport (default for Claude Desktop)
    python server.py --transport sse   # SSE transport (for remote / browser clients)

Add new tools by decorating a function with @mcp.tool().
Add new resources with @mcp.resource("gitlab://...").
"""

from __future__ import annotations

import json
from typing import Annotated

import gitlab_client as gl
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------
mcp = FastMCP(
    name="gitlab-issues",
    instructions="Read and manage GitLab issues assigned to you.",
)

# ---------------------------------------------------------------------------
# Helper: convert a GitLab issue dict to a compact, readable string
# ---------------------------------------------------------------------------


def _fmt_issue(i: dict) -> str:
    ref_full: str = i.get("references", {}).get("full", "")
    if ref_full:
        # references.full is already "namespace/project#iid" — use as-is.
        header = ref_full
    else:
        # Fall back to numeric project_id + iid.
        header = f"{i.get('project_id', '?')}#{i['iid']}"
    return (
        f"[{header}] {i['title']}\n"
        f"  state   : {i['state']}\n"
        f"  labels  : {', '.join(i.get('labels', [])) or '—'}\n"
        f"  url     : {i.get('web_url', '')}\n"
        f"  desc    : {(i.get('description') or '—')[:300]}"
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def whoami() -> str:
    """Return the authenticated GitLab user's name and username."""
    u = gl.get_current_user()
    return f"{u['name']} (@{u['username']})  —  {u.get('web_url', '')}"


@mcp.tool()
def list_my_issues(
    state: Annotated[str, "Issue state: opened | closed | all"] = "opened",
    labels: Annotated[str, "Comma-separated label filter, e.g. 'bug,help wanted'"] = "",
    project_id: Annotated[str, "Limit to this project (numeric ID or 'namespace/repo')"] = "",
    limit: Annotated[int, "Max number of issues to return (1-50)"] = 20,
) -> str:
    """List GitLab issues currently assigned to you."""
    issues = gl.list_assigned_issues(
        state=state,
        labels=labels,
        project_id=project_id or None,
        per_page=min(limit, 50),
    )
    if not issues:
        return "No issues found."
    return "\n\n".join(_fmt_issue(i) for i in issues)


@mcp.tool()
def read_issue(
    project_id: Annotated[str, "Numeric project ID or 'namespace/repo'"],
    issue_iid: Annotated[int, "Issue IID (the #number shown in GitLab)"],
) -> str:
    """Read the full details of a specific GitLab issue including description."""
    issue = gl.get_issue(project_id, issue_iid)
    notes = gl.list_issue_notes(project_id, issue_iid)

    user_notes = [n for n in notes if not n.get("system")]
    system_notes = [n for n in notes if n.get("system")]

    thread = "\n\n".join(
        f"  [{n['created_at'][:10]}] {n['author']['username']}:\n  {n['body']}" for n in user_notes
    )

    activity = "\n".join(f"  [{n['created_at'][:10]}] {n['body']}" for n in system_notes)

    result = _fmt_issue(issue)
    result += f"\n\n--- COMMENTS ({len(user_notes)}) ---\n"
    result += thread or "  (no comments yet)"
    if activity:
        result += f"\n\n--- ACTIVITY ({len(system_notes)}) ---\n{activity}"
    return result


@mcp.tool()
def answer_issue(
    project_id: Annotated[str, "Numeric project ID or 'namespace/repo'"],
    issue_iid: Annotated[int, "Issue IID"],
    message: Annotated[str, "The comment text to post (markdown supported)"],
    internal: Annotated[bool, "Post as confidential / internal note (GitLab EE only)"] = False,
) -> str:
    """Post a comment (note) on a GitLab issue."""
    note = gl.create_issue_note(project_id, issue_iid, message, internal=internal)
    note_ref = note.get("web_url") or f"note #{note['id']}"
    return f"Comment posted: {note_ref}"


@mcp.tool()
def close_issue(
    project_id: Annotated[str, "Numeric project ID or 'namespace/repo'"],
    issue_iid: Annotated[int, "Issue IID"],
    comment: Annotated[str, "Optional closing comment to post before closing"] = "",
) -> str:
    """Close a GitLab issue, optionally posting a final comment first."""
    if comment:
        gl.create_issue_note(project_id, issue_iid, comment)
    updated = gl.update_issue(project_id, issue_iid, state_event="close")
    return f"Issue {project_id}#{issue_iid} is now {updated['state']}."


@mcp.tool()
def reopen_issue(
    project_id: Annotated[str, "Numeric project ID or 'namespace/repo'"],
    issue_iid: Annotated[int, "Issue IID"],
) -> str:
    """Reopen a closed GitLab issue."""
    updated = gl.update_issue(project_id, issue_iid, state_event="reopen")
    return f"Issue {project_id}#{issue_iid} is now {updated['state']}."


@mcp.tool()
def label_issue(
    project_id: Annotated[str, "Numeric project ID or 'namespace/repo'"],
    issue_iid: Annotated[int, "Issue IID"],
    labels: Annotated[str, "Comma-separated list of labels to set (replaces existing)"],
) -> str:
    """Set (replace) the labels on a GitLab issue."""
    updated = gl.update_issue(project_id, issue_iid, labels=labels)
    current = ", ".join(updated.get("labels", []))
    return f"Labels updated → {current or '(none)'}"


@mcp.tool()
def find_projects(
    search: Annotated[str, "Search term to filter project names"] = "",
) -> str:
    """List GitLab projects you are a member of (optionally filtered)."""
    projects = gl.list_projects(search=search)
    if not projects:
        return "No projects found."
    lines = [f"{p['id']:>8}  {p['path_with_namespace']}" for p in projects]
    return "     ID  PATH\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Resources  (optional — expose raw JSON for programmatic consumers)
# ---------------------------------------------------------------------------


@mcp.resource("gitlab://issues/assigned")
def resource_assigned_issues() -> str:
    """Raw JSON of your open assigned issues."""
    issues = gl.list_assigned_issues()
    return json.dumps(issues, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    transport = "stdio"
    if "--transport" in sys.argv:
        idx = sys.argv.index("--transport")
        transport = sys.argv[idx + 1]

    mcp.run(transport=transport)
