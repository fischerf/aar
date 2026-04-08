"""Thin core loop — the heart of the agent runtime."""

from __future__ import annotations

import asyncio
import logging
import time

from agent.core.config import AgentConfig
from agent.core.events import (
    AssistantMessage,
    ErrorEvent,
    ReasoningBlock,
    StopReason,
    StreamChunk,
    ToolCall,
)
from agent.core.session import Session, trim_to_token_budget
from agent.core.state import AgentState
from agent.providers.base import Provider, ProviderResponse
from agent.tools.execution import ToolExecutor

logger = logging.getLogger(__name__)


async def run_loop(
    session: Session,
    provider: Provider,
    tool_executor: ToolExecutor,
    config: AgentConfig,
    on_event=None,
    cancel_event: asyncio.Event | None = None,
) -> Session:
    """Run the agent loop until completion, max steps, or timeout.

    Args:
        session: The current session with conversation history.
        provider: The LLM provider to use.
        tool_executor: Executor for tool calls.
        config: Agent configuration.
        on_event: Optional callback called with each new event.
        cancel_event: Optional asyncio.Event; set it to request cooperative cancellation.

    Returns:
        The updated session.
    """
    session.state = AgentState.RUNNING
    start_time = time.monotonic()
    done = False

    _log = logger.getChild("loop")
    _extra = {"session_id": session.session_id, "trace_id": session.trace_id}

    try:
        while not done and session.step_count < config.max_steps:
            # Cooperative cancellation
            if cancel_event is not None and cancel_event.is_set():
                session.state = AgentState.CANCELLED
                _emit(session, on_event, ErrorEvent(message="Agent cancelled", recoverable=False))
                return session

            # Timeout check
            elapsed = time.monotonic() - start_time
            if elapsed > config.timeout:
                session.state = AgentState.TIMED_OUT
                _emit(
                    session,
                    on_event,
                    ErrorEvent(
                        message=f"Agent timed out after {config.timeout}s",
                        recoverable=False,
                    ),
                )
                return session

            session.increment_step()
            messages = session.to_messages()

            # Automatic context management — trim old messages if configured
            if config.context_window > 0 and config.context_strategy == "sliding_window":
                messages = trim_to_token_budget(messages, config.context_window)

            tool_schemas = tool_executor.registry.to_provider_schemas() or None

            use_streaming = config.streaming and provider.supports_streaming

            # Provider call — timed, with retry for recoverable errors
            t_provider = time.monotonic()
            response: ProviderResponse | None = None
            for attempt in range(1, config.max_retries + 1):
                try:
                    if use_streaming:
                        response = await _consume_stream(
                            provider,
                            messages,
                            tool_schemas,
                            config.system_prompt,
                            session,
                            on_event,
                        )
                    else:
                        response = await provider.complete(
                            messages=messages,
                            tools=tool_schemas,
                            system=config.system_prompt,
                        )
                    break
                except Exception as e:
                    _friendly, _recoverable = _provider_error_message(e)
                    if _recoverable and attempt < config.max_retries:
                        delay = 2 ** (attempt - 1)
                        _log.info(
                            "Recoverable error at step %d (attempt %d/%d), retrying in %ds: %s",
                            session.step_count,
                            attempt,
                            config.max_retries,
                            delay,
                            _friendly,
                            extra=_extra,
                        )
                        await asyncio.sleep(delay)
                        continue
                    _log.warning(
                        "Provider error at step %d: %s",
                        session.step_count,
                        _friendly,
                        extra=_extra,
                    )
                    _log.debug("Provider error detail", exc_info=True, extra=_extra)
                    session.state = AgentState.ERROR
                    _emit(
                        session,
                        on_event,
                        ErrorEvent(message=_friendly, recoverable=_recoverable),
                    )
                    return session
            assert response is not None

            provider_ms = (time.monotonic() - t_provider) * 1000

            # Stamp provider timing and record metadata
            if response.meta:
                response.meta.duration_ms = provider_ms
                _emit(session, on_event, response.meta)

            # Record reasoning blocks
            for rb in response.reasoning:
                _emit(session, on_event, rb)

            # ── Token / cost budget enforcement ─────────────────────────
            if response.meta and response.meta.usage:
                from agent.core.tokens import TokenUsage, calculate_cost, get_pricing

                usage = TokenUsage.from_dict(response.meta.usage)
                session.total_input_tokens += usage.input_tokens
                session.total_output_tokens += usage.output_tokens

                # Cost tracking
                pricing = get_pricing(config.provider.model)
                if pricing:
                    step_cost = calculate_cost(usage, pricing)
                    session.total_cost += step_cost

                # Token budget check
                if config.token_budget > 0:
                    if session.total_tokens >= config.token_budget:
                        session.state = AgentState.BUDGET_EXCEEDED
                        _emit(
                            session,
                            on_event,
                            ErrorEvent(
                                message=(
                                    f"Token budget exceeded"
                                    f" ({session.total_tokens}/{config.token_budget})"
                                ),
                                recoverable=False,
                            ),
                        )
                        return session

                # Cost limit check
                if config.cost_limit > 0:
                    if session.total_cost >= config.cost_limit:
                        session.state = AgentState.BUDGET_EXCEEDED
                        _emit(
                            session,
                            on_event,
                            ErrorEvent(
                                message=(
                                    f"Cost limit exceeded"
                                    f" (${session.total_cost:.4f}/${config.cost_limit:.4f})"
                                ),
                                recoverable=False,
                            ),
                        )
                        return session

            _log.info(
                "step=%d provider_ms=%.0f tool_calls=%d",
                session.step_count,
                provider_ms,
                len(response.tool_calls),
                extra=_extra,
            )

            # Handle tool calls
            if response.tool_calls:
                # Emit ToolCall events BEFORE AssistantMessage so that
                # session.to_messages() sees the correct order:
                #   ToolCall… → AssistantMessage → ToolResult…
                # and can bundle the tool_calls onto the assistant message.
                for tc in response.tool_calls:
                    spec = tool_executor.registry.get(tc.tool_name)
                    if spec:
                        tc.data["side_effects"] = [e.value for e in spec.side_effects]
                    _emit(session, on_event, tc)
                _emit(
                    session,
                    on_event,
                    AssistantMessage(content=response.content, stop_reason=StopReason.TOOL_USE),
                )

                session.state = AgentState.WAITING_FOR_TOOL
                results = await tool_executor.execute(response.tool_calls)
                for tr in results:
                    _emit(session, on_event, tr)
                session.state = AgentState.RUNNING
                continue

            # Final assistant message
            stop = _parse_stop(response.stop_reason)
            _emit(session, on_event, AssistantMessage(content=response.content, stop_reason=stop))
            if stop in {StopReason.END_TURN, StopReason.MAX_TOKENS}:
                done = True

        if session.step_count >= config.max_steps and not done:
            session.state = AgentState.MAX_STEPS
            _emit(
                session,
                on_event,
                ErrorEvent(message=f"Reached max steps ({config.max_steps})", recoverable=False),
            )

        if session.state == AgentState.RUNNING:
            session.state = AgentState.COMPLETED

    except asyncio.CancelledError:
        session.state = AgentState.CANCELLED
        _emit(session, on_event, ErrorEvent(message="Agent cancelled", recoverable=False))
        raise

    return session


