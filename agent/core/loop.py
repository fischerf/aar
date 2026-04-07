"""Thin core loop — the heart of the agent runtime."""

from __future__ import annotations

import asyncio
import logging
import time

from agent.core.config import AgentConfig
from agent.core.events import (
    AssistantMessage,
    ErrorEvent,
    StopReason,
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

            # Provider call — timed, with retry for recoverable errors
            t_provider = time.monotonic()
            response: ProviderResponse | None = None
            for attempt in range(1, config.max_retries + 1):
                try:
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
