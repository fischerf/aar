"""Images returned by tools (e.g. an MCP ``screenshot``) reaching vision models.

Covers: ToolOutput handling and size/type guard in the executor, the session
message format, provider serialisation (Anthropic, OpenAI, generic, Ollama,
Gemini), vision gating and history pruning in the loop, JSONL persistence,
token estimates, the MCP bridge and the ACP tool-call card.
"""

from __future__ import annotations

import base64
import sys
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent.core.config import AgentConfig, ProviderConfig, SafetyConfig, ToolConfig
from agent.core.events import AssistantMessage, ImageURLBlock, ToolCall, ToolResult, UserMessage
from agent.core.loop import run_loop
from agent.core.multimodal import (
    base64_size,
    check_tool_image,
    image_block_from_base64,
    split_data_uri,
    tool_images_followup_message,
)
from agent.core.session import (
    Session,
    estimate_token_count,
    events_to_messages,
    prune_tool_result_images,
    strip_tool_result_images,
)
from agent.tools.execution import ToolExecutor
from agent.tools.registry import ToolRegistry
from agent.tools.schema import ToolOutput, ToolSpec
from tests.conftest import MockProvider

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")
PNG_URI = f"data:image/png;base64,{PNG_B64}"


def _img(b64: str = PNG_B64, mime: str = "image/png") -> ImageURLBlock:
    return image_block_from_base64(b64, mime)


def _tool_turn(images: list[ImageURLBlock] | None = None) -> list[dict[str, Any]]:
    """Internal messages for: user ask → assistant tool_use → tool_result."""
    events = [
        UserMessage(content="take a screenshot"),
        ToolCall(tool_name="screenshot", tool_call_id="tc1", arguments={}),
        AssistantMessage(content=""),
        ToolResult(
            tool_call_id="tc1",
            tool_name="screenshot",
            output="[image: image/png, 72 B]\nScreenshot of the screen (8x8).",
            images=images if images is not None else [_img()],
        ),
    ]
    return events_to_messages(events)


# ---------------------------------------------------------------------------
# multimodal helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_split_data_uri(self):
        assert split_data_uri(PNG_URI) == ("image/png", PNG_B64)
        assert split_data_uri("https://example.com/a.png") is None
        assert split_data_uri("data:image/png,rawtext") is None

    def test_base64_size(self):
        assert base64_size(PNG_B64) == len(PNG_BYTES)

    def test_check_tool_image(self):
        assert check_tool_image(_img(), max_bytes=10_000) is None
        assert "exceeds" in check_tool_image(_img(), max_bytes=10)
        assert "unsupported image type image/tiff" == check_tool_image(
            _img(mime="image/tiff"), max_bytes=10_000
        )
        url_block = ImageURLBlock.model_validate(
            {"type": "image_url", "image_url": {"url": "https://x/y.png"}}
        )
        assert check_tool_image(url_block, 10_000) == "not an inline base64 image"

    def test_followup_message(self):
        msg = tool_images_followup_message(
            [
                {"type": "tool_result", "tool_use_id": "a", "content": "x"},
                {
                    "type": "tool_result",
                    "tool_use_id": "b",
                    "content": "y",
                    "images": [{"type": "image_url", "image_url": {"url": PNG_URI}}],
                },
            ]
        )
        assert msg["role"] == "user"
        assert msg["content"][0]["type"] == "text" and "b" in msg["content"][0]["text"]
        assert msg["content"][1] == {"type": "image_url", "image_url": {"url": PNG_URI}}
        assert tool_images_followup_message([{"type": "tool_result", "content": ""}]) is None


# ---------------------------------------------------------------------------
# executor
# ---------------------------------------------------------------------------


def _registry_with(handler) -> ToolRegistry:
    reg = ToolRegistry()
    reg.add(
        ToolSpec(
            name="screenshot",
            description="Take a screenshot",
            input_schema={"type": "object", "properties": {}},
            handler=handler,
        )
    )
    return reg


