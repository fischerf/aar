"""Provider refusals — safety classifiers declining a request.

Anthropic returns ``stop_reason="refusal"`` (Claude Opus 4.7+) with a
``stop_details`` category and usually empty content; OpenAI's equivalent is
``finish_reason="content_filter"``.

Before this was handled, a refusal mapped to ``end_turn``, and the loop's
premature-end guardrail mistook the empty response for the model giving up —
so a declined request silently cost three provider calls and the category was
discarded. These tests pin the terminal behaviour and the plumbing that
carries the reason through both the blocking and streaming paths.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agent.core.config import SafetyConfig, ToolConfig
from agent.core.events import AssistantMessage, ErrorEvent, StopReason
from agent.core.loop import run_loop
from agent.core.loop_helpers import format_refusal, parse_stop
from agent.core.provider_runner import _consume_stream
from agent.core.session import Session
from agent.core.state import AgentState
from agent.providers.anthropic import _extract_stop_details
from agent.providers.anthropic import _map_stop_reason as _anthropic_stop
from agent.providers.base import ProviderMeta, ProviderResponse, StreamDelta
from agent.providers.openai import _map_stop_reason as _openai_stop
from agent.tools.execution import ToolExecutor

# ---------------------------------------------------------------------------
# Stop-reason mapping
# ---------------------------------------------------------------------------


class TestAnthropicStopReason:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("end_turn", StopReason.END_TURN),
            ("tool_use", StopReason.TOOL_USE),
            ("max_tokens", StopReason.MAX_TOKENS),
            ("refusal", StopReason.REFUSAL),
            # A stop sequence is a normal, complete turn.
            ("stop_sequence", StopReason.END_TURN),
            # Server-tool pause — aar doesn't resume, so the turn is over.
            ("pause_turn", StopReason.END_TURN),
        ],
    )
    def test_mapping(self, raw: str, expected: StopReason) -> None:
        assert _anthropic_stop(raw) == expected.value

    def test_unknown_falls_back_to_end_turn(self) -> None:
        """A future stop reason must not leak a non-StopReason string."""
        assert _anthropic_stop("some_future_reason") == StopReason.END_TURN.value
        assert _anthropic_stop(None) == StopReason.END_TURN.value

    def test_every_mapped_value_is_a_valid_stop_reason(self) -> None:
        for raw in ("end_turn", "tool_use", "max_tokens", "refusal", "pause_turn", "nonsense"):
            StopReason(_anthropic_stop(raw))  # raises if it isn't a real member


class TestOpenAIStopReason:
    def test_content_filter_is_a_refusal(self) -> None:
        assert _openai_stop("content_filter") == StopReason.REFUSAL.value

    def test_known_and_unknown(self) -> None:
        assert _openai_stop("stop") == StopReason.END_TURN.value
        assert _openai_stop("tool_calls") == StopReason.TOOL_USE.value
        assert _openai_stop("length") == StopReason.MAX_TOKENS.value
        assert _openai_stop("brand_new") == StopReason.END_TURN.value
        assert _openai_stop(None) == StopReason.END_TURN.value


def test_parse_stop_accepts_refusal() -> None:
    assert parse_stop("refusal") is StopReason.REFUSAL


# ---------------------------------------------------------------------------
# stop_details extraction and formatting
# ---------------------------------------------------------------------------


class TestStopDetails:
    def test_extracts_category_and_explanation(self) -> None:
        msg = SimpleNamespace(
            stop_details=SimpleNamespace(type="refusal", category="cyber", explanation="nope")
        )
        assert _extract_stop_details(msg) == {
            "type": "refusal",
            "category": "cyber",
            "explanation": "nope",
        }

    def test_absent_details_yield_none(self) -> None:
        assert _extract_stop_details(SimpleNamespace()) is None
        assert _extract_stop_details(SimpleNamespace(stop_details=None)) is None

    def test_partial_details(self) -> None:
        msg = SimpleNamespace(stop_details=SimpleNamespace(category="bio", explanation=None))
        assert _extract_stop_details(msg) == {"category": "bio"}

    @pytest.mark.parametrize(
        ("details", "expected"),
        [
            ({"category": "cyber", "explanation": "why"}, "cyber — why"),
            ({"category": "cyber"}, "cyber"),
            ({"explanation": "why"}, "why"),
            ({}, "no reason supplied by the provider"),
            (None, "no reason supplied by the provider"),
        ],
    )
    def test_format_refusal(self, details: dict[str, Any] | None, expected: str) -> None:
        assert format_refusal(details) == expected


# ---------------------------------------------------------------------------
# Streaming: the real stop reason must survive delta assembly
# ---------------------------------------------------------------------------


class _StreamStub:
    """Minimal provider that replays a fixed list of deltas."""

    def __init__(self, deltas: list[StreamDelta]) -> None:
        self._deltas = deltas

    async def stream(self, messages, tools=None, system=""):  # noqa: ANN001, ARG002
        for d in self._deltas:
            yield d


@pytest.mark.asyncio
async def test_stream_preserves_provider_stop_reason() -> None:
    """Regression: streaming used to infer the stop reason from tool calls
    alone, which cannot tell a refusal from a normal end_turn."""
    provider = _StreamStub(
        [
            StreamDelta(text=""),
            StreamDelta(
                done=True,
                stop_reason=StopReason.REFUSAL.value,
                stop_details={"category": "cyber", "explanation": "declined"},
            ),
        ]
    )
    session = Session()
    response = await _consume_stream(provider, [], None, "", session, None)
    assert response.stop_reason == StopReason.REFUSAL.value
    assert response.stop_details == {"category": "cyber", "explanation": "declined"}


@pytest.mark.asyncio
async def test_stream_without_stop_reason_still_infers() -> None:
    """Providers that don't report one keep the previous behaviour."""
    provider = _StreamStub([StreamDelta(text="hi"), StreamDelta(done=True)])
    session = Session()
    response = await _consume_stream(provider, [], None, "", session, None)
    assert response.stop_reason == StopReason.END_TURN.value
    assert response.stop_details is None


