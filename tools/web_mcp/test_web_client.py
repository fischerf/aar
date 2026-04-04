"""
test_web_client.py
~~~~~~~~~~~~~~~~~~
Unit tests for web_client.py — no network access required.

All HTTP calls are mocked via httpx transport mocking.

Usage:
    pytest tools/web_mcp/test_web_client.py -v
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio

# Make web_client importable
sys.path.insert(0, str(Path(__file__).parent))
import web_client  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _disable_rate_limit():
    """Remove rate-limit delay for fast tests."""
    with patch.object(web_client, "_MIN_REQUEST_INTERVAL", 0.0):
        yield


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear the disk cache before each test so results are isolated."""
    web_client._cache.clear()
    yield
    web_client._cache.clear()


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


class TestValidateUrl:
    def test_http_accepted(self):
        assert web_client._validate_url("http://example.com") == "http://example.com"

    def test_https_accepted(self):
        assert web_client._validate_url("https://example.com/path") == "https://example.com/path"

    def test_ftp_rejected(self):
        with pytest.raises(ValueError, match="Unsupported URL scheme"):
            web_client._validate_url("ftp://example.com")

    def test_file_rejected(self):
        with pytest.raises(ValueError, match="Unsupported URL scheme"):
            web_client._validate_url("file:///etc/passwd")

    def test_empty_scheme_rejected(self):
        with pytest.raises(ValueError, match="Unsupported URL scheme"):
            web_client._validate_url("example.com")

    def test_no_host_rejected(self):
        with pytest.raises(ValueError, match="no host"):
            web_client._validate_url("http://")


# ---------------------------------------------------------------------------
# HTML extraction
# ---------------------------------------------------------------------------


_SAMPLE_HTML = """\
<!DOCTYPE html>
<html>
<head>
    <title>Test Page Title</title>
    <meta name="description" content="A test page description">
</head>
<body>
    <nav><a href="/">Home</a></nav>
    <header><h1>Site Header</h1></header>
    <article>
        <h2>Article Heading</h2>
        <p>This is the main article content.</p>
        <p>Second paragraph of useful text.</p>
    </article>
    <footer>Copyright 2026</footer>
    <script>alert('evil');</script>
</body>
</html>
"""


class TestExtractReadable:
    def test_extracts_title(self):
        result = web_client._extract_readable(_SAMPLE_HTML, "https://example.com")
        assert result["title"] == "Test Page Title"

    def test_extracts_description(self):
        result = web_client._extract_readable(_SAMPLE_HTML, "https://example.com")
        assert result["description"] == "A test page description"

    def test_extracts_article_content(self):
        result = web_client._extract_readable(_SAMPLE_HTML, "https://example.com")
        assert "Article Heading" in result["text"]
        assert "main article content" in result["text"]

    def test_strips_nav_footer_script(self):
        result = web_client._extract_readable(_SAMPLE_HTML, "https://example.com")
        assert "alert" not in result["text"]
        assert "Copyright 2026" not in result["text"]

    def test_preserves_url(self):
        result = web_client._extract_readable(_SAMPLE_HTML, "https://example.com/page")
        assert result["url"] == "https://example.com/page"

    def test_og_description_fallback(self):
        html = """\
        <html><head>
            <meta property="og:description" content="OG desc">
        </head><body><p>text</p></body></html>
        """
        result = web_client._extract_readable(html, "https://x.com")
        assert result["description"] == "OG desc"

    def test_truncates_long_text(self):
        long_body = "<html><body><p>" + "x" * 25_000 + "</p></body></html>"
        result = web_client._extract_readable(long_body, "https://x.com")
        assert len(result["text"]) <= web_client._MAX_TEXT_LENGTH + 50  # +margin for suffix
        assert "[... truncated]" in result["text"]

    def test_falls_back_to_body(self):
        html = "<html><body><p>Body only content</p></body></html>"
        result = web_client._extract_readable(html, "https://x.com")
        assert "Body only content" in result["text"]


