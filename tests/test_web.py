"""Tests for the ``agent.transports.web`` HTTP/SSE transport.

Covers #7 (review-2026-06-plan):
  - concurrent ``handle_stream`` calls without ``session_id`` must not
    bleed events between sessions (each gets a unique ephemeral key);
  - SSE client disconnect must cancel the in-flight agent run promptly
    so the agent doesn't keep burning tokens for a dead listener;
  - malformed JSON bodies must produce a 400 rather than a 500.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from agent.core.config import AgentConfig, ProviderConfig
from agent.transports.web import WebTransport, create_asgi_app
from tests.conftest import MockProvider

# ---------------------------------------------------------------------------
# Test scaffolding: a minimal ASGI driver that lets us inject ``receive`` /
# ``send`` traffic without standing up a real HTTP server.
# ---------------------------------------------------------------------------


class _AsgiDriver:
    """Pump an ASGI app with a queue-backed ``receive`` and capturing ``send``."""

    def __init__(self) -> None:
        self.recv_queue: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self._send_event = asyncio.Event()

    async def receive(self):
        return await self.recv_queue.get()

    async def send(self, msg):
        self.sent.append(msg)
        self._send_event.set()

    async def wait_for_send(self, predicate, timeout: float = 2.0) -> dict:
        """Block until a sent message satisfies ``predicate``."""
        end = asyncio.get_event_loop().time() + timeout
        idx = 0
        while True:
            for i in range(idx, len(self.sent)):
                if predicate(self.sent[i]):
                    return self.sent[i]
            idx = len(self.sent)
            self._send_event.clear()
            remaining = end - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError(f"timed out waiting for send; got {self.sent!r}")
            try:
                await asyncio.wait_for(self._send_event.wait(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise asyncio.TimeoutError(
                    f"timed out waiting for send; got {self.sent!r}"
                ) from exc


def _make_post_scope(path: str) -> dict:
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [],
        "query_string": b"",
    }


async def _post(app, path: str, body: bytes) -> _AsgiDriver:
    driver = _AsgiDriver()
    await driver.recv_queue.put({"type": "http.request", "body": body, "more_body": False})
    await app(_make_post_scope(path), driver.receive, driver.send)
    return driver


# ---------------------------------------------------------------------------
# Malformed JSON / bad session_id must yield 400, not 500
# ---------------------------------------------------------------------------


class TestBadRequestHandling:
    """#7 — wrap ``json.loads`` + ``validate_session_id`` in try/except."""

    def _make_app(self):
        config = AgentConfig(provider=ProviderConfig(name="mock", model="mock-1"))
        provider = MockProvider()

        def _factory(*_a, **_kw):
            return provider

        with patch("agent.core.agent._create_provider", _factory):
            yield create_asgi_app(config=config)

    @pytest.mark.asyncio
    async def test_malformed_json_chat_returns_400(self) -> None:
        config = AgentConfig(provider=ProviderConfig(name="mock", model="mock-1"))
        app = create_asgi_app(config=config)
        driver = await _post(app, "/chat", b"{not valid json")
        start = next(m for m in driver.sent if m["type"] == "http.response.start")
        assert start["status"] == 400
        body = b"".join(m["body"] for m in driver.sent if m["type"] == "http.response.body")
        assert b"bad request" in body

    @pytest.mark.asyncio
    async def test_missing_prompt_field_returns_400(self) -> None:
        config = AgentConfig(provider=ProviderConfig(name="mock", model="mock-1"))
        app = create_asgi_app(config=config)
        driver = await _post(app, "/chat", json.dumps({"session_id": "abc"}).encode())
        start = next(m for m in driver.sent if m["type"] == "http.response.start")
        assert start["status"] == 400

    @pytest.mark.asyncio
    async def test_malformed_json_stream_returns_400(self) -> None:
        config = AgentConfig(provider=ProviderConfig(name="mock", model="mock-1"))
        app = create_asgi_app(config=config)
        driver = await _post(app, "/chat/stream", b"<<not json>>")
        start = next(m for m in driver.sent if m["type"] == "http.response.start")
        assert start["status"] == 400

    @pytest.mark.asyncio
    async def test_invalid_session_id_returns_400(self) -> None:
        """``validate_session_id`` rejects ``../path`` etc; must surface as 400."""
        config = AgentConfig(provider=ProviderConfig(name="mock", model="mock-1"))
        app = create_asgi_app(config=config)
        driver = await _post(
            app, "/chat", json.dumps({"prompt": "hi", "session_id": "../etc/passwd"}).encode()
        )
        start = next(m for m in driver.sent if m["type"] == "http.response.start")
        assert start["status"] == 400


