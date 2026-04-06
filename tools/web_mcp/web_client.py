"""
web_client.py
~~~~~~~~~~~~~
Async network layer, caching, rate-limiting, and content extraction
for web search, page fetch, and RSS/Atom feed reading.

All HTTP access is centralised here so that the MCP server stays thin.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import feedparser
import httpx
from selectolax.parser import HTMLParser

# Use the OS/system certificate store (handles corporate proxies and custom CAs).
# Falls back to httpx's default (certifi) if truststore is not available.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Cache (diskcache)
# ---------------------------------------------------------------------------

import diskcache

_cache = diskcache.Cache(".web_mcp_cache", size_limit=50 * 1024 * 1024)  # 50 MB

# TTLs in seconds
_TTL_SEARCH = 300  # 5 min
_TTL_FETCH = 1800  # 30 min
_TTL_RSS = 300  # 5 min

# ---------------------------------------------------------------------------
# Rate-limit (simple global token bucket)
# ---------------------------------------------------------------------------

_MIN_REQUEST_INTERVAL = 1.0  # seconds between requests
_last_request_time: float = 0.0
_rate_lock = asyncio.Lock()


async def _wait_for_rate_limit() -> None:
    global _last_request_time
    async with _rate_lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        if elapsed < _MIN_REQUEST_INTERVAL:
            await asyncio.sleep(_MIN_REQUEST_INTERVAL - elapsed)
        _last_request_time = time.monotonic()


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

_USER_AGENT = (
    "Mozilla/5.0 (compatible; WebMCP/1.0; +https://github.com/fischerf/aar)"
)
_TIMEOUT = 15.0
_MAX_REDIRECTS = 5



def _cache_key(prefix: str, value: str) -> str:
    h = hashlib.sha256(value.encode()).hexdigest()[:16]
    return f"{prefix}:{h}"


async def _http_get(url: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
    """Perform a rate-limited async GET request."""
    await _wait_for_rate_limit()
    merged_headers = {"User-Agent": _USER_AGENT}
    if headers:
        merged_headers.update(headers)
    async with httpx.AsyncClient(
        timeout=_TIMEOUT,
        follow_redirects=True,
        max_redirects=_MAX_REDIRECTS,

    ) as client:
        return await client.get(url, headers=merged_headers)


def _validate_url(url: str) -> str:
    """Validate and normalise a URL. Raises ValueError on bad input."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme!r} (only http/https allowed)")
    if not parsed.netloc:
        raise ValueError(f"Invalid URL (no host): {url}")
    return url


# ---------------------------------------------------------------------------
# Search (DuckDuckGo HTML)
# ---------------------------------------------------------------------------

_DDG_URL = "https://html.duckduckgo.com/html/"


def _parse_ddg_results(html: str, limit: int) -> list[dict[str, str]]:
    """Extract search results from DuckDuckGo HTML response."""
    tree = HTMLParser(html)
    results: list[dict[str, str]] = []

    for node in tree.css("div.result"):
        if len(results) >= limit:
            break

        title_node = node.css_first("a.result__a")
        snippet_node = node.css_first("a.result__snippet")

        if not title_node:
            continue

        href = title_node.attributes.get("href", "")
        # DDG wraps URLs in a redirect — extract the actual target
        if "uddg=" in href:
            qs = parse_qs(urlparse(href).query)
            href = qs.get("uddg", [href])[0]

        title = title_node.text(strip=True)
        snippet = snippet_node.text(strip=True) if snippet_node else ""

        if title and href:
            results.append({"title": title, "url": href, "snippet": snippet})

    return results


async def search(query: str, limit: int = 10) -> list[dict[str, str]]:
    """Search the web via DuckDuckGo HTML and return results.

    Returns a list of dicts with keys: title, url, snippet.
    """
    if not query or not query.strip():
        raise ValueError("Search query must not be empty.")
    limit = max(1, min(limit, 30))

    key = _cache_key("search", f"{query}:{limit}")
    cached = _cache.get(key)
    if cached is not None:
        return cached

    await _wait_for_rate_limit()
    async with httpx.AsyncClient(
        timeout=_TIMEOUT,
        follow_redirects=True,
        max_redirects=_MAX_REDIRECTS,

    ) as client:
        resp = await client.post(
            _DDG_URL,
            data={"q": query, "b": ""},
            headers={"User-Agent": _USER_AGENT},
        )

    if not resp.is_success:
        raise RuntimeError(f"DuckDuckGo returned HTTP {resp.status_code}")

    results = _parse_ddg_results(resp.text, limit)
    _cache.set(key, results, expire=_TTL_SEARCH)
    return results