class TestExecutor:
    async def test_tool_output_images_land_on_result(self):
        async def shot() -> ToolOutput:
            return ToolOutput(text="[image: image/png]", images=[_img()])

        ex = ToolExecutor(_registry_with(shot), ToolConfig(), SafetyConfig())
        [tr] = await ex.execute([ToolCall(tool_name="screenshot", tool_call_id="t", arguments={})])
        assert not tr.is_error
        assert tr.output == "[image: image/png]"
        assert tr.images == [_img()]

    async def test_plain_string_has_no_images(self):
        ex = ToolExecutor(_registry_with(lambda: "hello"), ToolConfig(), SafetyConfig())
        [tr] = await ex.execute([ToolCall(tool_name="screenshot", tool_call_id="t", arguments={})])
        assert tr.output == "hello" and tr.images == []

    async def test_oversized_and_unsupported_images_dropped_with_note(self):
        async def shot() -> ToolOutput:
            return ToolOutput(text="pics", images=[_img(), _img(mime="image/tiff")])

        cfg = ToolConfig(max_image_bytes=10)
        ex = ToolExecutor(_registry_with(shot), cfg, SafetyConfig())
        [tr] = await ex.execute([ToolCall(tool_name="screenshot", tool_call_id="t", arguments={})])
        assert tr.images == []
        assert "[image omitted: 72 B exceeds tools.max_image_bytes (10 B)]" in tr.output
        assert "[image omitted: unsupported image type image/tiff]" in tr.output
        assert tr.output.startswith("pics")


# ---------------------------------------------------------------------------
# session messages, gating helpers, pruning, persistence, token estimate
# ---------------------------------------------------------------------------


class TestSession:
    def test_tool_result_block_carries_images(self):
        msgs = _tool_turn()
        block = msgs[-1]["content"][0]
        assert block["type"] == "tool_result"
        assert block["content"].startswith("[image: image/png")
        assert block["images"] == [{"type": "image_url", "image_url": {"url": PNG_URI}}]

    def test_no_images_key_without_images(self):
        block = _tool_turn(images=[])[-1]["content"][0]
        assert "images" not in block

    def test_strip_adds_note_and_does_not_mutate(self):
        msgs = _tool_turn()
        stripped = strip_tool_result_images(msgs)
        block = stripped[-1]["content"][0]
        assert "images" not in block
        assert "1 image(s) not shown: the active model does not accept images" in block["content"]
        assert "images" in msgs[-1]["content"][0]  # original untouched

    def test_prune_keeps_most_recent(self):
        events = [
            ToolResult(tool_call_id=f"t{i}", output=f"shot {i}", images=[_img()]) for i in range(5)
        ]
        assert prune_tool_result_images(events, keep=2) == 3
        assert [bool(e.images) for e in events] == [False, False, False, True, True]
        assert "dropped from history" in events[0].output
        assert "dropped" not in events[4].output

    def test_prune_zero_drops_all(self):
        events = [ToolResult(tool_call_id="t", output="x", images=[_img()])]
        assert prune_tool_result_images(events, keep=0) == 1
        assert events[0].images == []

    def test_jsonl_round_trip(self, tmp_path):
        from agent.memory.session_store import SessionStore

        s = Session()
        s.add_tool_result(tool_call_id="t", tool_name="screenshot", output="x", images=[_img()])
        store = SessionStore(tmp_path)
        store.save(s)
        loaded = store.load(s.session_id)
        assert loaded.events[0].images == [_img()]

    def test_old_jsonl_without_images_field_loads(self, tmp_path):
        import json

        from agent.memory.session_store import SessionStore

        path = tmp_path / "old1.jsonl"
        path.write_text(
            json.dumps({"_meta": True, "schema_version": 1, "session_id": "old1"})
            + "\n"
            + json.dumps({"type": "tool_result", "tool_call_id": "t", "output": "x"})
            + "\n",
            encoding="utf-8",
        )
        loaded = SessionStore(tmp_path).load("old1")
        assert loaded.events[0].images == []

    def test_token_estimate_counts_images_not_base64(self):
        big = base64.b64encode(b"\x00" * 300_000).decode()
        with_img = estimate_token_count(_tool_turn([_img(big)]))
        without = estimate_token_count(_tool_turn([]))
        assert 1000 <= with_img - without <= 1500

    def test_compaction_estimates_count_images(self):
        from agent.core.compaction.compaction import (
            estimate_event_tokens,
            estimate_message_tokens,
        )

        tr = ToolResult(tool_call_id="t", output="x", images=[_img()])
        assert estimate_event_tokens(tr) >= 1200
        assert estimate_message_tokens(_tool_turn()[-1]) >= 1200