# ---------------------------------------------------------------------------
# DDG result parsing
# ---------------------------------------------------------------------------


_SAMPLE_DDG_HTML = """\
<html><body>
<div class="result">
    <a class="result__a" href="https://example.com/page1">First Result</a>
    <a class="result__snippet">Snippet for first result.</a>
</div>
<div class="result">
    <a class="result__a" href="https://example.com/page2">Second Result</a>
    <a class="result__snippet">Snippet for second result.</a>
</div>
<div class="result">
    <a class="result__a" href="https://example.com/page3">Third Result</a>
</div>
</body></html>
"""


class TestParseDdgResults:
    def test_parses_results(self):
        results = web_client._parse_ddg_results(_SAMPLE_DDG_HTML, 10)
        assert len(results) == 3
        assert results[0]["title"] == "First Result"
        assert results[0]["url"] == "https://example.com/page1"
        assert results[0]["snippet"] == "Snippet for first result."

    def test_respects_limit(self):
        results = web_client._parse_ddg_results(_SAMPLE_DDG_HTML, 2)
        assert len(results) == 2

    def test_missing_snippet_is_empty(self):
        results = web_client._parse_ddg_results(_SAMPLE_DDG_HTML, 10)
        assert results[2]["snippet"] == ""

    def test_extracts_uddg_redirect(self):
        html = """\
        <html><body>
        <div class="result">
            <a class="result__a"
               href="//duckduckgo.com/l/?uddg=https%3A%2F%2Freal.example.com&amp;rut=abc">
               Title
            </a>
        </div>
        </body></html>
        """
        results = web_client._parse_ddg_results(html, 10)
        assert len(results) == 1
        assert results[0]["url"] == "https://real.example.com"

    def test_empty_html_returns_empty(self):
        results = web_client._parse_ddg_results("<html><body></body></html>", 10)
        assert results == []


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------


class TestCacheKey:
    def test_deterministic(self):
        k1 = web_client._cache_key("search", "python asyncio")
        k2 = web_client._cache_key("search", "python asyncio")
        assert k1 == k2

    def test_different_prefix(self):
        k1 = web_client._cache_key("search", "test")
        k2 = web_client._cache_key("fetch", "test")
        assert k1 != k2

    def test_different_value(self):
        k1 = web_client._cache_key("search", "foo")
        k2 = web_client._cache_key("search", "bar")
        assert k1 != k2


# ---------------------------------------------------------------------------
# search() — async with mocked HTTP
# ---------------------------------------------------------------------------


class TestSearch:
    @pytest.mark.asyncio
    async def test_empty_query_raises(self):
        with pytest.raises(ValueError, match="empty"):
            await web_client.search("")

    @pytest.mark.asyncio
    async def test_whitespace_query_raises(self):
        with pytest.raises(ValueError, match="empty"):
            await web_client.search("   ")

    @pytest.mark.asyncio
    async def test_returns_parsed_results(self):
        mock_resp = httpx.Response(200, text=_SAMPLE_DDG_HTML)

        async def mock_post(*args, **kwargs):
            return mock_resp

        with patch("httpx.AsyncClient.post", side_effect=mock_post):
            results = await web_client.search("test query", limit=5)

        assert len(results) == 3
        assert results[0]["title"] == "First Result"

    @pytest.mark.asyncio
    async def test_limit_clamped_high(self):
        mock_resp = httpx.Response(200, text=_SAMPLE_DDG_HTML)

        async def mock_post(*args, **kwargs):
            return mock_resp

        with patch("httpx.AsyncClient.post", side_effect=mock_post):
            results = await web_client.search("test", limit=100)

        # limit is clamped to 30, but only 3 results exist in mock HTML
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_caches_results(self):
        mock_resp = httpx.Response(200, text=_SAMPLE_DDG_HTML)
        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return mock_resp

        with patch("httpx.AsyncClient.post", side_effect=mock_post):
            r1 = await web_client.search("cached query", limit=10)
            r2 = await web_client.search("cached query", limit=10)

        assert r1 == r2
        assert call_count == 1  # second call served from cache

    @pytest.mark.asyncio
    async def test_http_error_raises(self):
        mock_resp = httpx.Response(503, text="Service Unavailable")

        async def mock_post(*args, **kwargs):
            return mock_resp

        with (
            patch("httpx.AsyncClient.post", side_effect=mock_post),
            pytest.raises(RuntimeError, match="503"),
        ):
            await web_client.search("fail query")