# ---------------------------------------------------------------------------
# Concurrent no-session-id streams must not share an active-stream slot.
# ---------------------------------------------------------------------------


class TestNoSessionStreamIsolation:
    """#7 — two concurrent ``handle_stream`` calls without ``session_id``
    must get distinct ephemeral keys in ``_active_streams``."""

    @pytest.mark.asyncio
    async def test_two_concurrent_streams_get_distinct_keys(self) -> None:
        provider_a = MockProvider()
        provider_a.enqueue_text("alpha-done", stop="end_turn")
        provider_b = MockProvider()
        provider_b.enqueue_text("beta-done", stop="end_turn")

        # Each handle_stream call needs a fresh provider instance — patch
        # the agent's provider factory to alternate between them.
        providers = [provider_a, provider_b]

        def _factory(*_a, **_kw):
            return providers.pop(0)

        config = AgentConfig(provider=ProviderConfig(name="mock", model="mock-1"))

        with patch("agent.core.agent._create_provider", _factory):
            transport = WebTransport(config=config)

            it_a = await transport.handle_stream(prompt="alpha")
            it_b = await transport.handle_stream(prompt="beta")

            # The two iterators must carry distinct ephemeral session ids
            # AND the transport's internal active-streams dict must contain
            # both keys at the same time (no collision on "").
            assert it_a.session_id != it_b.session_id
            assert it_a.session_id in transport._active_streams
            assert it_b.session_id in transport._active_streams

            # Drain both iterators to completion so the background tasks
            # don't leak between tests.
            async def _drain(it):
                async for _ in it:
                    pass

            await asyncio.gather(_drain(it_a), _drain(it_b))


# ---------------------------------------------------------------------------
# SSE client disconnect must cancel the agent run.
# ---------------------------------------------------------------------------


class TestSseDisconnectCancelsRun:
    """#7 — closing the SSE connection must cancel ``run_agent`` promptly."""

    @pytest.mark.asyncio
    async def test_disconnect_cancels_iterator(self) -> None:
        """When the client sends ``http.disconnect``, the iterator and the
        underlying run task must be cancelled within ~1 s.
        """

        # Provider that blocks until cancelled. Simulates a long-running
        # streaming turn so we can verify mid-stream cancellation.
        cancelled = asyncio.Event()

        class _SlowProvider(MockProvider):
            async def complete(self, messages, tools=None, system=""):
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                return await super().complete(messages, tools, system)

        provider = _SlowProvider()
        provider.enqueue_text("never reached", stop="end_turn")
        config = AgentConfig(
            provider=ProviderConfig(name="mock", model="mock-1"),
            timeout=60.0,
        )

        with patch("agent.core.agent._create_provider", lambda *a, **kw: provider):
            app = create_asgi_app(config=config)

            driver = _AsgiDriver()
            body = json.dumps({"prompt": "go"}).encode()
            await driver.recv_queue.put({"type": "http.request", "body": body, "more_body": False})

            app_task = asyncio.create_task(
                app(_make_post_scope("/chat/stream"), driver.receive, driver.send)
            )

            # Wait until the response has started (so the disconnect watcher
            # is active and the agent has started running).
            try:
                await driver.wait_for_send(
                    lambda m: m["type"] == "http.response.start", timeout=2.0
                )
            except asyncio.TimeoutError:
                app_task.cancel()
                raise

            # Simulate client closing the connection.
            await driver.recv_queue.put({"type": "http.disconnect"})

            # The app must finish quickly now (cancellation propagated).
            try:
                await asyncio.wait_for(app_task, timeout=5.0)
            except asyncio.TimeoutError:
                app_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await app_task
                pytest.fail("ASGI handler did not exit within 5 s of http.disconnect")

            # And the provider call inside agent.run was cancelled.
            assert cancelled.is_set(), (
                "provider call was not cancelled by SSE disconnect — agent kept running"
            )