# ---------------------------------------------------------------------------
# provider serialisation
# ---------------------------------------------------------------------------


class TestAnthropic:
    def test_images_inside_tool_result(self):
        from agent.providers.anthropic import _convert_messages_for_anthropic

        out = _convert_messages_for_anthropic(_tool_turn())
        block = out[-1]["content"][0]
        assert block["type"] == "tool_result" and "images" not in block
        text, image = block["content"]
        assert text["type"] == "text" and text["text"].startswith("[image: image/png")
        assert image == {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64},
        }

    def test_plain_tool_result_unchanged(self):
        from agent.providers.anthropic import _convert_messages_for_anthropic

        out = _convert_messages_for_anthropic(_tool_turn([]))
        assert isinstance(out[-1]["content"][0]["content"], str)

    def test_stripped_messages_send_text_only(self):
        from agent.providers.anthropic import _convert_messages_for_anthropic

        out = _convert_messages_for_anthropic(strip_tool_result_images(_tool_turn()))
        block = out[-1]["content"][0]
        assert isinstance(block["content"], str) and "images" not in block


@pytest.mark.parametrize("module", ["openai", "generic"])
class TestOpenAIStyle:
    def _build(self, module: str, messages):
        if module == "openai":
            from agent.providers.openai import _build_messages
        else:
            from agent.providers.generic import _build_messages
        return _build_messages(messages, "")

    def test_images_follow_tool_message_as_user_message(self, module):
        out = self._build(module, _tool_turn())
        roles = [m["role"] for m in out]
        assert roles == ["user", "assistant", "tool", "user"]
        tool_msg, followup = out[2], out[3]
        assert isinstance(tool_msg["content"], str)
        assert tool_msg["content"].startswith("[image: image/png")
        assert followup["content"][1] == {"type": "image_url", "image_url": {"url": PNG_URI}}

    def test_no_followup_without_images(self, module):
        out = self._build(module, _tool_turn([]))
        assert [m["role"] for m in out] == ["user", "assistant", "tool"]

    def test_followup_after_all_parallel_tool_messages(self, module):
        msgs = _tool_turn()
        msgs[-1]["content"].insert(
            0, {"type": "tool_result", "tool_use_id": "tc0", "content": "ok", "is_error": False}
        )
        out = self._build(module, msgs)
        assert [m["role"] for m in out] == ["user", "assistant", "tool", "tool", "user"]


class TestOllama:
    def test_images_follow_as_user_message_with_images_field(self):
        from agent.providers.ollama import _build_messages

        out = _build_messages(_tool_turn(), "")
        assert [m["role"] for m in out] == ["user", "assistant", "tool", "user"]
        assert out[2]["content"].startswith("[image: image/png")
        assert out[3]["images"] == [PNG_B64]
        assert "tc1" in out[3]["content"]

    def test_no_followup_without_images(self):
        from agent.providers.ollama import _build_messages

        out = _build_messages(_tool_turn([]), "")
        assert [m["role"] for m in out] == ["user", "assistant", "tool"]