async def _consume_stream(
    provider: Provider,
    messages: list,
    tools: list | None,
    system: str,
    session: Session,
    on_event,
) -> ProviderResponse:
    """Consume a provider stream, emit StreamChunk events, return assembled response."""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    stop_reason = ""

    async for delta in provider.stream(messages=messages, tools=tools, system=system):
        # Emit chunk events for text and reasoning deltas
        if delta.text or delta.reasoning_delta:
            _emit(
                session,
                on_event,
                StreamChunk(
                    text=delta.text,
                    reasoning_text=delta.reasoning_delta,
                ),
            )

        if delta.text:
            content_parts.append(delta.text)
        if delta.reasoning_delta:
            reasoning_parts.append(delta.reasoning_delta)

        # Accumulated tool call (emitted when stream signals done or per-call)
        if delta.tool_call_delta:
            tc = delta.tool_call_delta
            tool_calls.append(
                ToolCall(
                    tool_name=tc.get("tool_name", ""),
                    tool_call_id=tc.get("tool_call_id", ""),
                    arguments=tc.get("arguments", {}),
                )
            )

        if delta.done:
            _emit(session, on_event, StreamChunk(finished=True))
            if tool_calls:
                stop_reason = StopReason.TOOL_USE.value
            else:
                stop_reason = StopReason.END_TURN.value
            break

    reasoning_blocks = []
    if reasoning_parts:
        reasoning_blocks = [ReasoningBlock(content="".join(reasoning_parts))]

    return ProviderResponse(
        content="".join(content_parts),
        tool_calls=tool_calls,
        stop_reason=stop_reason,
        reasoning=reasoning_blocks,
    )


def _emit(session: Session, on_event, event) -> None:
    """Append an event to the session and fire the callback."""
    session.append(event)
    if on_event:
        on_event(event)


def _provider_error_message(exc: BaseException) -> tuple[str, bool]:
    """Return ``(user_friendly_message, is_recoverable)`` for a provider exception.

    Keeps raw tracebacks out of user-facing output while still giving
    actionable context.  The full traceback is still available at DEBUG level.
    """
    type_name = type(exc).__name__
    exc_str = str(exc).strip()

    # ── Network / transport layer (httpx / httpcore) ──────────────────────────
    if any(
        t in type_name for t in ("ReadTimeout", "WriteTimeout", "PoolTimeout", "ConnectTimeout")
    ):
        return (
            "Request timed out — the provider took too long to respond. You can try again.",
            True,
        )
    if any(t in type_name for t in ("ConnectError", "ConnectionError", "NetworkError")):
        return (
            "Could not connect to the provider — check that the server URL is correct"
            " and the service is running.",
            True,
        )
    if any(t in type_name for t in ("RemoteProtocolError", "LocalProtocolError")):
        return (
            f"Provider returned an unexpected response ({type_name}). You can try again.",
            True,
        )

    # ── Provider-level errors (Anthropic / OpenAI SDK, etc.) ─────────────────
    if any(
        t in type_name for t in ("AuthenticationError", "PermissionDeniedError", "PermissionDenied")
    ):
        return "Authentication failed — check your API key.", False
    if "RateLimitError" in type_name:
        return "Rate limit exceeded — wait a moment, then try again.", True
    if any(t in type_name for t in ("APIStatusError", "HTTPStatusError")):
        detail = exc_str or type_name
        return f"Provider returned an error: {detail}", True

    # ── Fallback ──────────────────────────────────────────────────────────────
    detail = exc_str or type_name
    return f"Provider error ({type_name}): {detail}", False


def _parse_stop(reason: str) -> StopReason:
    try:
        return StopReason(reason)
    except ValueError:
        return StopReason.END_TURN
