"""Session observability — aggregate metrics from a session's typed events.

Usage::

    from agent.extensions.observability import session_metrics

    m = session_metrics(session)
    print(f"steps={m.total_steps} tokens={m.total_tokens} errors={m.total_errors}")
    print(f"provider_ms={m.total_provider_duration_ms:.0f} tool_ms={m.total_tool_duration_ms:.0f}")
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.core.events import ErrorEvent, ProviderMeta, ToolCall, ToolResult
from agent.core.session import Session


@dataclass
class ToolCallMetrics:
    tool_name: str
    duration_ms: float
    is_error: bool


@dataclass
class StepMetrics:
    """Metrics for a single provider round-trip."""

    step: int
    provider_duration_ms: float = 0.0
    tool_calls: list[ToolCallMetrics] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tool_duration_ms(self) -> float:
        return sum(t.duration_ms for t in self.tool_calls)


@dataclass
class SessionMetrics:
    """Aggregated metrics for a complete session."""

    session_id: str = ""
    trace_id: str = ""
    total_steps: int = 0
    total_tool_calls: int = 0
    total_errors: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_provider_duration_ms: float = 0.0
    total_tool_duration_ms: float = 0.0
    steps: list[StepMetrics] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens


def session_metrics(session: Session) -> SessionMetrics:
    """Aggregate timing and usage metrics from a session's events.

    Iterates over all events once. ProviderMeta events mark the start of a new
    logical step; ToolCall/ToolResult events are attributed to the step in which
    they occur.
    """
    metrics = SessionMetrics(
        session_id=session.session_id,
        trace_id=session.trace_id,
        total_steps=session.step_count,
    )

    current_step: StepMetrics | None = None
    pending_tool_names: dict[str, str] = {}  # tool_call_id -> tool_name

    for event in session.events:
        if isinstance(event, ProviderMeta):
            # Each ProviderMeta marks a new provider round-trip
            step_num = len(metrics.steps) + 1
            current_step = StepMetrics(
                step=step_num,
                provider_duration_ms=event.duration_ms,
                input_tokens=event.usage.get("input_tokens", 0),
                output_tokens=event.usage.get("output_tokens", 0),
            )
            metrics.steps.append(current_step)
            metrics.total_provider_duration_ms += event.duration_ms
            metrics.total_input_tokens += current_step.input_tokens
            metrics.total_output_tokens += current_step.output_tokens

        elif isinstance(event, ToolCall):
            metrics.total_tool_calls += 1
            pending_tool_names[event.tool_call_id] = event.tool_name

        elif isinstance(event, ToolResult):
            metrics.total_tool_duration_ms += event.duration_ms
            if event.is_error:
                metrics.total_errors += 1
            if current_step is not None:
                current_step.tool_calls.append(ToolCallMetrics(
                    tool_name=event.tool_name,
                    duration_ms=event.duration_ms,
                    is_error=event.is_error,
                ))

        elif isinstance(event, ErrorEvent):
            metrics.total_errors += 1

    return metrics