class TestGemini:
    def test_inline_data_inside_function_response_http(self):
        from agent.providers.gemini import _build_contents

        contents = _build_contents(_tool_turn())
        assert contents[-1]["role"] == "user"
        parts = contents[-1]["parts"]
        assert len(parts) == 1
        response = parts[0]["functionResponse"]
        assert response["name"] == "screenshot"
        assert response["response"]["content"].startswith("[image: image/png")
        assert response["parts"] == [{"inlineData": {"mimeType": "image/png", "data": PNG_B64}}]

    def test_inline_data_is_bytes_in_sdk_mode(self):
        from agent.providers.gemini import _build_contents

        parts = _build_contents(_tool_turn(), sdk_mode=True)[-1]["parts"]
        assert parts[0]["functionResponse"]["parts"][0]["inlineData"]["data"] == PNG_BYTES

    def test_sdk_accepts_parts(self):
        pytest.importorskip("google.genai")
        from google.genai import types

        from agent.providers.gemini import _build_contents

        content = types.Content.model_validate(_build_contents(_tool_turn(), sdk_mode=True)[-1])
        response = content.parts[0].function_response
        assert response.parts[0].inline_data.data == PNG_BYTES
        assert response.parts[0].inline_data.mime_type == "image/png"

    @pytest.mark.parametrize("sdk_mode", [False, True])
    def test_parallel_results_keep_images_with_their_function(self, sdk_mode):
        from copy import deepcopy

        from agent.providers.gemini import _build_contents

        messages = events_to_messages(
            [
                UserMessage(content="Inspect two images"),
                ToolCall(tool_name="first", tool_call_id="tc1", arguments={}),
                ToolCall(tool_name="second", tool_call_id="tc2", arguments={}),
                AssistantMessage(content=""),
                ToolResult(tool_call_id="tc1", tool_name="first", output="first", images=[_img()]),
                ToolResult(
                    tool_call_id="tc2",
                    tool_name="second",
                    output="second",
                    images=[_img(mime="image/jpeg"), _img()],
                ),
            ]
        )
        original = deepcopy(messages)
        contents = _build_contents(messages, sdk_mode=sdk_mode)
        parts = contents[-1]["parts"]
        assert contents[-1]["role"] == "user"
        assert len(parts) == 2
        responses = [part["functionResponse"] for part in parts]
        assert [response["name"] for response in responses] == ["first", "second"]
        assert [len(response["parts"]) for response in responses] == [1, 2]
        assert responses[1]["parts"][0]["inlineData"]["mimeType"] == "image/jpeg"
        expected = PNG_BYTES if sdk_mode else PNG_B64
        assert responses[0]["parts"][0]["inlineData"]["data"] == expected
        assert messages == original

    @pytest.mark.parametrize("sdk_mode", [False, True])
    def test_legacy_models_retain_sibling_image_encoding(self, sdk_mode):
        from agent.providers.gemini import _build_contents

        contents = _build_contents(
            _tool_turn(), sdk_mode=sdk_mode, multimodal_function_responses=False
        )
        parts = contents[-1]["parts"]
        assert contents[-1]["role"] == "user"
        assert "parts" not in parts[0]["functionResponse"]
        expected = PNG_BYTES if sdk_mode else PNG_B64
        assert parts[1] == {"inlineData": {"mimeType": "image/png", "data": expected}}

    def test_text_only_result_has_no_multimodal_parts(self):
        from agent.providers.gemini import _build_contents

        response = _build_contents(_tool_turn([]))[-1]["parts"][0]["functionResponse"]
        assert "parts" not in response
        assert response["name"] == "screenshot"
        assert response["response"]["content"].startswith("[image: image/png")

    def test_unsupported_image_leaves_text_response_intact(self):
        from agent.providers.gemini import _build_contents

        messages = _tool_turn()
        messages[-1]["content"][0]["images"] = [
            {"type": "image_url", "image_url": {"url": "https://example.test/image.png"}}
        ]
        response = _build_contents(messages)[-1]["parts"][0]["functionResponse"]
        assert "parts" not in response
        assert response["response"]["content"].startswith("[image: image/png")

    @pytest.mark.parametrize("streaming", [False, True])
    @pytest.mark.parametrize("model", ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-3.5-flash"])
    async def test_http_request_encodes_tool_images_for_model(self, streaming, model):
        import json

        import httpx

        from agent.providers.gemini import GeminiProvider

        requests = []

        def handle(request):
            requests.append(request)
            contents = json.loads(request.content)["contents"]
            assert contents[-1]["role"] == "user"
            parts = contents[-1]["parts"]
            response = parts[0]["functionResponse"]
            assert response["name"] == "screenshot"
            image = {"inlineData": {"mimeType": "image/png", "data": PNG_B64}}
            if model.startswith("gemini-3"):
                assert len(parts) == 1
                assert response["parts"] == [image]
            else:
                assert "parts" not in response
                assert parts[1:] == [image]
            body = {
                "candidates": [{"content": {"parts": [{"text": "OK"}]}, "finishReason": "STOP"}]
            }
            if streaming:
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    text="data: " + json.dumps(body) + "\n\n",
                )
            return httpx.Response(200, json=body)

        provider = GeminiProvider(
            ProviderConfig(
                name="gemini",
                model=model,
                api_key="test-key",
                base_url="https://example.test/gemini/gemini-3.5-flash",
            )
        )
        await provider._http_client.aclose()
        provider._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        try:
            if streaming:
                deltas = [delta async for delta in provider.stream(_tool_turn())]
                assert "".join(delta.text for delta in deltas) == "OK"
            else:
                assert (await provider.complete(_tool_turn())).content == "OK"
            assert len(requests) == 1
        finally:
            await provider.close()

    def test_user_image_attachment_reaches_gemini(self):
        from agent.providers.gemini import _build_contents

        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": PNG_URI}},
                    {"type": "text", "text": "what is this?"},
                ],
            }
        ]
        parts = _build_contents(msgs)[0]["parts"]
        assert parts[0] == {"inlineData": {"mimeType": "image/png", "data": PNG_B64}}
        assert parts[1] == {"text": "what is this?"}


