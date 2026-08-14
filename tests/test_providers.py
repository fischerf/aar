"""Provider normalization tests — compatibility matrix with mocks.

Tests that each provider adapter correctly normalizes:
- plain text responses
- tool calls
- reasoning / thinking blocks
- stop reasons
- malformed tool outputs

These use mocked HTTP responses, not live API calls.

Live tests (skipped by default) exercise the real APIs:
    pytest tests/test_providers.py -m live --live
Requires ANTHROPIC_API_KEY and/or OPENAI_API_KEY in the environment.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.core.config import ProviderConfig
from agent.core.events import StopReason
from agent.providers.base import ProviderCapabilities

# ---------------------------------------------------------------------------
# Base provider contract
# ---------------------------------------------------------------------------


class TestProviderCapabilities:
    def test_capabilities_dataclass(self):
        caps = ProviderCapabilities(
            name="test", tools=True, reasoning=True, streaming=False, structured_output=True
        )
        d = caps.to_dict()
        assert d["name"] == "test"
        assert d["tools"] is True
        assert d["reasoning"] is True
        assert d["streaming"] is False
        assert d["structured_output"] is True


# ---------------------------------------------------------------------------
# Anthropic provider normalization
# ---------------------------------------------------------------------------


class TestAnthropicNormalization:
    def _make_provider(self):
        """Create an Anthropic provider with a mocked client."""
        from agent.providers.anthropic import AnthropicProvider

        config = ProviderConfig(
            name="anthropic", model="claude-sonnet-4-20250514", api_key="test-key"
        )
        with patch("anthropic.AsyncAnthropic"):
            provider = AnthropicProvider(config)
        return provider

    @pytest.mark.asyncio
    async def test_plain_text_response(self):
        provider = self._make_provider()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="Hello!")]
        mock_response.stop_reason = "end_turn"
        mock_response.model = "claude-sonnet-4-20250514"
        mock_response.usage = MagicMock(input_tokens=10, output_tokens=5)
        mock_response.id = "msg_123"

        provider._client.messages.create = AsyncMock(return_value=mock_response)
        result = await provider.complete([{"role": "user", "content": "Hi"}])

        assert result.content == "Hello!"
        assert result.stop_reason == StopReason.END_TURN.value
        assert result.tool_calls == []
        assert result.meta.provider == "anthropic"
        assert result.meta.usage["input_tokens"] == 10

    @pytest.mark.asyncio
    async def test_tool_call_response(self):
        provider = self._make_provider()

        text_block = MagicMock(type="text", text="Let me check")
        tool_block = MagicMock(type="tool_use", id="toolu_123", input={"path": "test.py"})
        tool_block.name = "read_file"  # .name is special on MagicMock, must set explicitly
        mock_response = MagicMock()
        mock_response.content = [text_block, tool_block]
        mock_response.stop_reason = "tool_use"
        mock_response.model = "claude-sonnet-4-20250514"
        mock_response.usage = MagicMock(input_tokens=20, output_tokens=15)
        mock_response.id = "msg_456"

        provider._client.messages.create = AsyncMock(return_value=mock_response)
        result = await provider.complete(
            [{"role": "user", "content": "Read test.py"}],
            tools=[{"name": "read_file", "description": "Read a file", "input_schema": {}}],
        )

        assert result.content == "Let me check"
        assert result.stop_reason == StopReason.TOOL_USE.value
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].tool_name == "read_file"
        assert result.tool_calls[0].tool_call_id == "toolu_123"
        assert result.tool_calls[0].arguments == {"path": "test.py"}

    @pytest.mark.asyncio
    async def test_thinking_block(self):
        provider = self._make_provider()

        thinking_block = MagicMock(type="thinking", thinking="Let me reason about this...")
        text_block = MagicMock(type="text", text="The answer is 42")
        mock_response = MagicMock()
        mock_response.content = [thinking_block, text_block]
        mock_response.stop_reason = "end_turn"
        mock_response.model = "claude-sonnet-4-20250514"
        mock_response.usage = MagicMock(input_tokens=10, output_tokens=20)
        mock_response.id = "msg_789"

        provider._client.messages.create = AsyncMock(return_value=mock_response)
        result = await provider.complete([{"role": "user", "content": "Think hard"}])

        assert len(result.reasoning) == 1
        assert "reason about this" in result.reasoning[0].content
        assert result.content == "The answer is 42"

    @pytest.mark.asyncio
    async def test_stop_reason_mapping(self):
        provider = self._make_provider()

        for api_reason, expected in [
            ("end_turn", "end_turn"),
            ("tool_use", "tool_use"),
            ("max_tokens", "max_tokens"),
        ]:
            mock_response = MagicMock()
            mock_response.content = [MagicMock(type="text", text="x")]
            mock_response.stop_reason = api_reason
            mock_response.model = "claude-sonnet-4-20250514"
            mock_response.usage = MagicMock(input_tokens=1, output_tokens=1)
            mock_response.id = "msg"

            provider._client.messages.create = AsyncMock(return_value=mock_response)
            result = await provider.complete([{"role": "user", "content": "x"}])
            assert result.stop_reason == expected

    def test_capabilities(self):
        provider = self._make_provider()
        caps = provider.capabilities()
        assert caps.name == "anthropic"
        assert caps.tools is True
        assert caps.reasoning is True
        assert caps.streaming is True


# ---------------------------------------------------------------------------
# OpenAI provider normalization
# ---------------------------------------------------------------------------


class TestOpenAINormalization:
    def _make_provider(self):
        from agent.providers.openai import OpenAIProvider

        config = ProviderConfig(name="openai", model="gpt-4o", api_key="test-key")
        with patch("openai.AsyncOpenAI"):
            provider = OpenAIProvider(config)
        return provider

    def test_read_timeout_null_sets_unlimited(self):
        """read_timeout=null should produce an httpx.Timeout with read=None (unlimited)."""
        import httpx

        from agent.providers.openai import OpenAIProvider

        config = ProviderConfig(
            name="openai",
            model="gpt-4o-mini",
            api_key="key",
            extra={"read_timeout": None},
        )
        with patch("openai.AsyncOpenAI") as mock_cls:
            OpenAIProvider(config)
        _, kwargs = mock_cls.call_args
        timeout = kwargs.get("timeout")
        assert isinstance(timeout, httpx.Timeout)
        assert timeout.read is None
        assert timeout.connect == 10.0

    def test_read_timeout_value_sets_seconds(self):
        """read_timeout=120 should produce httpx.Timeout(read=120, connect=10)."""
        import httpx

        from agent.providers.openai import OpenAIProvider

        config = ProviderConfig(
            name="openai",
            model="gpt-4o-mini",
            api_key="key",
            extra={"read_timeout": 120},
        )
        with patch("openai.AsyncOpenAI") as mock_cls:
            OpenAIProvider(config)
        _, kwargs = mock_cls.call_args
        timeout = kwargs.get("timeout")
        assert isinstance(timeout, httpx.Timeout)
        assert timeout.read == 120.0

    def test_explicit_timeout_overrides_read_timeout(self):
        """extra.timeout takes precedence over extra.read_timeout."""
        from agent.providers.openai import OpenAIProvider

        config = ProviderConfig(
            name="openai",
            model="gpt-4o-mini",
            api_key="key",
            extra={"timeout": 30, "read_timeout": None},
        )
        with patch("openai.AsyncOpenAI") as mock_cls:
            OpenAIProvider(config)
        _, kwargs = mock_cls.call_args
        assert kwargs.get("timeout") == 30.0

    @pytest.mark.asyncio
    async def test_plain_text_response(self):
        provider = self._make_provider()

        mock_choice = MagicMock()
        mock_choice.message.content = "Hello from GPT!"
        mock_choice.message.tool_calls = None
        mock_choice.finish_reason = "stop"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.model = "gpt-4o"
        mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        mock_response.id = "chatcmpl-123"

        provider._client.chat.completions.create = AsyncMock(return_value=mock_response)
        result = await provider.complete([{"role": "user", "content": "Hi"}])

        assert result.content == "Hello from GPT!"
        assert result.stop_reason == StopReason.END_TURN.value
        assert result.tool_calls == []
        assert result.meta.provider == "openai"
        assert result.meta.usage["input_tokens"] == 10

    @pytest.mark.asyncio
    async def test_tool_call_response(self):
        provider = self._make_provider()

        mock_tc = MagicMock()
        mock_tc.id = "call_123"
        mock_tc.function.name = "read_file"
        mock_tc.function.arguments = '{"path": "test.py"}'

        mock_choice = MagicMock()
        mock_choice.message.content = None
        mock_choice.message.tool_calls = [mock_tc]
        mock_choice.finish_reason = "tool_calls"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.model = "gpt-4o"
        mock_response.usage = MagicMock(prompt_tokens=20, completion_tokens=10, total_tokens=30)
        mock_response.id = "chatcmpl-456"

        provider._client.chat.completions.create = AsyncMock(return_value=mock_response)
        result = await provider.complete(
            [{"role": "user", "content": "Read test.py"}],
            tools=[{"name": "read_file", "description": "Read", "input_schema": {}}],
        )

        assert result.stop_reason == StopReason.TOOL_USE.value
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].tool_name == "read_file"
        assert result.tool_calls[0].arguments == {"path": "test.py"}

    @pytest.mark.asyncio
    async def test_malformed_tool_arguments(self):
        """Malformed JSON in tool arguments should not crash."""
        provider = self._make_provider()

        mock_tc = MagicMock()
        mock_tc.id = "call_bad"
        mock_tc.function.name = "bash"
        mock_tc.function.arguments = "not valid json {{"

        mock_choice = MagicMock()
        mock_choice.message.content = None
        mock_choice.message.tool_calls = [mock_tc]
        mock_choice.finish_reason = "tool_calls"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.model = "gpt-4o"
        mock_response.usage = MagicMock(prompt_tokens=5, completion_tokens=5, total_tokens=10)
        mock_response.id = "chatcmpl-bad"

        provider._client.chat.completions.create = AsyncMock(return_value=mock_response)
        result = await provider.complete([{"role": "user", "content": "x"}])

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].arguments == {"raw": "not valid json {{"}

    @pytest.mark.asyncio
    async def test_stop_reason_mapping(self):
        provider = self._make_provider()

        for api_reason, expected in [
            ("stop", "end_turn"),
            ("tool_calls", "tool_use"),
            ("length", "max_tokens"),
        ]:
            mock_choice = MagicMock()
            mock_choice.message.content = "x"
            mock_choice.message.tool_calls = None
            mock_choice.finish_reason = api_reason

            mock_response = MagicMock()
            mock_response.choices = [mock_choice]
            mock_response.model = "gpt-4o"
            mock_response.usage = MagicMock(prompt_tokens=1, completion_tokens=1, total_tokens=2)
            mock_response.id = "msg"

            provider._client.chat.completions.create = AsyncMock(return_value=mock_response)
            result = await provider.complete([{"role": "user", "content": "x"}])
            assert result.stop_reason == expected


# ---------------------------------------------------------------------------
# OpenAI message conversion
# ---------------------------------------------------------------------------


class TestOpenAIMessageConversion:
    def test_system_prompt_prepended(self):
        from agent.providers.openai import _build_messages

        msgs = [{"role": "user", "content": "hi"}]
        result = _build_messages(msgs, "Be helpful")
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "Be helpful"
        assert result[1]["role"] == "user"

    def test_no_system_prompt(self):
        from agent.providers.openai import _build_messages

        msgs = [{"role": "user", "content": "hi"}]
        result = _build_messages(msgs, "")
        assert result[0]["role"] == "user"

    def test_tool_result_conversion(self):
        from agent.providers.openai import _build_messages

        msgs = [
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tc_1", "content": "output"}],
            }
        ]
        result = _build_messages(msgs, "")
        assert result[0]["role"] == "tool"
        assert result[0]["tool_call_id"] == "tc_1"

    def test_assistant_with_tool_use_blocks(self):
        from agent.providers.openai import _build_messages

        msgs = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me check"},
                    {"type": "tool_use", "id": "tc_1", "name": "bash", "input": {"command": "ls"}},
                ],
            }
        ]
        result = _build_messages(msgs, "")
        assert result[0]["role"] == "assistant"
        assert result[0]["tool_calls"][0]["function"]["name"] == "bash"


# ---------------------------------------------------------------------------
# Ollama provider normalization
# ---------------------------------------------------------------------------


class TestOllamaNormalization:
    def _make_provider(self, **extra):
        from agent.providers.ollama import OllamaProvider

        config = ProviderConfig(name="ollama", model="llama3", extra=extra)
        return OllamaProvider(config)

    @pytest.mark.asyncio
    async def test_plain_text_response(self):
        provider = self._make_provider()

        mock_json = {
            "model": "llama3",
            "message": {"role": "assistant", "content": "Hello from Ollama!"},
            "done_reason": "stop",
            "prompt_eval_count": 10,
            "eval_count": 5,
        }

        provider._client.post = AsyncMock(
            return_value=MagicMock(
                json=lambda: mock_json,
                raise_for_status=lambda: None,
            )
        )
        result = await provider.complete([{"role": "user", "content": "Hi"}])

        assert result.content == "Hello from Ollama!"
        assert result.stop_reason == StopReason.END_TURN.value
        assert result.meta.provider == "ollama"
        assert result.meta.usage["input_tokens"] == 10

    @pytest.mark.asyncio
    async def test_tool_call_response(self):
        provider = self._make_provider()

        mock_json = {
            "model": "llama3",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "read_file", "arguments": {"path": "test.py"}}}
                ],
            },
            "done_reason": "stop",
        }

        provider._client.post = AsyncMock(
            return_value=MagicMock(
                json=lambda: mock_json,
                raise_for_status=lambda: None,
            )
        )
        result = await provider.complete(
            [{"role": "user", "content": "Read test.py"}],
            tools=[{"name": "read_file", "description": "Read", "input_schema": {}}],
        )

        assert result.stop_reason == StopReason.TOOL_USE.value
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].tool_name == "read_file"
        assert result.tool_calls[0].arguments == {"path": "test.py"}

    @pytest.mark.asyncio
    async def test_think_mode_extraction(self):
        provider = self._make_provider(supports_reasoning=True)

        mock_json = {
            "model": "deepseek-r1",
            "message": {
                "role": "assistant",
                "content": "<think>Let me reason about this</think>The answer is 42",
            },
            "done_reason": "stop",
        }

        provider._client.post = AsyncMock(
            return_value=MagicMock(
                json=lambda: mock_json,
                raise_for_status=lambda: None,
            )
        )
        result = await provider.complete([{"role": "user", "content": "Think"}])

        assert result.content == "The answer is 42"
        assert len(result.reasoning) == 1
        assert "reason about this" in result.reasoning[0].content

    @pytest.mark.asyncio
    async def test_think_mode_disabled(self):
        """Think tags are always stripped from content (prevents token leakage).
        supports_reasoning only controls whether think=true is sent in the payload."""
        provider = self._make_provider(supports_reasoning=False)

        mock_json = {
            "model": "llama3",
            "message": {
                "role": "assistant",
                "content": "<think>thought</think>answer",
            },
            "done_reason": "stop",
        }

        provider._client.post = AsyncMock(
            return_value=MagicMock(
                json=lambda: mock_json,
                raise_for_status=lambda: None,
            )
        )
        result = await provider.complete([{"role": "user", "content": "x"}])

        # Tags are always extracted to prevent raw token leakage in ACP/TUI output
        assert result.content == "answer"
        assert len(result.reasoning) == 1
        assert "thought" in result.reasoning[0].content

    def test_capabilities_tools_opt_out(self):
        provider = self._make_provider(supports_tools=False)
        caps = provider.capabilities()
        assert caps.tools is False

    def test_capabilities_reasoning_opt_in(self):
        provider = self._make_provider(supports_reasoning=True)
        caps = provider.capabilities()
        assert caps.reasoning is True


# ---------------------------------------------------------------------------
# Gemini HTTP compatibility
# ---------------------------------------------------------------------------


class TestGeminiHttpCompatibility:
    def test_thought_signature_survives_tool_round_trip(self):
        from agent.core.session import Session
        from agent.providers.gemini import _build_contents, _parse_http_response

        signature = "c2lnbmF0dXJl"
        response = _parse_http_response(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "read_file",
                                        "args": {"path": "test.py"},
                                    },
                                    "thoughtSignature": signature,
                                }
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"totalTokenCount": 10},
                "modelVersion": "gemini-3.5-flash",
            },
            "fallback",
            {
                "x-request-id": "req-123",
                "x-quota-usage-percent": "7",
                "x-quota-reset-date": "2026-09-01T00:00:00Z",
            },
        )

        session = Session()
        session.add_user_message("Read test.py")
        session.append(response.tool_calls[0])
        session.add_assistant_message("", stop_reason=StopReason.TOOL_USE)
        session.add_tool_result(
            tool_call_id=response.tool_calls[0].tool_call_id,
            tool_name="read_file",
            output="contents",
        )

        contents = _build_contents(session.to_messages())
        function_part = contents[1]["parts"][0]
        assert function_part["thoughtSignature"] == signature
        assert response.meta is not None
        assert response.meta.request_id == "req-123"
        assert response.meta.data["response_headers"]["x-quota-usage-percent"] == "7"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "error_name"),
        [(400, "InvalidRequest"), (429, "RateLimited"), (503, "Transient")],
    )
    async def test_streaming_http_errors_retain_body_and_headers(self, status, error_name):
        import httpx

        from agent.providers.errors import InvalidRequest, RateLimited, Transient
        from agent.providers.gemini import _raise_for_status

        error_type = {
            "InvalidRequest": InvalidRequest,
            "RateLimited": RateLimited,
            "Transient": Transient,
        }[error_name]
        response = httpx.Response(
            status,
            headers={
                "content-type": "text/event-stream",
                "x-request-id": "req-error",
                "x-quota-usage-percent": "100",
                "x-quota-reset-date": "2026-09-01T00:00:00Z",
            },
            stream=httpx.ByteStream(b'{"error":"missing thought signature"}'),
            request=httpx.Request("POST", "https://example.test/gemini"),
        )

        with pytest.raises(error_type) as exc_info:
            await _raise_for_status(response)

        assert type(exc_info.value).__name__ == error_name
        detail = str(exc_info.value)
        assert "missing thought signature" in detail
        assert "x-request-id=req-error" in detail
        assert "x-quota-usage-percent=100" in detail


# ---------------------------------------------------------------------------
# Ollama message conversion
# ---------------------------------------------------------------------------


class TestOllamaMessageConversion:
    def test_system_prompt(self):
        from agent.providers.ollama import _build_messages

        msgs = [{"role": "user", "content": "hi"}]
        result = _build_messages(msgs, "Be helpful")
        assert result[0]["role"] == "system"

    def test_tool_result_conversion(self):
        from agent.providers.ollama import _build_messages

        msgs = [
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tc_1", "content": "output"}],
            }
        ]
        result = _build_messages(msgs, "")
        assert result[0]["role"] == "tool"

    def test_tool_schema_conversion(self):
        from agent.providers.ollama import _convert_tools

        tools = [{"name": "bash", "description": "Run shell", "input_schema": {"type": "object"}}]
        result = _convert_tools(tools)
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "bash"


# ---------------------------------------------------------------------------
# Live provider tests — skipped unless --live is passed
# ---------------------------------------------------------------------------

_PING_PROMPT = "Reply with exactly the word PONG and nothing else."

_ECHO_TOOL = {
    "name": "echo",
    "description": "Echo a message back",
    "input_schema": {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    },
}


@pytest.mark.live
class TestLiveAnthropic:
    """Integration tests against the real Anthropic Messages API.

    Requires:
        export ANTHROPIC_API_KEY=sk-ant-...

    Run with:
        pytest tests/test_providers.py -m live --live -k Anthropic
    """

    def _provider(self):
        import os

        from agent.providers.anthropic import AnthropicProvider

        return AnthropicProvider(
            ProviderConfig(
                name="anthropic",
                model="claude-haiku-4-5-20251001",
                api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            )
        )

    @pytest.mark.asyncio
    async def test_plain_text_response(self):
        provider = self._provider()
        result = await provider.complete([{"role": "user", "content": _PING_PROMPT}])
        assert result.content.strip()
        assert result.stop_reason
        assert result.meta is not None
        assert result.meta.usage.get("input_tokens", 0) > 0

    @pytest.mark.asyncio
    async def test_tool_call_response(self):
        provider = self._provider()
        result = await provider.complete(
            messages=[{"role": "user", "content": "Call echo with message='hello'"}],
            tools=[_ECHO_TOOL],
        )
        # The model should request a tool call
        assert len(result.tool_calls) > 0
        assert result.tool_calls[0].tool_name == "echo"

    @pytest.mark.asyncio
    async def test_stop_reasons_normalized(self):
        provider = self._provider()
        result = await provider.complete([{"role": "user", "content": _PING_PROMPT}])
        assert result.stop_reason in {"end_turn", "max_tokens", "tool_use"}

    @pytest.mark.asyncio
    async def test_provider_meta_populated(self):
        provider = self._provider()
        result = await provider.complete([{"role": "user", "content": _PING_PROMPT}])
        assert result.meta is not None
        assert result.meta.provider == "anthropic"
        assert result.meta.model


@pytest.mark.live
class TestLiveOpenAI:
    """Integration tests against the real OpenAI Chat Completions API.

    Requires:
        export OPENAI_API_KEY=sk-...

    Run with:
        pytest tests/test_providers.py -m live --live -k OpenAI
    """

    def _provider(self):
        import os

        from agent.providers.openai import OpenAIProvider

        return OpenAIProvider(
            ProviderConfig(
                name="openai",
                model="gpt-4o-mini",
                api_key=os.environ.get("OPENAI_API_KEY", ""),
            )
        )

    @pytest.mark.asyncio
    async def test_plain_text_response(self):
        provider = self._provider()
        result = await provider.complete([{"role": "user", "content": _PING_PROMPT}])
        assert result.content.strip()
        assert result.stop_reason
        assert result.meta is not None
        assert result.meta.usage.get("input_tokens", 0) > 0

    @pytest.mark.asyncio
    async def test_tool_call_response(self):
        provider = self._provider()
        result = await provider.complete(
            messages=[{"role": "user", "content": "Call echo with message='hello'"}],
            tools=[_ECHO_TOOL],
        )
        assert len(result.tool_calls) > 0
        assert result.tool_calls[0].tool_name == "echo"

    @pytest.mark.asyncio
    async def test_stop_reasons_normalized(self):
        provider = self._provider()
        result = await provider.complete([{"role": "user", "content": _PING_PROMPT}])
        assert result.stop_reason in {"end_turn", "max_tokens", "tool_use", "stop"}

    @pytest.mark.asyncio
    async def test_provider_meta_populated(self):
        provider = self._provider()
        result = await provider.complete([{"role": "user", "content": _PING_PROMPT}])
        assert result.meta is not None
        assert result.meta.provider == "openai"
        assert result.meta.model


# ---------------------------------------------------------------------------
# Provider streaming flush guarantees (#6)
# ---------------------------------------------------------------------------


class _AsyncLineIter:
    """Async iterator over a fixed list of lines (mimics httpx aiter_lines)."""

    def __init__(self, lines):
        self._lines = list(lines)
        self._idx = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._idx >= len(self._lines):
            raise StopAsyncIteration
        line = self._lines[self._idx]
        self._idx += 1
        return line


class _FakeStreamResp:
    def __init__(self, lines, status_code: int = 200):
        self.status_code = status_code
        self._lines = lines
        self.text = ""
        self.headers = {}

    async def aread(self):
        return b""

    def aiter_lines(self):
        return _AsyncLineIter(self._lines)


class _FakeStreamCM:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, exc_type, exc, tb):
        return False


class TestOllamaStreamFlushOnTruncation:
    """#6 — Ollama stream must flush tool calls even without a `done` frame.

    Without the fix the loop fell out of the `async with` block without
    emitting accumulated tool calls or a terminal `done=True` sentinel,
    leaving the consumer stuck.
    """

    def _make_provider(self):
        from agent.providers.ollama import OllamaProvider

        return OllamaProvider(ProviderConfig(name="ollama", model="llama3"))

    @pytest.mark.asyncio
    async def test_flushes_tool_calls_when_no_done_frame(self):
        import json as _json

        provider = self._make_provider()

        # Server emits text chunks + a tool_call chunk, then drops the
        # connection without ever sending {"done": true}.
        chunks = [
            _json.dumps({"model": "llama3", "message": {"content": "thinking..."}}),
            _json.dumps(
                {
                    "model": "llama3",
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "read_file",
                                    "arguments": {"path": "/tmp/x"},
                                }
                            }
                        ],
                    },
                }
            ),
        ]
        provider._client.stream = MagicMock(return_value=_FakeStreamCM(_FakeStreamResp(chunks)))

        deltas = [d async for d in provider.stream([{"role": "user", "content": "hi"}])]

        # Tool call must still surface despite no done frame
        tool_deltas = [d.tool_call_delta for d in deltas if d.tool_call_delta]
        assert len(tool_deltas) == 1
        assert tool_deltas[0]["tool_name"] == "read_file"
        # Exactly one terminal done sentinel
        assert sum(1 for d in deltas if d.done) == 1
        assert deltas[-1].done is True

    @pytest.mark.asyncio
    async def test_normal_done_frame_emits_once(self):
        """Regression guard: a well-formed stream must still emit exactly one done."""
        import json as _json

        provider = self._make_provider()

        chunks = [
            _json.dumps({"model": "llama3", "message": {"content": "hello"}}),
            _json.dumps(
                {
                    "model": "llama3",
                    "message": {"content": ""},
                    "done": True,
                    "prompt_eval_count": 5,
                    "eval_count": 2,
                }
            ),
        ]
        provider._client.stream = MagicMock(return_value=_FakeStreamCM(_FakeStreamResp(chunks)))

        deltas = [d async for d in provider.stream([{"role": "user", "content": "hi"}])]
        done_deltas = [d for d in deltas if d.done]
        assert len(done_deltas) == 1
        assert done_deltas[0].meta is not None
        assert done_deltas[0].meta.usage["input_tokens"] == 5


class TestOpenAIStreamEmitOnce:
    """#6 — OpenAI stream must emit accumulated tool calls EXACTLY ONCE.

    Some OpenAI-compatible backends (Azure, certain proxies) send an extra
    usage-only chunk after `finish_reason` arrives. Without the emit-once
    guard the loop would re-emit every tool call on that follow-up chunk.
    """

    def _make_provider(self):
        from agent.providers.openai import OpenAIProvider

        with patch("openai.AsyncOpenAI"):
            return OpenAIProvider(ProviderConfig(name="openai", model="gpt-4o", api_key="k"))

    @pytest.mark.asyncio
    async def test_extra_usage_chunk_after_finish_does_not_reemit(self):
        provider = self._make_provider()

        # Build minimal SDK-shaped chunks. Using MagicMock with the dotted
        # attribute access the provider expects.
        def _chunk(text=None, tool_name=None, args=None, finish=None, usage=None):
            choice = MagicMock()
            choice.finish_reason = finish
            delta = MagicMock()
            delta.content = text
            if tool_name is not None or args is not None:
                tc = MagicMock()
                tc.index = 0
                tc.id = "tc_1"
                tc.function.name = tool_name
                tc.function.arguments = args
                delta.tool_calls = [tc]
            else:
                delta.tool_calls = None
            choice.delta = delta
            chunk = MagicMock()
            chunk.choices = [choice]
            if usage is not None:
                u = MagicMock()
                u.prompt_tokens = usage[0]
                u.completion_tokens = usage[1]
                u.total_tokens = usage[0] + usage[1]
                chunk.usage = u
            else:
                chunk.usage = None
            return chunk

        chunks = [
            _chunk(tool_name="read_file", args='{"path":"/x"}'),
            _chunk(finish="tool_calls"),
            # Extra usage-only chunk AFTER finish_reason (Azure/proxy quirk).
            # finish_reason set again here would re-trigger emission without
            # the emitted_tools guard.
            _chunk(finish="tool_calls", usage=(10, 3)),
        ]

        async def _aiter(self):
            for c in chunks:
                yield c

        stream_resp = MagicMock()
        stream_resp.__aiter__ = _aiter

        provider._client.chat.completions.create = AsyncMock(return_value=stream_resp)

        deltas = [d async for d in provider.stream([{"role": "user", "content": "hi"}])]

        tool_deltas = [d.tool_call_delta for d in deltas if d.tool_call_delta]
        assert len(tool_deltas) == 1, f"tool_call emitted {len(tool_deltas)} times, expected 1"
        assert tool_deltas[0]["tool_name"] == "read_file"
        # One terminal done with meta from the trailing usage chunk
        done_deltas = [d for d in deltas if d.done]
        assert len(done_deltas) == 1
        assert done_deltas[0].meta is not None
        assert done_deltas[0].meta.usage["input_tokens"] == 10


# ---------------------------------------------------------------------------
# Base Provider.stream() fallback — covers providers that don't implement
# native streaming and rely on the default in agent/providers/base.py.
# ---------------------------------------------------------------------------


class TestProviderStreamFallback:
    """The default ``Provider.stream()`` replays a ``complete()`` response as deltas.

    The previous implementation yielded only one ``StreamDelta(text=..., done=True)``
    and silently dropped tool calls, reasoning, and provider metadata. The fix
    replays each piece as its own delta so the stream consumer assembles a
    response indistinguishable from a native stream.
    """

    def _provider(self, response):
        from agent.providers.base import Provider

        class _FakeProvider(Provider):
            @property
            def name(self) -> str:
                return "fake"

            async def complete(self, messages, tools=None, system=""):
                return response

        return _FakeProvider(ProviderConfig(name="fake", model="m"))

    @pytest.mark.asyncio
    async def test_fallback_emits_text_and_terminal_done(self):
        from agent.core.events import ProviderMeta
        from agent.providers.base import ProviderResponse

        response = ProviderResponse(
            content="hello world",
            stop_reason="end_turn",
            meta=ProviderMeta(provider="fake", model="m"),
        )
        provider = self._provider(response)

        deltas = [d async for d in provider.stream([{"role": "user", "content": "hi"}])]

        texts = [d.text for d in deltas if d.text]
        assert "".join(texts) == "hello world"
        assert deltas[-1].done is True
        assert deltas[-1].meta is not None
        assert deltas[-1].meta.provider == "fake"
        # Exactly one terminal delta
        assert sum(1 for d in deltas if d.done) == 1

    @pytest.mark.asyncio
    async def test_fallback_preserves_tool_calls(self):
        """Without the fix tool calls vanish — they must survive the fallback."""
        from agent.core.events import ProviderMeta, ToolCall
        from agent.providers.base import ProviderResponse

        response = ProviderResponse(
            content="",
            tool_calls=[
                ToolCall(
                    tool_name="read_file",
                    tool_call_id="tc_1",
                    arguments={"path": "/tmp/x"},
                )
            ],
            stop_reason="tool_use",
            meta=ProviderMeta(provider="fake", model="m"),
        )
        provider = self._provider(response)

        deltas = [d async for d in provider.stream([{"role": "user", "content": "hi"}])]
        tool_deltas = [d.tool_call_delta for d in deltas if d.tool_call_delta]
        assert len(tool_deltas) == 1
        assert tool_deltas[0]["tool_name"] == "read_file"
        assert tool_deltas[0]["tool_call_id"] == "tc_1"
        assert tool_deltas[0]["arguments"] == {"path": "/tmp/x"}

    @pytest.mark.asyncio
    async def test_fallback_preserves_reasoning(self):
        from agent.core.events import ProviderMeta, ReasoningBlock
        from agent.providers.base import ProviderResponse

        response = ProviderResponse(
            content="answer",
            reasoning=[ReasoningBlock(content="first thought"), ReasoningBlock(content="second")],
            stop_reason="end_turn",
            meta=ProviderMeta(provider="fake", model="m"),
        )
        provider = self._provider(response)

        deltas = [d async for d in provider.stream([{"role": "user", "content": "hi"}])]
        reasoning = "".join(d.reasoning_delta for d in deltas if d.reasoning_delta)
        assert "first thought" in reasoning
        assert "second" in reasoning

    @pytest.mark.asyncio
    async def test_fallback_roundtrip_through_consume_stream(self):
        """End-to-end: run the fallback through the loop's stream consumer
        and confirm the assembled ProviderResponse matches complete()."""
        from agent.core.events import ProviderMeta, ToolCall
        from agent.core.provider_runner import _consume_stream
        from agent.core.session import Session
        from agent.providers.base import ProviderResponse

        response = ProviderResponse(
            content="hi there",
            tool_calls=[ToolCall(tool_name="search", tool_call_id="tc_42", arguments={"q": "x"})],
            stop_reason="tool_use",
            meta=ProviderMeta(provider="fake", model="m"),
        )
        provider = self._provider(response)
        session = Session()

        events = []

        def on_event(ev):
            events.append(ev)

        assembled = await _consume_stream(
            provider=provider,
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            system="",
            session=session,
            on_event=on_event,
        )

        assert assembled.content == "hi there"
        assert len(assembled.tool_calls) == 1
        assert assembled.tool_calls[0].tool_name == "search"
        assert assembled.tool_calls[0].tool_call_id == "tc_42"
        assert assembled.stop_reason == "tool_use"
        assert assembled.meta is not None and assembled.meta.provider == "fake"


# ---------------------------------------------------------------------------
# _thinking helpers
# ---------------------------------------------------------------------------


class TestExtractThinkTags:
    def test_basic(self):
        from agent.providers._thinking import extract_think_tags

        clean, blocks = extract_think_tags("<think>reason here</think>answer")
        assert clean == "answer"
        assert len(blocks) == 1
        assert blocks[0].content == "reason here"

    def test_no_tags(self):
        from agent.providers._thinking import extract_think_tags

        clean, blocks = extract_think_tags("plain answer")
        assert clean == "plain answer"
        assert blocks == []

    def test_unclosed_tag(self):
        from agent.providers._thinking import extract_think_tags

        clean, blocks = extract_think_tags("<think>unclosed reasoning")
        assert clean == ""
        assert len(blocks) == 1
        assert "unclosed reasoning" in blocks[0].content

    def test_multiple_blocks(self):
        from agent.providers._thinking import extract_think_tags

        clean, blocks = extract_think_tags("<think>a</think>text1<think>b</think>text2")
        assert "text1" in clean
        assert "text2" in clean
        assert len(blocks) == 2

    def test_empty_think_block_ignored(self):
        from agent.providers._thinking import extract_think_tags

        clean, blocks = extract_think_tags("<think></think>answer")
        assert clean == "answer"
        assert blocks == []


class TestExtractChannelTokens:
    def test_basic(self):
        from agent.providers._thinking import extract_channel_tokens

        clean, blocks = extract_channel_tokens(
            "<|channel>thought\nsome reasoning\n<channel|>answer"
        )
        assert clean == "answer"
        assert len(blocks) == 1
        assert "some reasoning" in blocks[0].content

    def test_empty_block_dropped(self):
        from agent.providers._thinking import extract_channel_tokens

        # Gemma4 with thinking disabled emits an empty block — should be silently dropped
        clean, blocks = extract_channel_tokens("<|channel>thought\n<channel|>answer")
        assert clean == "answer"
        assert blocks == []

    def test_unclosed_block(self):
        from agent.providers._thinking import extract_channel_tokens

        clean, blocks = extract_channel_tokens("<|channel>thought\nunclosed reasoning")
        assert clean == ""
        assert len(blocks) == 1
        assert "unclosed reasoning" in blocks[0].content

    def test_no_tokens(self):
        from agent.providers._thinking import extract_channel_tokens

        clean, blocks = extract_channel_tokens("plain text")
        assert clean == "plain text"
        assert blocks == []


class TestExtractAll:
    def test_channel_then_think(self):
        from agent.providers._thinking import extract_all

        content = "<|channel>thought\ngemma thought\n<channel|><think>qwen thought</think>answer"
        clean, blocks = extract_all(content)
        assert clean == "answer"
        assert len(blocks) == 2

    def test_passthrough(self):
        from agent.providers._thinking import extract_all

        clean, blocks = extract_all("plain")
        assert clean == "plain"
        assert blocks == []


class TestExtractReasoningContent:
    def test_reasoning_content_str(self):
        from agent.providers._thinking import extract_reasoning_content

        msg = MagicMock()
        msg.reasoning_content = "  deep thought  "
        del msg.reasoning_details  # ensure AttributeError → hasattr returns False
        blocks = extract_reasoning_content(msg)
        assert len(blocks) == 1
        assert blocks[0].content == "deep thought"

    def test_reasoning_details_list(self):
        from agent.providers._thinking import extract_reasoning_content

        item = MagicMock()
        item.summary = "summarized thought"
        item.text = None
        msg = MagicMock()
        msg.reasoning_details = [item]
        blocks = extract_reasoning_content(msg)
        assert len(blocks) == 1
        assert "summarized thought" in blocks[0].content

    def test_dict_reasoning_content(self):
        from agent.providers._thinking import extract_reasoning_content

        blocks = extract_reasoning_content({"reasoning_content": "dict thought"})
        assert len(blocks) == 1
        assert "dict thought" in blocks[0].content

    def test_empty_message(self):
        from agent.providers._thinking import extract_reasoning_content

        blocks = extract_reasoning_content({})
        assert blocks == []


class TestStreamThinkingRouter:
    def test_no_thinking(self):
        from agent.providers._thinking import StreamThinkingRouter

        router = StreamThinkingRouter()
        clean, reasoning = router.feed("hello world")
        assert clean == "hello world"
        assert reasoning == ""

    def test_think_tag_in_single_chunk(self):
        from agent.providers._thinking import StreamThinkingRouter

        router = StreamThinkingRouter()
        clean, reasoning = router.feed("<think>reason</think>answer")
        assert clean == "answer"
        assert reasoning == "reason"

    def test_think_tag_split_across_chunks(self):
        from agent.providers._thinking import StreamThinkingRouter

        router = StreamThinkingRouter()
        c1, r1 = router.feed("<thi")
        assert c1 == "" and r1 == ""  # partial — buffered
        c2, r2 = router.feed("nk>reason</think>answer")
        assert c2 == "answer"
        assert r2 == "reason"

    def test_channel_token_in_single_chunk(self):
        from agent.providers._thinking import StreamThinkingRouter

        router = StreamThinkingRouter()
        clean, reasoning = router.feed("<|channel>thought\ngemma reason\n<channel|>answer")
        assert clean == "answer"
        assert "gemma reason" in reasoning

    def test_channel_token_split_across_chunks(self):
        from agent.providers._thinking import StreamThinkingRouter

        router = StreamThinkingRouter()
        c1, r1 = router.feed("<|channel>thought\n")
        assert c1 == "" and r1 == ""
        c2, r2 = router.feed("thinking text<channel|>")
        assert c2 == ""
        assert "thinking text" in r2
        c3, r3 = router.feed("answer")
        assert c3 == "answer" and r3 == ""

    def test_flush_unclosed_thinking(self):
        from agent.providers._thinking import StreamThinkingRouter

        # Reasoning content is emitted immediately by feed(), not buffered for flush().
        # An unclosed <think> block: content inside is returned by feed() as reasoning_delta.
        router = StreamThinkingRouter()
        clean, reasoning = router.feed("<think>partial content")
        assert clean == ""
        assert "partial content" in reasoning
        # flush() has nothing extra to drain (no partial opener buffer)
        clean2, reasoning2 = router.flush()
        assert clean2 == "" and reasoning2 == ""

    def test_flush_partial_opener_buffer(self):
        from agent.providers._thinking import StreamThinkingRouter

        router = StreamThinkingRouter()
        router.feed("<thi")  # buffered as potential opener
        clean, reasoning = router.flush()
        assert "<thi" in clean
        assert reasoning == ""

    def test_empty_chunk(self):
        from agent.providers._thinking import StreamThinkingRouter

        router = StreamThinkingRouter()
        clean, reasoning = router.feed("")
        assert clean == "" and reasoning == ""


class TestProviderErrorTaxonomy:
    """#4 — ``agent.providers.errors`` taxonomy + ``translate_provider_errors``.

    The runner now decides retry / messaging via ``isinstance`` against
    typed errors raised by each adapter. The classifier in ``errors.py``
    centralises the SDK-name table that used to be duplicated in
    ``provider_runner.py``.
    """

    # ---- classifier -----------------------------------------------------

    def test_classify_rate_limit_by_name(self):
        from agent.providers.errors import RateLimited, classify_provider_exception

        class FakeSdkRateLimitError(Exception):
            pass

        result = classify_provider_exception(FakeSdkRateLimitError("slow down"))
        assert isinstance(result, RateLimited)
        assert "slow down" in str(result)

    def test_classify_auth_failure_by_name(self):
        from agent.providers.errors import AuthFailure, classify_provider_exception

        class FakePermissionDeniedError(Exception):
            pass

        result = classify_provider_exception(FakePermissionDeniedError("nope"))
        assert isinstance(result, AuthFailure)

    def test_classify_transient_by_name(self):
        from agent.providers.errors import Transient, classify_provider_exception

        class FakeReadTimeout(Exception):
            pass

        result = classify_provider_exception(FakeReadTimeout("too slow"))
        assert isinstance(result, Transient)

    def test_classify_invalid_request_by_name(self):
        from agent.providers.errors import InvalidRequest, classify_provider_exception

        class FakeBadRequestError(Exception):
            pass

        result = classify_provider_exception(FakeBadRequestError("bad"))
        assert isinstance(result, InvalidRequest)

    def test_classify_unknown_returns_none(self):
        """Exceptions outside the known families must NOT be wrapped; the caller
        re-raises the original so we don't hide novel error types."""
        from agent.providers.errors import classify_provider_exception

        class Mystery(Exception):
            pass

        assert classify_provider_exception(Mystery("???")) is None

    def test_classify_passes_through_already_typed(self):
        """An already-typed ProviderError must not be double-wrapped."""
        from agent.providers.errors import RateLimited, classify_provider_exception

        original = RateLimited("hit")
        result = classify_provider_exception(original)
        assert result is original

    def test_retryable_attribute(self):
        """The ``retryable`` flag is part of the taxonomy contract —
        rate-limits and transient errors are retryable, auth and invalid
        requests are not."""
        from agent.providers.errors import (
            AuthFailure,
            InvalidRequest,
            RateLimited,
            Transient,
        )

        assert RateLimited.retryable is True
        assert Transient.retryable is True
        assert AuthFailure.retryable is False
        assert InvalidRequest.retryable is False

    # ---- translate_sdk_errors context manager ---------------------------

    @pytest.mark.asyncio
    async def test_translate_sdk_errors_wraps_sdk_exception(self):
        from agent.providers.errors import RateLimited, translate_sdk_errors

        class FakeRateLimitError(Exception):
            pass

        with pytest.raises(RateLimited) as exc_info:
            async with translate_sdk_errors():
                raise FakeRateLimitError("throttled")
        # __cause__ preserves the original for debugging.
        assert isinstance(exc_info.value.__cause__, FakeRateLimitError)

    @pytest.mark.asyncio
    async def test_translate_sdk_errors_lets_unknown_propagate(self):
        from agent.providers.errors import translate_sdk_errors

        class Mystery(Exception):
            pass

        with pytest.raises(Mystery):
            async with translate_sdk_errors():
                raise Mystery("raw")

    @pytest.mark.asyncio
    async def test_translate_sdk_errors_passes_typed_through(self):
        from agent.providers.errors import AuthFailure, translate_sdk_errors

        original = AuthFailure("bad key")
        with pytest.raises(AuthFailure) as exc_info:
            async with translate_sdk_errors():
                raise original
        # Same instance — not re-wrapped, no spurious __cause__.
        assert exc_info.value is original

    @pytest.mark.asyncio
    async def test_translate_sdk_errors_does_not_swallow_cancellation(self):
        """``CancelledError`` is a ``BaseException`` — the context manager
        must NOT classify it as a provider error or cooperative cancellation
        in the loop will break."""
        import asyncio

        from agent.providers.errors import translate_sdk_errors

        with pytest.raises(asyncio.CancelledError):
            async with translate_sdk_errors():
                raise asyncio.CancelledError()

    # ---- @translate_provider_errors decorator ---------------------------

    @pytest.mark.asyncio
    async def test_decorator_wraps_coroutine_complete(self):
        from agent.providers.errors import Transient, translate_provider_errors

        class FakeConnectError(Exception):
            pass

        @translate_provider_errors
        async def fake_complete(self):
            raise FakeConnectError("down")

        with pytest.raises(Transient):
            await fake_complete(self=None)

    @pytest.mark.asyncio
    async def test_decorator_wraps_async_generator_stream(self):
        """The decorator must auto-detect async generators (``yield`` inside)
        and wrap them WITHOUT collapsing the generator into a single value."""
        from agent.providers.errors import RateLimited, translate_provider_errors

        class FakeRateLimitError(Exception):
            pass

        @translate_provider_errors
        async def fake_stream(self):
            yield "a"
            yield "b"
            raise FakeRateLimitError("slow down")

        seen: list[str] = []
        with pytest.raises(RateLimited):
            async for x in fake_stream(self=None):
                seen.append(x)
        # Both pre-error yields surfaced; the error only fires on the third pull.
        assert seen == ["a", "b"]

    @pytest.mark.asyncio
    async def test_decorator_passes_through_successful_stream(self):
        """Regression: success path — every yielded value reaches the consumer
        and the generator exits cleanly."""
        from agent.providers.errors import translate_provider_errors

        @translate_provider_errors
        async def fake_stream(self):
            for i in range(3):
                yield i

        result = []
        async for v in fake_stream(self=None):
            result.append(v)
        assert result == [0, 1, 2]

    # ---- runner uses isinstance ----------------------------------------

    def test_runner_is_rate_limit_via_typed_error(self):
        """`_is_rate_limit` returns True for our typed RateLimited without
        falling back to substring matching."""
        from agent.core.provider_runner import _is_rate_limit
        from agent.providers.errors import RateLimited

        assert _is_rate_limit(RateLimited("hit"))

    def test_runner_is_rate_limit_via_legacy_name(self):
        """Backwards-compat: an unwrapped SDK exception (e.g. raised by a
        custom Provider subclass that didn't use the decorator) is still
        classified correctly via the centralised name table."""
        from agent.core.provider_runner import _is_rate_limit

        class FakeRateLimitError(Exception):
            pass

        assert _is_rate_limit(FakeRateLimitError("slow"))

    def test_runner_message_for_auth_failure(self):
        from agent.core.provider_runner import _provider_error_message
        from agent.providers.errors import AuthFailure

        msg, recoverable = _provider_error_message(AuthFailure("bad key"))
        assert "Authentication failed" in msg
        assert recoverable is False

    def test_runner_message_for_transient(self):
        from agent.core.provider_runner import _provider_error_message
        from agent.providers.errors import Transient

        msg, recoverable = _provider_error_message(Transient("oops"))
        assert recoverable is True

    def test_runner_message_for_unknown(self):
        """Mystery exceptions keep the legacy ``Provider error (Name): ...`` shape
        so logs and TUIs that already parse it don't break."""
        from agent.core.provider_runner import _provider_error_message

        class Mystery(Exception):
            pass

        msg, recoverable = _provider_error_message(Mystery("???"))
        assert "Mystery" in msg
        assert recoverable is False
