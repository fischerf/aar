"""
test_mcp.py
~~~~~~~~~~~
Standalone smoke-test for the gitlab_mcp MCP server.

Launches server.py as a stdio subprocess (the same way rof_ai_demo does),
calls each tool in turn, and prints the results.  No ROF framework needed —
pure MCP SDK only.

Usage
-----
    # From the tools/gitlab_mcp directory:
    python test_mcp.py

    # With SSL verification disabled (internal / corporate CA):
    python test_mcp.py --no-verify

    # Test a specific tool only:
    python test_mcp.py --tool whoami
    python test_mcp.py --tool list_my_issues
    python test_mcp.py --tool find_projects

    # Read a specific issue (requires --project and --iid):
    python test_mcp.py --tool read_issue --project myteam/backend --iid 42

Environment variables
---------------------
    GITLAB_TOKEN      GitLab personal access token  (required)
    GITLAB_URL        GitLab instance URL            (default: from gitlab_client.py)
    GITLAB_SSL_VERIFY 0 / false to disable SSL       (overridden by --no-verify)

    On Windows cmd.exe, set WITHOUT quotes:
        set GITLAB_TOKEN=glpat-xxxxxxxxxxxx
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Resolve server.py path relative to this file
# ---------------------------------------------------------------------------
_HERE = Path(__file__).parent.resolve()
_SERVER = _HERE / "server.py"

# ---------------------------------------------------------------------------
# Colour helpers (no external deps)
# ---------------------------------------------------------------------------
_USE_COLOUR = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOUR else text


def green(t: str) -> str:
    return _c("92", t)


def red(t: str) -> str:
    return _c("91", t)


def yellow(t: str) -> str:
    return _c("93", t)


def cyan(t: str) -> str:
    return _c("96", t)


def bold(t: str) -> str:
    return _c("1", t)


def dim(t: str) -> str:
    return _c("2", t)


def _ok(label: str, detail: str = "") -> None:
    print(f"  {green('✔')} {bold(label)}" + (f"  {dim(detail)}" if detail else ""))


def _fail(label: str, detail: str = "") -> None:
    print(f"  {red('✗')} {bold(label)}" + (f"\n    {red(detail)}" if detail else ""))


def _section(title: str) -> None:
    bar = "─" * (60 - len(title) - 2)
    print(f"\n{cyan('──')}  {bold(title)}  {cyan(bar)}")


# ---------------------------------------------------------------------------
# MCP session helper
# ---------------------------------------------------------------------------


async def _run_test(args: argparse.Namespace) -> int:
    """
    Open one stdio MCP session to server.py, run the requested test(s),
    then close.  Returns 0 on full success, 1 if any test failed.
    """
    try:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
    except ImportError:
        print(red("ERROR: 'mcp' package not installed.  Run:  pip install mcp>=1.0"))
        return 1

    # ── Build subprocess env ──────────────────────────────────────────────
    env = dict(os.environ)  # full parent env so GITLAB_TOKEN etc. are inherited

    if args.no_verify:
        env["GITLAB_SSL_VERIFY"] = "0"
        env["PYTHONHTTPSVERIFY"] = "0"
        print(yellow("  ⚠ SSL verification disabled (--no-verify)"))

    # Warn if token looks like it has shell quotes embedded
    token = env.get("GITLAB_TOKEN", "").strip("\"'")
    raw_token = env.get("GITLAB_TOKEN", "")
    if raw_token and (raw_token.startswith('"') or raw_token.startswith("'")):
        print(
            yellow(
                f"  ⚠ GITLAB_TOKEN starts with a quote character ({raw_token[0]!r}) — stripping.\n"
                "    On Windows cmd.exe use:  set GITLAB_TOKEN=value  (no quotes).\n"
                '    On PowerShell use:       $env:GITLAB_TOKEN = "value"'
            )
        )
        # Fix it in the env that gets passed to the subprocess too.
        env["GITLAB_TOKEN"] = token

    if not token:
        # Not in the parent env — check if gitlab_client.py has a hardcoded default.
        try:
            import sys as _sys

            _saved = _sys.path[:]
            _sys.path.insert(0, str(_HERE))
            import importlib
            import os as _os

            _os.environ.setdefault("GITLAB_SSL_VERIFY", env.get("GITLAB_SSL_VERIFY", "1"))
            import gitlab_client as _gc

            _sys.path = _saved
            token = _gc.GITLAB_TOKEN or ""
        except Exception:
            token = ""

    if not token:
        print(
            red(
                "  ERROR: GITLAB_TOKEN is not set.\n"
                "  On Windows cmd.exe:  set GITLAB_TOKEN=glpat-xxxxxxxxxxxx\n"
                '  On PowerShell:       $env:GITLAB_TOKEN = "glpat-xxxxxxxxxxxx"'
            )
        )
        return 1

    # Make sure the resolved token is in the subprocess env.
    env["GITLAB_TOKEN"] = token

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(_SERVER)],
        env=env,
    )

    failures = 0

    # ── Connect ───────────────────────────────────────────────────────────
    _section("Connecting to gitlab_mcp server")
    print(f"  Server : {dim(str(_SERVER))}")
    print(f"  Python : {dim(sys.executable)}")
    print(f"  URL    : {dim(env.get('GITLAB_URL', '(from gitlab_client.py default)'))}")
    print()

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await asyncio.sleep(0)

            # ── tools/list ────────────────────────────────────────────────
            _section("Tool discovery  (tools/list)")
            result = await session.list_tools()
            tools = {t.name: t for t in result.tools}
            if not tools:
                _fail("No tools discovered — server may have failed to start")
                return 1

            for t_name, t_def in tools.items():
                desc = (getattr(t_def, "description", "") or "").strip()
                _ok(t_name, desc[:80])

            # ── decide which tools to run ─────────────────────────────────
            if args.tool:
                to_run = [args.tool]
                if args.tool not in tools:
                    print(
                        yellow(f"\n  ⚠ Tool '{args.tool}' not in discovered list; trying anyway…")
                    )
            else:
                # Default test order: fast / read-only first
                to_run = [t for t in ["whoami", "find_projects", "list_my_issues"] if t in tools]
                # Add read_issue only when explicitly requested (needs params)
                if not to_run:
                    to_run = list(tools.keys())

            # ── run each selected tool ────────────────────────────────────
            for tool_name in to_run:
                _section(f"Tool: {tool_name}")
                call_args = _build_args(tool_name, args)
                if call_args is not None:
                    print(f"  Args: {dim(repr(call_args))}")

                try:
                    resp = await session.call_tool(tool_name, call_args or {})
                except Exception as exc:
                    _fail(tool_name, f"Exception during call: {exc}")
                    failures += 1
                    continue

                if getattr(resp, "isError", False):
                    error_text = _extract_text(resp.content)
                    _fail(tool_name, error_text)
                    failures += 1
                else:
                    output = _extract_text(resp.content)
                    _ok(tool_name)
                    # Pretty-print the output
                    for line in output.splitlines():
                        print(f"    {line}")

    # ── Summary ───────────────────────────────────────────────────────────
    _section("Summary")
    if failures == 0:
        print(f"  {green('All tests passed.')}")
    else:
        print(f"  {red(f'{failures} test(s) failed.')}")
    return 0 if failures == 0 else 1


def _build_args(tool_name: str, args: argparse.Namespace) -> dict[str, Any] | None:
    """Build the arguments dict for a specific tool call."""
    if tool_name == "whoami":
        return {}

    if tool_name == "find_projects":
        return {"search": args.search or ""}

    if tool_name == "list_my_issues":
        call: dict[str, Any] = {"state": args.state, "limit": args.limit}
        if args.project:
            call["project_id"] = args.project
        if args.labels:
            call["labels"] = args.labels
        return call

    if tool_name == "read_issue":
        if not args.project or not args.iid:
            print(
                yellow(
                    "  ⚠ read_issue requires --project and --iid.\n"
                    "    Example:  --tool read_issue --project myteam/backend --iid 42"
                )
            )
            return None
        return {"project_id": args.project, "issue_iid": int(args.iid)}

    if tool_name == "answer_issue":
        if not args.project or not args.iid or not args.message:
            print(yellow("  ⚠ answer_issue requires --project, --iid, and --message."))
            return None
        return {"project_id": args.project, "issue_iid": int(args.iid), "message": args.message}

    if tool_name == "close_issue":
        if not args.project or not args.iid:
            print(yellow("  ⚠ close_issue requires --project and --iid."))
            return None
        return {"project_id": args.project, "issue_iid": int(args.iid)}

    if tool_name == "reopen_issue":
        if not args.project or not args.iid:
            print(yellow("  ⚠ reopen_issue requires --project and --iid."))
            return None
        return {"project_id": args.project, "issue_iid": int(args.iid)}

    if tool_name == "label_issue":
        if not args.project or not args.iid or not args.labels:
            print(yellow("  ⚠ label_issue requires --project, --iid, and --labels."))
            return None
        return {"project_id": args.project, "issue_iid": int(args.iid), "labels": args.labels}

    # Unknown tool — pass no arguments and let the server respond
    return {}


def _extract_text(content_list: Any) -> str:
    """Flatten an MCP content list to a plain string."""
    if not content_list:
        return ""
    parts: list[str] = []
    for item in content_list:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(str(text))
        else:
            parts.append(repr(item))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="test_mcp",
        description="Smoke-test the gitlab_mcp MCP server via stdio.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python test_mcp.py                                      # run whoami + find_projects + list_my_issues
  python test_mcp.py --no-verify                          # same, with SSL verification disabled
  python test_mcp.py --tool whoami                        # test whoami only
  python test_mcp.py --tool list_my_issues --limit 5      # list 5 open issues
  python test_mcp.py --tool read_issue --project ns/repo --iid 7
  python test_mcp.py --tool answer_issue --project ns/repo --iid 7 --message "Done!"
  python test_mcp.py --tool find_projects --search myteam
""",
    )

    p.add_argument(
        "--no-verify",
        dest="no_verify",
        action="store_true",
        default=False,
        help="Disable SSL certificate verification (sets GITLAB_SSL_VERIFY=0).",
    )
    p.add_argument(
        "--tool",
        metavar="NAME",
        default="",
        help=(
            "Run only this tool.  One of: whoami, find_projects, list_my_issues, "
            "read_issue, answer_issue, close_issue, reopen_issue, label_issue."
        ),
    )
    p.add_argument(
        "--project",
        metavar="ID_OR_PATH",
        default="",
        help="Project numeric ID or 'namespace/repo' path (required for issue tools).",
    )
    p.add_argument(
        "--iid",
        metavar="N",
        default="",
        help="Issue IID (the #number shown in GitLab).",
    )
    p.add_argument(
        "--state",
        choices=["opened", "closed", "all"],
        default="opened",
        help="Issue state filter for list_my_issues (default: opened).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=10,
        metavar="N",
        help="Max issues to return from list_my_issues (default: 10).",
    )
    p.add_argument(
        "--labels",
        metavar="LABELS",
        default="",
        help="Comma-separated label filter / replacement, e.g. 'bug,help wanted'.",
    )
    p.add_argument(
        "--search",
        metavar="TERM",
        default="",
        help="Search term for find_projects.",
    )
    p.add_argument(
        "--message",
        metavar="TEXT",
        default="",
        help="Comment text for answer_issue.",
    )

    return p.parse_args()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not _SERVER.exists():
        print(red(f"ERROR: server.py not found at {_SERVER}"))
        sys.exit(1)

    _args = _parse_args()

    print()
    print(bold("gitlab_mcp  –  MCP server smoke test"))
    print(dim("─" * 60))

    exit_code = asyncio.run(_run_test(_args))
    sys.exit(exit_code)