# ---------------------------------------------------------------------------
# loop: vision gating and pruning
# ---------------------------------------------------------------------------


class _VisionMock(MockProvider):
    def __init__(self, vision: bool) -> None:
        super().__init__()
        self._vision = vision

    @property
    def supports_vision(self) -> bool:
        return self._vision


def _screenshot_registry() -> ToolRegistry:
    async def shot(n: int = 0) -> ToolOutput:
        return ToolOutput(text=f"[image: image/png, 72 B] #{n}", images=[_img()])

    reg = ToolRegistry()
    reg.add(
        ToolSpec(
            name="screenshot",
            description="Take a screenshot",
            input_schema={"type": "object", "properties": {"n": {"type": "integer"}}},
            handler=shot,
        )
    )
    return reg


def _config(**tools: Any) -> AgentConfig:
    return AgentConfig(
        provider=ProviderConfig(name="mock", model="mock-1"),
        max_steps=10,
        timeout=30.0,
        tools=ToolConfig(enabled_builtins=[], **tools),
    )


def _last_tool_block(messages: list[dict]) -> dict:
    for msg in reversed(messages):
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result":
                    return block
    raise AssertionError("no tool_result block sent")


class TestLoopGating:
    async def test_vision_provider_receives_images(self):
        provider = _VisionMock(vision=True)
        provider.enqueue_tool_call("screenshot", {}, tool_call_id="tc1")
        provider.enqueue_text("I see a screen")
        session = Session()
        session.add_user_message("look")
        ex = ToolExecutor(_screenshot_registry(), ToolConfig(), SafetyConfig())
        await run_loop(session, provider, ex, _config())

        block = _last_tool_block(provider.call_history[-1]["messages"])
        assert block["images"] == [{"type": "image_url", "image_url": {"url": PNG_URI}}]

    async def test_non_vision_provider_gets_placeholder_only(self):
        provider = _VisionMock(vision=False)
        provider.enqueue_tool_call("screenshot", {}, tool_call_id="tc1")
        provider.enqueue_text("ok")
        session = Session()
        session.add_user_message("look")
        ex = ToolExecutor(_screenshot_registry(), ToolConfig(), SafetyConfig())
        await run_loop(session, provider, ex, _config())

        block = _last_tool_block(provider.call_history[-1]["messages"])
        assert "images" not in block
        assert block["content"].startswith("[image: image/png, 72 B]")
        assert "not shown: the active model does not accept images" in block["content"]
        # The session itself keeps the image (a later /model switch may use it).
        tr = next(e for e in session.events if isinstance(e, ToolResult))
        assert tr.images

    async def test_loop_prunes_old_images(self):
        provider = _VisionMock(vision=True)
        for i in range(3):
            provider.enqueue_tool_call("screenshot", {"n": i}, tool_call_id=f"t{i}")
        provider.enqueue_text("done")
        session = Session()
        session.add_user_message("look thrice")
        cfg = _config(keep_recent_images=1)
        ex = ToolExecutor(_screenshot_registry(), cfg.tools, SafetyConfig())
        await run_loop(session, provider, ex, cfg)

        results = [e for e in session.events if isinstance(e, ToolResult)]
        assert len(results) == 3
        assert [bool(r.images) for r in results] == [False, False, True]
        last_sent = provider.call_history[-1]["messages"]
        sent_images = [
            b
            for m in last_sent
            if isinstance(m.get("content"), list)
            for b in m["content"]
            if b.get("type") == "tool_result" and b.get("images")
        ]
        assert len(sent_images) == 1


