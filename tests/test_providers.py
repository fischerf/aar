"""Provider normalization tests — compatibility matrix with mocks.

Tests that each provider adapter correctly normalizes:
- plain text responses
- tool calls
- reasoning / thinking blocks
- stop reasons
- malformed tool outputs

These use mocked HTTP responses, not live API calls.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.core.config import ProviderConfig
from agent.core.events import ReasoningBlock, StopReason, ToolCall
from agent.providers.base import Provider, ProviderCapabilities, ProviderResponse


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

        config = ProviderConfig(name="anthropic", model="claude-sonnet-4-20250514", api_key="test-key")
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
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tc_1", "content": "output"}
            ]}
        ]
        result = _build_messages(msgs, "")
        assert result[0]["role"] == "tool"
        assert result[0]["tool_call_id"] == "tc_1"

    def test_assistant_with_tool_use_blocks(self):
        from agent.providers.openai import _build_messages

        msgs = [
            {"role": "assistant", "content": [
                {"type": "text", "text": "Let me check"},
                {"type": "tool_use", "id": "tc_1", "name": "bash", "input": {"command": "ls"}},
            ]}
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
        """Without supports_reasoning, think tags should stay in content."""
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

        assert "<think>" in result.content
        assert result.reasoning == []

    def test_capabilities_tools_opt_out(self):
        provider = self._make_provider(supports_tools=False)
        caps = provider.capabilities()
        assert caps.tools is False

    def test_capabilities_reasoning_opt_in(self):
        provider = self._make_provider(supports_reasoning=True)
        caps = provider.capabilities()
        assert caps.reasoning is True


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
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tc_1", "content": "output"}
            ]}
        ]
        result = _build_messages(msgs, "")
        assert result[0]["role"] == "tool"

    def test_tool_schema_conversion(self):
        from agent.providers.ollama import _convert_tools

        tools = [{"name": "bash", "description": "Run shell", "input_schema": {"type": "object"}}]
        result = _convert_tools(tools)
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "bash"
