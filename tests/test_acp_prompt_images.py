"""Images in ACP prompts reach the model instead of an ``[image]`` placeholder."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest

from agent.memory.session_store import SessionStore
from agent.transports.acp.common import _prompt_content
from tests.conftest import MockProvider
from tests.test_acp import _make_config, _make_sdk_agent

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 32).decode()


def test_text_only_prompt_stays_a_string():
    text, content, problems = _prompt_content([{"type": "text", "text": "hi"}], max_image_bytes=1000)
    assert text == content == "hi"
    assert problems == []


def test_images_become_blocks_before_the_text():
    prompt = [
        {"type": "text", "text": "Was ist auf dem Foto?"},
        {"type": "image", "mimeType": "image/png", "data": PNG},
    ]
    text, content, problems = _prompt_content(prompt, max_image_bytes=1000)
    assert text == "Was ist auf dem Foto?"
    assert [b.type for b in content] == ["image_url", "text"]
    assert content[0].image_url.url == f"data:image/png;base64,{PNG}"
    assert content[1].text == "Was ist auf dem Foto?"
    assert problems == []


def test_image_without_text_gets_a_default_question():
    _, content, _ = _prompt_content([{"type": "image", "mimeType": "image/png", "data": PNG}], max_image_bytes=1000)
    assert content[-1].text


def test_oversized_and_unsupported_images_are_reported():
    prompt = [
        {"type": "text", "text": "x"},
        {"type": "image", "mimeType": "image/png", "data": PNG},
        {"type": "image", "mimeType": "image/tiff", "data": PNG},
    ]
    _, content, problems = _prompt_content(prompt, max_image_bytes=10)
    assert content == "x"
    assert len(problems) == 2


@pytest.mark.asyncio
async def test_prompt_sends_image_to_the_provider(tmp_path):
    provider = MockProvider()
    provider.enqueue_text("Ein Tisch.", stop="end_turn")
    config = _make_config().model_copy(update={"session_dir": tmp_path})
    sdk_agent = _make_sdk_agent(provider)
    sdk_agent._config = config
    sdk_agent._store = SessionStore(tmp_path)
    session_id = (await sdk_agent.new_session()).session_id
    sdk_agent._conn = AsyncMock()

    resp = await sdk_agent.prompt(
        prompt=[{"type": "text", "text": "Was ist darauf?"}, {"type": "image", "mimeType": "image/png", "data": PNG}],
        session_id=session_id,
    )

    assert resp.stop_reason == "end_turn"
    sent = str(provider.call_history[0]["messages"])
    assert PNG in sent
    assert "[image]" not in sent