# ---------------------------------------------------------------------------
# MCP bridge
# ---------------------------------------------------------------------------


class TestMCPBridgeImages:
    def _fake_image(self, data: Any = PNG_B64, mime: str = "image/png"):
        from unittest.mock import MagicMock

        block = MagicMock(spec=["data", "mimeType"])
        block.data = data
        block.mimeType = mime
        return block

    def _fake_text(self, text: str):
        from unittest.mock import MagicMock

        block = MagicMock(spec=["text"])
        block.text = text
        return block

    def test_content_to_output_keeps_image(self):
        from agent.extensions.mcp import _content_to_output

        out = _content_to_output([self._fake_image(), self._fake_text("Screenshot (8x8).")])
        assert isinstance(out, ToolOutput)
        assert out.text == "[image: image/png, 72 B]\nScreenshot (8x8)."
        assert out.images == [_img()]

    def test_raw_bytes_are_encoded(self):
        from agent.extensions.mcp import _content_to_output

        out = _content_to_output([self._fake_image(data=PNG_BYTES)])
        assert out.images == [_img()]

    def test_text_only_stays_string(self):
        from agent.extensions.mcp import _content_to_output

        assert _content_to_output([self._fake_text("hi")]) == "hi"

    def test_image_blob_resource(self):
        from unittest.mock import MagicMock

        from agent.extensions.mcp import _content_to_output

        resource = MagicMock(spec=["blob", "mimeType", "uri"])
        resource.blob = PNG_B64
        resource.mimeType = "image/png"
        resource.uri = "file:///x.png"
        block = MagicMock(spec=["resource"])
        block.resource = resource
        out = _content_to_output([block])
        assert isinstance(out, ToolOutput) and out.images == [_img()]

    async def test_bridge_tool_through_executor(self, monkeypatch):
        from agent.extensions.mcp import MCPBridge, MCPServerConfig
        from tests.test_mcp import _make_fake_tool, _make_mcp_sys_modules

        modules, session = _make_mcp_sys_modules(
            tools=[_make_fake_tool("screenshot", "shot", {"type": "object", "properties": {}})],
            call_result_content=[self._fake_image(), self._fake_text("Screenshot.")],
        )
        session.call_tool.return_value.isError = False
        for key, mod in modules.items():
            monkeypatch.setitem(sys.modules, key, mod)

        reg = ToolRegistry()
        async with MCPBridge([MCPServerConfig(name="win", command="x")]) as bridge:
            await bridge.register_all(reg)
            ex = ToolExecutor(reg, ToolConfig(), SafetyConfig())
            [tr] = await ex.execute(
                [ToolCall(tool_name="screenshot", tool_call_id="t", arguments={})]
            )
        assert not tr.is_error
        assert tr.output.startswith("[image: image/png, 72 B]")
        assert tr.images == [_img()]

    async def test_error_result_raises_text_only(self, monkeypatch):
        from agent.extensions.mcp import MCPClient, MCPServerConfig, MCPToolError
        from tests.test_mcp import _make_mcp_sys_modules

        modules, session = _make_mcp_sys_modules(
            call_result_content=[self._fake_text("boom"), self._fake_image()]
        )
        session.call_tool.return_value.isError = True
        for key, mod in modules.items():
            monkeypatch.setitem(sys.modules, key, mod)
        async with MCPClient(MCPServerConfig(name="s", command="x")) as client:
            with pytest.raises(MCPToolError, match="boom"):
                await client.call_tool("screenshot", {})