# ---------------------------------------------------------------------------
# fetch() — async with mocked HTTP
# ---------------------------------------------------------------------------


class TestFetch:
    @pytest.mark.asyncio
    async def test_extracts_html_page(self):
        mock_resp = httpx.Response(
            200,
            text=_SAMPLE_HTML,
            headers={"content-type": "text/html; charset=utf-8"},
        )

        async def mock_get(*args, **kwargs):
            return mock_resp

        with patch("httpx.AsyncClient.get", side_effect=mock_get):
            result = await web_client.fetch("https://example.com")

        assert result["title"] == "Test Page Title"
        assert "main article content" in result["text"]

    @pytest.mark.asyncio
    async def test_non_html_returns_raw_text(self):
        mock_resp = httpx.Response(
            200,
            text="plain text content here",
            headers={"content-type": "text/plain"},
        )

        async def mock_get(*args, **kwargs):
            return mock_resp

        with patch("httpx.AsyncClient.get", side_effect=mock_get):
            result = await web_client.fetch("https://example.com/file.txt")

        assert result["text"] == "plain text content here"
        assert result["title"] == ""

    @pytest.mark.asyncio
    async def test_invalid_scheme_raises(self):
        with pytest.raises(ValueError, match="Unsupported URL scheme"):
            await web_client.fetch("ftp://example.com/file")

    @pytest.mark.asyncio
    async def test_http_error_raises(self):
        mock_resp = httpx.Response(404, text="Not Found")

        async def mock_get(*args, **kwargs):
            return mock_resp

        with (
            patch("httpx.AsyncClient.get", side_effect=mock_get),
            pytest.raises(RuntimeError, match="404"),
        ):
            await web_client.fetch("https://example.com/missing")

    @pytest.mark.asyncio
    async def test_caches_results(self):
        mock_resp = httpx.Response(
            200,
            text=_SAMPLE_HTML,
            headers={"content-type": "text/html"},
        )
        call_count = 0

        async def mock_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return mock_resp

        with patch("httpx.AsyncClient.get", side_effect=mock_get):
            r1 = await web_client.fetch("https://example.com/cached")
            r2 = await web_client.fetch("https://example.com/cached")

        assert r1 == r2
        assert call_count == 1


# ---------------------------------------------------------------------------
# read_rss() — async with mocked HTTP
# ---------------------------------------------------------------------------

_SAMPLE_RSS = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
    <title>Test Feed</title>
    <description>A test RSS feed</description>
    <link>https://example.com</link>
    <item>
        <title>First Entry</title>
        <link>https://example.com/entry1</link>
        <pubDate>Fri, 04 Apr 2026 12:00:00 GMT</pubDate>
        <description>Summary of the first entry.</description>
    </item>
    <item>
        <title>Second Entry</title>
        <link>https://example.com/entry2</link>
        <pubDate>Thu, 03 Apr 2026 12:00:00 GMT</pubDate>
        <description>Summary of the second entry.</description>
    </item>
</channel>
</rss>
"""

_SAMPLE_ATOM = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
    <title>Atom Feed</title>
    <subtitle>An Atom test feed</subtitle>
    <link href="https://atom.example.com"/>
    <entry>
        <title>Atom Entry</title>
        <link href="https://atom.example.com/1"/>
        <published>2026-04-04T10:00:00Z</published>
        <summary>Atom entry summary.</summary>
    </entry>
</feed>
"""


