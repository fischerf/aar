"""
test_mcp.py
~~~~~~~~~~~
Standalone smoke-test for the web_mcp MCP server.

Launches server.py as a stdio subprocess (the same way aar does),
calls each tool in turn, and prints the results.  No framework needed —
pure MCP SDK only.

Usage
-----
    # From the repo root:
    python tools/web_mcp/test_mcp.py

    # Test a specific tool only:
    python tools/web_mcp/test_mcp.py --tool search --query "python asyncio"
    python tools/web_mcp/test_mcp.py --tool fetch --url https://example.com
    python tools/web_mcp/test_mcp.py --tool read_rss --url https://feeds.bbci.co.uk/news/rss.xml
"""

from __future__ import annotations

import argparse
import asyncio
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
    print(f"  {green('OK')} {bold(label)}" + (f"  {dim(detail)}" if detail else ""))


def _fail(label: str, detail: str = "") -> None:
    print(f"  {red('FAIL')} {bold(label)}" + (f"\n    {red(detail)}" if detail else ""))


def _section(title: str) -> None:
    bar = "-" * (60 - len(title) - 2)
    print(f"\n{cyan('--')}  {bold(title)}  {cyan(bar)}")


# ---------------------------------------------------------------------------
# MCP session helper
# ---------------------------------------------------------------------------


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


def _build_args(tool_name: str, args: argparse.Namespace) -> dict[str, Any] | None:
    """Build the arguments dict for a specific tool call."""
    if tool_name == "search":
        query = args.query or "python programming"
        return {"query": query, "limit": args.limit}

    if tool_name == "fetch":
        url = args.url or "https://example.com"
        return {"url": url}

    if tool_name == "read_rss":
        url = args.url or "https://feeds.bbci.co.uk/news/rss.xml"
        return {"feed_url": url, "limit": args.limit}

    return {}


async def _run_test(args: argparse.Namespace) -> int:
    """
    Open one stdio MCP session to server.py, run the requested test(s),
    then close.  Returns 0 on full success, 1 if any test failed.
    """
    try:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
    except ImportError:
        print(red("ERROR: 'mcp' package not installed.  Run:  pip install 'mcp[cli]>=1.0'"))
        return 1

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(_SERVER)],
    )

    failures = 0

    # ── Connect ───────────────────────────────────────────────────────────
    _section("Connecting to web_mcp server")
    print(f"  Server : {dim(str(_SERVER))}")
    print(f"  Python : {dim(sys.executable)}")
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
                        yellow(f"\n  WARNING: Tool '{args.tool}' not in discovered list; trying anyway...")
                    )
            else:
                to_run = [t for t in ["search", "fetch", "read_rss"] if t in tools]
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
                    for line in output.splitlines()[:30]:
                        print(f"    {line}")
                    total_lines = len(output.splitlines())
                    if total_lines > 30:
                        print(dim(f"    ... ({total_lines - 30} more lines)"))

    # ── Summary ───────────────────────────────────────────────────────────
    _section("Summary")
    if failures == 0:
        print(f"  {green('All tests passed.')}")
    else:
        print(f"  {red(f'{failures} test(s) failed.')}")
    return 0 if failures == 0 else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="test_mcp",
        description="Smoke-test the web_mcp MCP server via stdio.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python test_mcp.py                                       # run all three tools
  python test_mcp.py --tool search --query "rust lang"     # search only
  python test_mcp.py --tool fetch --url https://example.com
  python test_mcp.py --tool read_rss --url https://feeds.bbci.co.uk/news/rss.xml --limit 5
""",
    )

    p.add_argument(
        "--tool",
        metavar="NAME",
        default="",
        help="Run only this tool.  One of: search, fetch, read_rss.",
    )
    p.add_argument(
        "--query",
        metavar="TEXT",
        default="",
        help="Search query for the search tool (default: 'python programming').",
    )
    p.add_argument(
        "--url",
        metavar="URL",
        default="",
        help="URL for fetch or read_rss tools.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=5,
        metavar="N",
        help="Max results/entries to return (default: 5).",
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
    print(bold("web_mcp  –  MCP server smoke test"))
    print(dim("-" * 60))

    exit_code = asyncio.run(_run_test(_args))
    sys.exit(exit_code)
