"""Tests for agent/extensions/observability.py"""

from __future__ import annotations

import pytest

from agent.core.events import ErrorEvent, ProviderMeta, ToolCall, ToolResult
from agent.core.session import Session
from agent.core.state import AgentState
from agent.extensions.observability import SessionMetrics, StepMetrics, session_metrics


def _make_session(
    *,
    n_steps: int = 1,
    tool_calls: int = 0,
    errors: int = 0,
    provider_ms: float = 50.0,
    tool_ms: float = 10.0,
) -> Session:
    s = Session()
    s.state = AgentState.COMPLETED
    s.step_count = n_steps

    for step in range(n_steps):
        s.append(ProviderMeta(
            provider="mock",
            model="mock-1",
            usage={"input_tokens": 10, "output_tokens": 5},
            duration_ms=provider_ms,
        ))
        for t in range(tool_calls):
            tc_id = f"tc_{step}_{t}"
            s.append(ToolCall(tool_name="echo", tool_call_id=tc_id, arguments={}))
            s.append(ToolResult(
                tool_call_id=tc_id,
                tool_name="echo",
                output="ok",
                duration_ms=tool_ms,
            ))

    for _ in range(errors):
        s.append(ErrorEvent(message="boom", recoverable=False))

    return s


class TestSessionMetrics:
    def test_empty_session(self):
        m = session_metrics(Session())
        assert m.total_steps == 0
        assert m.total_tool_calls == 0
        assert m.total_errors == 0
        assert m.total_tokens == 0

    def test_single_step_no_tools(self):
        s = _make_session(n_steps=1, tool_calls=0)
        m = session_metrics(s)

        assert m.total_provider_duration_ms == 50.0
        assert m.total_tool_duration_ms == 0.0
        assert m.total_input_tokens == 10
        assert m.total_output_tokens == 5
        assert m.total_tokens == 15
        assert len(m.steps) == 1
        assert m.steps[0].provider_duration_ms == 50.0

    def test_multi_step_with_tools(self):
        s = _make_session(n_steps=3, tool_calls=2, provider_ms=40.0, tool_ms=8.0)
        m = session_metrics(s)

        assert m.total_provider_duration_ms == pytest.approx(120.0)
        assert m.total_tool_calls == 6        # 3 steps × 2 tools
        assert m.total_tool_duration_ms == pytest.approx(48.0)  # 6 × 8ms
        assert len(m.steps) == 3
        assert len(m.steps[0].tool_calls) == 2

    def test_error_counting(self):
        s = _make_session(n_steps=1, errors=3)
        m = session_metrics(s)
        assert m.total_errors == 3

    def test_tool_error_counted(self):
        s = Session()
        s.append(ProviderMeta(provider="m", model="m", usage={}, duration_ms=10))
        s.append(ToolCall(tool_name="bash", tool_call_id="tc_1", arguments={}))
        s.append(ToolResult(tool_call_id="tc_1", tool_name="bash", output="Error", is_error=True, duration_ms=5))
        m = session_metrics(s)
        assert m.total_errors == 1
        assert m.steps[0].tool_calls[0].is_error is True

    def test_session_ids_propagated(self):
        s = Session()
        m = session_metrics(s)
        assert m.session_id == s.session_id
        assert m.trace_id == s.trace_id

    def test_step_total_tool_duration(self):
        s = _make_session(n_steps=1, tool_calls=3, tool_ms=5.0)
        m = session_metrics(s)
        assert m.steps[0].total_tool_duration_ms == pytest.approx(15.0)
