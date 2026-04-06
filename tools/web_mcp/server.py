"""
server.py
~~~~~~~~~
Minimal Web MCP server: search, fetch, and RSS/Atom feed reading.

Run:
    python server.py                   # stdio transport (default)
    python server.py --transport sse   # SSE transport

Tools:
    search(query, limit)       — Web search via DuckDuckGo HTML
    fetch(url)                 — Fetch and extract readable page content
    read_rss(feed_url, limit)  — Read RSS/Atom feed entries
"""

from __future__ import annotations

from typing import Annotated

try:
    from . import web_client
except ImportError:
    import web_client  # type: ignore[no-redef]
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------
mcp = FastMCP(
    name="web-tools",
    instructions=(
        "Search the web, fetch web page content, and read RSS/Atom feeds. "
        "Use these tools for research, information gathering, and monitoring."
    ),
)

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def search(
    query: Annotated[str, "Search query string"],
    limit: Annotated[int, "Max number of results to return (1-30)"] = 10,
) -> str:
    """Search the web using DuckDuckGo and return results with title, URL, and snippet."""
    results = await web_client.search(query, limit=limit)
    if not results:
        return "No results found."
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(
            f"{i}. {r['title']}\n"
            f"   {r['url']}\n"
            f"   {r['snippet']}"
        )
    return "\n\n".join(lines)


@mcp.tool()
async def fetch(
    url: Annotated[str, "URL of the web page to fetch (http or https)"],
) -> str:
    """Fetch a web page and extract its readable text content."""
    result = await web_client.fetch(url)
    parts = []
    if result["title"]:
        parts.append(f"Title: {result['title']}")
    if result["description"]:
        parts.append(f"Description: {result['description']}")
    parts.append(f"URL: {result['url']}")
    parts.append(f"\n{result['text']}")
    return "\n".join(parts)


@mcp.tool()
async def read_rss(
    feed_url: Annotated[str, "URL of the RSS or Atom feed"],
    limit: Annotated[int, "Max number of entries to return (1-50)"] = 20,
) -> str:
    """Read an RSS or Atom feed and return the latest entries."""
    result = await web_client.read_rss(feed_url, limit=limit)

    parts = []
    if result["feed_title"]:
        parts.append(f"Feed: {result['feed_title']}")
    if result["feed_description"]:
        parts.append(f"Description: {result['feed_description']}")
    if result["feed_link"]:
        parts.append(f"Link: {result['feed_link']}")
    parts.append(f"Entries: {len(result['entries'])}")
    parts.append("")

    for i, entry in enumerate(result["entries"], 1):
        lines = [f"{i}. {entry['title']}"]
        if entry["link"]:
            lines.append(f"   {entry['link']}")
        if entry["published"]:
            lines.append(f"   Published: {entry['published']}")
        if entry["summary"]:
            lines.append(f"   {entry['summary']}")
        parts.append("\n".join(lines))

    return "\n\n".join(parts)


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
