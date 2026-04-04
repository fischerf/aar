# web-mcp

A minimal **Model Context Protocol (MCP) server** for web access — search the
web, fetch page content, and read RSS/Atom feeds. Designed to plug directly into
[aar](https://github.com/fischerf/aar) or any MCP-compatible client.

---

## File layout

```
web_mcp/
├── server.py           # MCP server — tool definitions
├── web_client.py       # HTTP, caching, extraction logic
├── __init__.py         # Package marker (required for aar's -c import)
├── requirements.txt
├── test_web_client.py  # Unit tests (no network needed)
├── test_mcp.py         # Smoke test (launches real server subprocess)
└── README.md
```

- **`web_client.py`** — all network, caching, and parsing logic; no MCP concepts.
- **`server.py`** — pure MCP wiring; imports from `web_client`, no HTTP code.

---

## Available tools

| Tool | Description |
|---|---|
| `search(query, limit)` | Web search via DuckDuckGo HTML — no API key required |
| `fetch(url)` | Fetch a page and extract its readable text content |
| `read_rss(feed_url, limit)` | Read RSS or Atom feed entries |

When used through aar with `prefix_tools: true`, tools are exposed as
`web__search`, `web__fetch`, and `web__read_rss`.

---

## Setup

### 1. Install dependencies

```bash
pip install -r tools/web_mcp/requirements.txt
```

Or install everything at once with the aar dev extras:

```bash
pip install -e ".[all,dev]"
```

### 2. No API keys required

`web_mcp` uses DuckDuckGo's HTML endpoint for search — no account or token needed.

### 3. SSL / corporate proxy

On systems with a corporate CA or SSL-inspecting proxy, `web_mcp` automatically
uses the OS certificate store via `truststore`. No configuration needed — as long
as the corporate CA is trusted at the OS level, HTTPS requests will work.

If you need to disable SSL verification entirely (not recommended):

```bash
# The truststore package must be installed (included in requirements.txt)
# Disable at OS level or contact your IT admin to add the CA to the system store.
```

---

## Using with aar

### Project-local config (`tools/mcp_servers.json`)

Used when running `aar` from the project root:

```json
{
  "servers": [
    {
      "name": "web",
      "transport": "stdio",
      "command": "python",
      "args": [
        "-c",
        "import sys; sys.path.insert(0, 'tools'); from web_mcp.server import mcp; mcp.run(transport='stdio')"
      ],
      "prefix_tools": true
    }
  ]
}
```

Launch:

```bash
aar tui --mcp-config tools/mcp_servers.json
```

### Global config (`~/.aar/mcp_servers.json`)

To use `web_mcp` from **any working directory**, use the absolute path to the
`tools/` directory:

```json
{
  "servers": [
    {
      "name": "web",
      "transport": "stdio",
      "command": "python",
      "args": [
        "-c",
        "import sys; sys.path.insert(0, '/absolute/path/to/aar/tools'); from web_mcp.server import mcp; mcp.run(transport='stdio')"
      ],
      "prefix_tools": true
    }
  ]
}
```

Replace `/absolute/path/to/aar/tools` with the actual path on your system,
e.g. `B:/Github_my/aar/tools` on Windows.

Launch from anywhere:

```bash
aar tui --mcp-config ~/.aar/mcp_servers.json
```

---

## Caching

Results are cached on disk in `.web_mcp_cache/` (relative to the working
directory when the server starts). Cache is handled automatically — no
configuration needed.

| Tool | Cache TTL |
|---|---|
| `search` | 5 minutes |
| `fetch` | 30 minutes |
| `read_rss` | 5 minutes |

---

## Testing

### Unit tests (no network)

```bash
pytest tools/web_mcp/test_web_client.py -v
```

All HTTP calls are mocked — runs offline in ~10 seconds.

### MCP smoke test (live network)

Launches the real server as a subprocess and calls each tool end-to-end:

```bash
# Run all tools with defaults
python tools/web_mcp/test_mcp.py

# Test a specific tool
python tools/web_mcp/test_mcp.py --tool search --query "python asyncio"
python tools/web_mcp/test_mcp.py --tool fetch --url https://example.com
python tools/web_mcp/test_mcp.py --tool read_rss --url https://feeds.bbci.co.uk/news/rss.xml --limit 5
```

---

## Example prompts

> "Search the web for recent Python asyncio news."

> "Fetch the content of https://docs.python.org/3/library/asyncio.html and summarise it."

> "Read the BBC News RSS feed and give me the top 5 headlines."

> "Search for 'fastmcp tutorial' and fetch the first result."

---

## Extending

### Add a new tool

Add the logic to `web_client.py`:

```python
async def read_sitemap(url: str) -> list[str]:
    url = _validate_url(url)
    resp = await _http_get(url)
    # parse sitemap XML ...
    return urls
```

Then expose it in `server.py`:

```python
@mcp.tool()
async def read_sitemap(
    url: Annotated[str, "URL of the sitemap.xml"],
) -> str:
    """List all URLs in a sitemap."""
    urls = await web_client.read_sitemap(url)
    return "\n".join(urls)
```

Restart the server — the new tool is immediately available to aar.
