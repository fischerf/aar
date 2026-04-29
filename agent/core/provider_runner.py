"""Provider request machinery for the core loop.

Extracted from :mod:`agent.core.loop` to keep the main runtime thin. Handles:

- Retry loop with exponential backoff for recoverable errors
- Streaming response consumption (provider deltas → ``ProviderResponse``)
- Translation of provider exceptions to user-friendly messages

The core loop imports :func:`provider_request` and uses the :class:`ProviderRequestFailed`
sentinel to know when to return early after an unrecoverable provider error.
"""

from __future__ import annotations

import asyncio
import random
import time

from agent.core.config import AgentConfig
from agent.core.events import (
    ErrorEvent,
    ProviderMeta,
    ReasoningBlock,
    StopReason,
    StreamChunk,
    ToolCall,
)
from agent.core.loop_helpers import emit
from agent.core.session import Session
from agent.core.state import AgentState
from agent.providers.base import Provider, ProviderResponse


class ProviderRequestFailed(RuntimeError):
    """Sentinel exception: provider request gave up after retries.

    The core loop catches this to return the updated session instead of
    letting the exception bubble up.
    """


async def provider_request(
    *,
    provider: Provider,
    messages: list[dict],
    tool_schemas: list[dict] | None,
    system_prompt: str,
    session: Session,
    on_event,
    config: AgentConfig,
    use_streaming: bool,
    log,
    log_extra: dict[str, str],
) -> tuple[ProviderResponse, float]:
    """Run a provider request with retry logic and return response plus duration."""
    t_provider = time.monotonic()
    response: ProviderResponse | None = None
    for attempt in range(1, config.max_retries + 1):
        try:
            if use_streaming:
                response = await _consume_stream(
                    provider, messages, tool_schemas, system_prompt, session, on_event
                )
            else:
                response = await provider.complete(
                    messages=messages, tools=tool_schemas, system=system_prompt
                )
            break
        except Exception as e:
            friendly, recoverable = _provider_error_message(e)
            if recoverable and attempt < config.max_retries:
                delay = 2 ** (attempt - 1)
                log.info(
                    "Recoverable error at step %d (attempt %d/%d), retrying in %ds: %s",
                    session.step_count,
                    attempt,
                    config.max_retries,
                    delay,
                    friendly,
                    extra=log_extra,
                )
                await asyncio.sleep(random.uniform(0.5, 1.5) * delay)
                continue
            log.warning(
                "Provider error at step %d: %s",
                session.step_count,
                friendly,
                extra=log_extra,
            )
            log.debug("Provider error detail", exc_info=True, extra=log_extra)
            session.state = AgentState.ERROR
            emit(session, on_event, ErrorEvent(message=friendly, recoverable=recoverable))
            raise ProviderRequestFailed from e
    if response is None:
        raise RuntimeError("Provider returned no response after retries")
    return response, (time.monotonic() - t_provider) * 1000


async def _consume_stream(
    provider: Provider,
    messages: list,
    tools: list | None,
    system: str,
    session: Session,
    on_event,
) -> ProviderResponse:
    """Consume a provider stream, emit StreamChunk events, return assembled response.

    Always emits exactly one ``StreamChunk(finished=True)`` — including when the
    stream raises mid-iteration or ends without a ``done=True`` delta — so
    downstream consumers (SSE transports, TUI streams) never hang waiting for
    a stream-end marker.
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    stop_reason = ""
    meta: ProviderMeta | None = None
    saw_done = False

    try:
        async for delta in provider.stream(messages=messages, tools=tools, system=system):
            if delta.text or delta.reasoning_delta:
                emit(
                    session,
                    on_event,
                    StreamChunk(text=delta.text, reasoning_text=delta.reasoning_delta),
                )

            if delta.text:
                content_parts.append(delta.text)
            if delta.reasoning_delta:
                reasoning_parts.append(delta.reasoning_delta)

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
                saw_done = True
                meta = delta.meta
                stop_reason = StopReason.TOOL_USE.value if tool_calls else StopReason.END_TURN.value
                break
    finally:
        # Unconditional stream-end marker. Safe to emit on exception paths too —
        # it means "the stream is over", not "the run succeeded".
        emit(session, on_event, StreamChunk(finished=True))

    if not saw_done:
        # Provider closed its stream without a terminal delta. Default to
        # END_TURN so the loop doesn't spin, but log so we can see it.
        import logging

        logging.getLogger(__name__).warning(
            "Provider stream ended without done=True (received %d text chunks, "
            "%d reasoning chunks, %d tool calls)",
            len(content_parts),
            len(reasoning_parts),
            len(tool_calls),
        )
        stop_reason = StopReason.TOOL_USE.value if tool_calls else StopReason.END_TURN.value

    reasoning_blocks = [ReasoningBlock(content="".join(reasoning_parts))] if reasoning_parts else []

    return ProviderResponse(
        content="".join(content_parts),
        tool_calls=tool_calls,
        stop_reason=stop_reason,
        reasoning=reasoning_blocks,
        meta=meta,
    )


def _provider_error_message(exc: BaseException) -> tuple[str, bool]:
    """Return ``(user_friendly_message, is_recoverable)`` for a provider exception.

    Keeps raw tracebacks out of user-facing output while still giving
    actionable context. The full traceback is still available at DEBUG level.
    """
    type_name = type(exc).__name__
    exc_str = str(exc).strip()

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

    if any(
        t in type_name for t in ("AuthenticationError", "PermissionDeniedError", "PermissionDenied")
    ):
        return "Authentication failed — check your API key.", False
    if "RateLimitError" in type_name:
        return "Rate limit exceeded — wait a moment, then try again.", True
    if any(t in type_name for t in ("APIStatusError", "HTTPStatusError")):
        detail = exc_str or type_name
        return f"Provider returned an error: {detail}", True

    detail = exc_str or type_name
    return f"Provider error ({type_name}): {detail}", False