# ---------------------------------------------------------------------------
# Loop behaviour
# ---------------------------------------------------------------------------


_DEFAULT_DETAILS: dict[str, Any] = {"category": "cyber"}


def _refusal_response(details: dict[str, Any] | None = _DEFAULT_DETAILS) -> ProviderResponse:
    """A refusal as the providers emit it: empty content, terminal stop reason.

    Pass ``details=None`` for a provider that reported no structured reason.
    """
    return ProviderResponse(
        content="",
        stop_reason=StopReason.REFUSAL.value,
        stop_details=details,
        meta=ProviderMeta(provider="mock", model="mock-1"),
    )


@pytest.mark.asyncio
async def test_loop_stops_on_refusal_without_retrying(
    mock_provider, tool_registry, default_config
) -> None:
    """The premature-end guardrail must not fire on a refusal.

    A refusal returns empty content, which is exactly the shape that guardrail
    watches for — so without explicit handling the loop re-asks twice before
    giving up, spending three calls on a request that cannot succeed.
    """
    mock_provider.enqueue(_refusal_response({"category": "cyber", "explanation": "declined"}))
    session = Session()
    session.add_user_message("something disallowed")

    executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
    result = await run_loop(session, mock_provider, executor, default_config)

    assert result.step_count == 1, "refusal must not be retried"
    assert len(mock_provider.call_history) == 1

    assistant = [e for e in result.events if isinstance(e, AssistantMessage)]
    assert len(assistant) == 1
    assert assistant[0].stop_reason is StopReason.REFUSAL
    assert assistant[0].data["stop_details"] == {"category": "cyber", "explanation": "declined"}

    errors = [e for e in result.events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    assert "declined by the provider" in errors[0].message
    assert "cyber" in errors[0].message
    assert errors[0].recoverable is False


@pytest.mark.asyncio
async def test_loop_refusal_without_details_still_terminates(
    mock_provider, tool_registry, default_config
) -> None:
    mock_provider.enqueue(_refusal_response(None))
    session = Session()
    session.add_user_message("hi")

    executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
    result = await run_loop(session, mock_provider, executor, default_config)

    assert result.step_count == 1
    errors = [e for e in result.events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    assert "no reason supplied" in errors[0].message
    # No stop_details key when the provider supplied none.
    assistant = [e for e in result.events if isinstance(e, AssistantMessage)]
    assert "stop_details" not in assistant[0].data


@pytest.mark.asyncio
async def test_loop_still_recovers_from_genuine_premature_end(
    mock_provider, tool_registry, default_config
) -> None:
    """Guard against over-correcting: an empty end_turn is still retried."""
    mock_provider.enqueue_text("", stop="end_turn")
    mock_provider.enqueue_text("done properly", stop="end_turn")
    session = Session()
    session.add_user_message("do the thing")

    executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
    result = await run_loop(session, mock_provider, executor, default_config)

    assert result.state is AgentState.COMPLETED
    assert not [e for e in result.events if isinstance(e, ErrorEvent)]
