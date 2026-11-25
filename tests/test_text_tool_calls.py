"""Tool calls that a model (Mistral without [TOOL_CALLS]) wrote as plain text."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.core.config import ProviderConfig
from agent.providers._text_tool_calls import may_be_text_tool_call, parse_text_tool_calls
from agent.providers.ollama import OllamaProvider

TOOLS = {"list_directory", "read_file"}
TOOL_SCHEMAS = [
    {"name": "list_directory", "description": "", "input_schema": {"type": "object", "properties": {}}},
    {"name": "read_file", "description": "", "input_schema": {"type": "object", "properties": {}}},
]


class TestParse:
    def test_single_call_without_marker(self):
        text = 'list_directory[ARGS]{"path": "C:\\\\Users\\\\x\\\\Downloads"}'
        assert parse_text_tool_calls(text, TOOLS) == [
            {"function": {"name": "list_directory", "arguments": {"path": "C:\\Users\\x\\Downloads"}}}
        ]

    def test_marker_whitespace_and_several_calls(self):
        text = ' [TOOL_CALLS]list_directory[ARGS]{"path": "a"}\n[TOOL_CALLS]read_file[ARGS]{"path": "b"} '
        calls = parse_text_tool_calls(text, TOOLS)
        assert [c["function"]["name"] for c in calls] == ["list_directory", "read_file"]

    @pytest.mark.parametrize(
        "text",
        [
            "Hier sind die Dateien.",
            'unknown_tool[ARGS]{"a": 1}',
            'list_directory[ARGS]{"path": "a"} und noch Text',
            'Ich rufe list_directory[ARGS]{"path": "a"} auf',
            'list_directory[ARGS]["a"]',
            'list_directory[ARGS]{"path": ',
            "",
        ],
    )
    def test_anything_else_is_left_alone(self, text):
        assert parse_text_tool_calls(text, TOOLS) is None

    def test_no_tools_offered(self):
        assert parse_text_tool_calls('list_directory[ARGS]{"path": "a"}', set()) is None


class TestMayBe:
    @pytest.mark.parametrize(
        "text",
        ["", "  ", "[TOOL", "[TOOL_CALLS]", "list_dir", "list_directory", "list_directory[AR", 'read_file[ARGS]{"pa'],
    )
    def test_held(self, text):
        assert may_be_text_tool_call(text, TOOLS)

    @pytest.mark.parametrize("text", ["Hier", "Ich ", "list_directoryX[", "listen", "read_file sagt", "1."])
    def test_released(self, text):
        assert not may_be_text_tool_call(text, TOOLS)


def _stream_provider(frames: list[dict]) -> OllamaProvider:
    provider = OllamaProvider(ProviderConfig(name="ollama", model="ministral-3:14b"))
    lines = [json.dumps(f) for f in frames]

    class _Lines:
        def __init__(self):
            self._it = iter(lines)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration from None

    resp = MagicMock(status_code=200)
    resp.aiter_lines = lambda: _Lines()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    provider._client.stream = MagicMock(return_value=cm)
    return provider


def _chunks(*texts: str) -> list[dict]:
    frames = [{"model": "m", "message": {"content": t}} for t in texts]
    return frames + [{"model": "m", "message": {"content": ""}, "done": True, "done_reason": "stop"}]


async def _collect(provider, tools=TOOL_SCHEMAS):
    return [d async for d in provider.stream([{"role": "user", "content": "hi"}], tools=tools)]


class TestOllamaStream:
    @pytest.mark.asyncio
    async def test_text_tool_call_becomes_a_tool_call(self):
        deltas = await _collect(_stream_provider(_chunks("list_", "directory[ARGS]", '{"path": "C:/x"}')))
        assert "".join(d.text for d in deltas if d.text) == ""
        calls = [d.tool_call_delta for d in deltas if d.tool_call_delta]
        assert [(c["tool_name"], c["arguments"]) for c in calls] == [("list_directory", {"path": "C:/x"})]
        assert deltas[-1].stop_reason == "tool_use"

    @pytest.mark.asyncio
    async def test_normal_answer_streams_unchanged(self):
        deltas = await _collect(_stream_provider(_chunks("Hier ", "sind ", "die Dateien.")))
        assert "".join(d.text for d in deltas if d.text) == "Hier sind die Dateien."
        assert not any(d.tool_call_delta for d in deltas)
        # Released at the first chunk that cannot start a call: no buffering of the whole answer.
        assert deltas[0].text == "Hier "

    @pytest.mark.asyncio
    async def test_held_prefix_that_turns_out_to_be_text_is_not_lost(self):
        deltas = await _collect(_stream_provider(_chunks("list", "en wir die Dateien auf.")))
        assert "".join(d.text for d in deltas if d.text) == "listen wir die Dateien auf."

    @pytest.mark.asyncio
    async def test_broken_call_text_is_kept(self):
        deltas = await _collect(_stream_provider(_chunks('read_file[ARGS]{"path": ')))
        assert "".join(d.text for d in deltas if d.text) == 'read_file[ARGS]{"path": '
        assert not any(d.tool_call_delta for d in deltas)

    @pytest.mark.asyncio
    async def test_without_tools_nothing_is_held(self):
        deltas = await _collect(_stream_provider(_chunks('list_directory[ARGS]{"path": "a"}')), tools=None)
        assert "".join(d.text for d in deltas if d.text) == 'list_directory[ARGS]{"path": "a"}'


class TestOllamaComplete:
    @pytest.mark.asyncio
    async def test_text_tool_call_becomes_a_tool_call(self):
        provider = OllamaProvider(ProviderConfig(name="ollama", model="ministral-3:14b"))
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(
            return_value={
                "model": "ministral-3:14b",
                "message": {"content": 'list_directory[ARGS]{"path": "C:/x"}'},
                "done_reason": "stop",
            }
        )
        provider._client.post = AsyncMock(return_value=resp)

        result = await provider.complete([{"role": "user", "content": "hi"}], tools=TOOL_SCHEMAS)

        assert result.content == ""
        assert [(c.tool_name, c.arguments) for c in result.tool_calls] == [("list_directory", {"path": "C:/x"})]
        assert result.stop_reason == "tool_use"