class TestReadRss:
    @pytest.mark.asyncio
    async def test_parses_rss(self):
        mock_resp = httpx.Response(200, text=_SAMPLE_RSS)

        async def mock_get(*args, **kwargs):
            return mock_resp

        with patch("httpx.AsyncClient.get", side_effect=mock_get):
            result = await web_client.read_rss("https://example.com/feed.xml")

        assert result["feed_title"] == "Test Feed"
        assert result["feed_description"] == "A test RSS feed"
        assert len(result["entries"]) == 2
        assert result["entries"][0]["title"] == "First Entry"
        assert result["entries"][0]["link"] == "https://example.com/entry1"
        assert "Summary of the first" in result["entries"][0]["summary"]

    @pytest.mark.asyncio
    async def test_parses_atom(self):
        mock_resp = httpx.Response(200, text=_SAMPLE_ATOM)

        async def mock_get(*args, **kwargs):
            return mock_resp

        with patch("httpx.AsyncClient.get", side_effect=mock_get):
            result = await web_client.read_rss("https://atom.example.com/feed")

        assert result["feed_title"] == "Atom Feed"
        assert len(result["entries"]) == 1
        assert result["entries"][0]["title"] == "Atom Entry"

    @pytest.mark.asyncio
    async def test_respects_limit(self):
        mock_resp = httpx.Response(200, text=_SAMPLE_RSS)

        async def mock_get(*args, **kwargs):
            return mock_resp

        with patch("httpx.AsyncClient.get", side_effect=mock_get):
            result = await web_client.read_rss("https://example.com/feed.xml", limit=1)

        assert len(result["entries"]) == 1

    @pytest.mark.asyncio
    async def test_invalid_scheme_raises(self):
        with pytest.raises(ValueError, match="Unsupported URL scheme"):
            await web_client.read_rss("ftp://example.com/feed.xml")

    @pytest.mark.asyncio
    async def test_http_error_raises(self):
        mock_resp = httpx.Response(500, text="Internal Server Error")

        async def mock_get(*args, **kwargs):
            return mock_resp

        with (
            patch("httpx.AsyncClient.get", side_effect=mock_get),
            pytest.raises(RuntimeError, match="500"),
        ):
            await web_client.read_rss("https://example.com/broken-feed")

    @pytest.mark.asyncio
    async def test_truncates_long_summary(self):
        long_summary = "x" * 1000
        rss = f"""\
<?xml version="1.0"?>
<rss version="2.0"><channel><title>T</title>
<item><title>Long</title><description>{long_summary}</description></item>
</channel></rss>"""
        mock_resp = httpx.Response(200, text=rss)

        async def mock_get(*args, **kwargs):
            return mock_resp

        with patch("httpx.AsyncClient.get", side_effect=mock_get):
            result = await web_client.read_rss("https://example.com/long-feed")

        assert len(result["entries"][0]["summary"]) <= 504  # 500 + "..."

    @pytest.mark.asyncio
    async def test_caches_results(self):
        mock_resp = httpx.Response(200, text=_SAMPLE_RSS)
        call_count = 0

        async def mock_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return mock_resp

        with patch("httpx.AsyncClient.get", side_effect=mock_get):
            r1 = await web_client.read_rss("https://example.com/cached-feed")
            r2 = await web_client.read_rss("https://example.com/cached-feed")

        assert r1 == r2
        assert call_count == 1


# ---------------------------------------------------------------------------
# Config loading (aar integration)
# ---------------------------------------------------------------------------


class TestMcpConfigLoading:
    """Verify that mcp_servers.json loads and includes the web server entry."""

    def test_load_config_includes_web(self):
        config_path = Path(__file__).parent.parent / "mcp_servers.json"
        if not config_path.exists():
            pytest.skip("mcp_servers.json not found")

        # Use aar's own config loader
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from agent.extensions.mcp import load_mcp_config

        configs = load_mcp_config(str(config_path))
        names = [c.name for c in configs]
        assert "web" in names

        web_cfg = next(c for c in configs if c.name == "web")
        assert web_cfg.transport == "stdio"
        assert web_cfg.prefix_tools is True
        assert "web_mcp.server" in " ".join(web_cfg.args)