# ---------------------------------------------------------------------------
# ACP: the tool-call card carries the image
# ---------------------------------------------------------------------------


class TestAcpImages:
    async def test_tool_call_update_has_image_block(self, tmp_path):
        pytest.importorskip("acp")
        from acp.schema import ImageContentBlock, ToolCallProgress

        from agent.memory.session_store import SessionStore
        from tests.test_acp import _make_config, _make_sdk_agent

        provider = MockProvider()
        provider.enqueue_tool_call("screenshot", {}, tool_call_id="tc_img")
        provider.enqueue_text("done")
        sdk_agent = _make_sdk_agent(provider)
        sdk_agent._config = _make_config().model_copy(update={"session_dir": tmp_path})
        sdk_agent._store = SessionStore(tmp_path)
        mock_conn = AsyncMock()
        sdk_agent._conn = mock_conn

        async def shot() -> ToolOutput:
            return ToolOutput(text="[image: image/png, 72 B]", images=[_img()])

        original_make = sdk_agent._make_aar_agent

        def make_with_tool(*args: Any, **kwargs: Any) -> Any:
            agent = original_make(*args, **kwargs)
            agent.registry.add(
                ToolSpec(
                    name="screenshot",
                    description="shot",
                    input_schema={"type": "object", "properties": {}},
                    handler=shot,
                )
            )
            return agent

        sdk_agent._make_aar_agent = make_with_tool  # type: ignore[method-assign]

        r = await sdk_agent.new_session()
        await sdk_agent.prompt(prompt=[{"text": "go"}], session_id=r.session_id)

        updates = [c.kwargs["update"] for c in mock_conn.session_update.call_args_list]
        final = [
            u
            for u in updates
            if isinstance(u, ToolCallProgress)
            and u.tool_call_id == "tc_img"
            and u.status == "completed"
        ]
        assert final, "tool call never completed"
        images = [c.content for c in final[-1].content if isinstance(c.content, ImageContentBlock)]
        assert len(images) == 1
        assert images[0].mime_type == "image/png" and images[0].data == PNG_B64
        # The wire form uses ACP's camelCase field name.
        dumped = final[-1].model_dump(by_alias=True, exclude_none=True)
        assert any(c["content"].get("mimeType") == "image/png" for c in dumped["content"])