# ---------------------------------------------------------------------------
# Fetch (readable page extraction)
# ---------------------------------------------------------------------------

_STRIP_TAGS = {
    "script", "style", "nav", "footer", "header", "aside", "form",
    "iframe", "noscript", "svg", "button", "input", "select", "textarea",
}

_MAX_TEXT_LENGTH = 20_000  # characters


def _extract_readable(html: str, url: str) -> dict[str, str]:
    """Extract readable text content from HTML."""
    tree = HTMLParser(html)

    # Title
    title_node = tree.css_first("title")
    title = title_node.text(strip=True) if title_node else ""

    # Meta description
    description = ""
    meta_desc = tree.css_first('meta[name="description"]')
    if meta_desc:
        description = meta_desc.attributes.get("content", "")
    if not description:
        meta_og = tree.css_first('meta[property="og:description"]')
        if meta_og:
            description = meta_og.attributes.get("content", "")

    # Strip unwanted elements
    for tag in _STRIP_TAGS:
        for node in tree.css(tag):
            node.decompose()

    # Try <article> or <main> first, fall back to <body>
    content_node = tree.css_first("article") or tree.css_first("main") or tree.css_first("body")
    if content_node:
        text = content_node.text(separator="\n", strip=True)
    else:
        text = tree.text(separator="\n", strip=True)

    # Collapse excessive whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    if len(text) > _MAX_TEXT_LENGTH:
        text = text[:_MAX_TEXT_LENGTH] + "\n\n[... truncated]"

    return {"title": title, "description": description, "text": text, "url": url}


async def fetch(url: str) -> dict[str, str]:
    """Fetch a web page and extract its readable content.

    Returns a dict with keys: title, description, text, url.
    """
    url = _validate_url(url)

    key = _cache_key("fetch", url)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    resp = await _http_get(url)
    if not resp.is_success:
        raise RuntimeError(f"HTTP {resp.status_code} fetching {url}")

    content_type = resp.headers.get("content-type", "")
    if "text/html" not in content_type and "application/xhtml" not in content_type:
        # Return raw text for non-HTML content (text/plain, etc.)
        text = resp.text[:_MAX_TEXT_LENGTH]
        result = {"title": "", "description": "", "text": text, "url": url}
        _cache.set(key, result, expire=_TTL_FETCH)
        return result

    result = _extract_readable(resp.text, url)
    _cache.set(key, result, expire=_TTL_FETCH)
    return result


# ---------------------------------------------------------------------------
# RSS / Atom feeds
# ---------------------------------------------------------------------------


async def read_rss(feed_url: str, limit: int = 20) -> dict[str, Any]:
    """Read an RSS or Atom feed and return structured entries.

    Returns a dict with keys: feed_title, feed_description, feed_link, entries.
    Each entry has: title, link, published, summary.
    """
    feed_url = _validate_url(feed_url)
    limit = max(1, min(limit, 50))

    key = _cache_key("rss", f"{feed_url}:{limit}")
    cached = _cache.get(key)
    if cached is not None:
        return cached

    resp = await _http_get(feed_url)
    if not resp.is_success:
        raise RuntimeError(f"HTTP {resp.status_code} fetching feed {feed_url}")

    feed = feedparser.parse(resp.text)

    entries = []
    for entry in feed.entries[:limit]:
        summary = entry.get("summary", "")
        if len(summary) > 500:
            summary = summary[:500] + "..."
        entries.append({
            "title": entry.get("title", ""),
            "link": entry.get("link", ""),
            "published": entry.get("published", ""),
            "summary": summary,
        })

    result = {
        "feed_title": feed.feed.get("title", ""),
        "feed_description": feed.feed.get("description", ""),
        "feed_link": feed.feed.get("link", ""),
        "entries": entries,
    }
    _cache.set(key, result, expire=_TTL_RSS)
    return result
