"""Thin core loop — the heart of the agent runtime.

Everything that isn't loop control flow lives elsewhere:

- :mod:`agent.core.provider_runner` — retries, streaming, error translation
- :mod:`agent.core.loop_helpers`   — event emission, usage/budget, misc utilities
- :mod:`agent.core.guardrails`     — repetition detection, budget proximity, max-token recovery
"""

from __future__ import annotations

import asyncio
import logging
import time

from agent.core.config import AgentConfig
from agent.core.events import (
    AssistantMessage,
    ContextWindowEvent,
    ErrorEvent,
    SessionEvent,
    StopReason,
    ToolResult,
)
from agent.core.guardrails import LoopGuardrails
from agent.core.loop_helpers import (
    append_internal_user_message,
    apply_usage_and_budget,
    detect_truncated_tool_call,
    emit,
    emit_provider_observation,
    format_refusal,
    parse_stop,
)
from agent.core.provider_runner import ProviderRequestFailed, provider_request
from agent.core.session import (
    Session,
    compact_to_token_budget,
    estimate_token_count,
    trim_to_token_budget,
    truncate_old_tool_results,
)
from agent.core.state import AgentState
from agent.extensions.api import BlockResult
from agent.extensions.manager import ExtensionManager
from agent.providers.base import Provider
from agent.tools.execution import ToolExecutor

logger = logging.getLogger(__name__)


async def run_loop(
    session: Session,
    provider: Provider,
    tool_executor: ToolExecutor,
    config: AgentConfig,
    on_event=None,
    cancel_event: asyncio.Event | None = None,
    extension_manager: ExtensionManager | None = None,
) -> Session:
    """Run the agent loop until completion, max steps, or timeout.

    Args:
        session: The current session with conversation history.
        provider: The LLM provider to use.
        tool_executor: Executor for tool calls.
        config: Agent configuration.
        on_event: Optional callback called with each new event.
        cancel_event: Optional asyncio.Event; set it to request cooperative cancellation.
        extension_manager: Optional extension manager for firing lifecycle hooks.

    Returns:
        The updated session.
    """
    session.state = AgentState.RUNNING
    start_time = time.monotonic()
    done = False
    guardrails = LoopGuardrails(config.guardrails)

    log = logger.getChild("loop")
    log_extra = {"session_id": session.session_id, "trace_id": session.trace_id}

    if extension_manager is not None:
        await extension_manager.fire_event("session_start", SessionEvent(action="started"))

    try:
        while not done and session.step_count < config.max_steps:
            if cancel_event is not None and cancel_event.is_set():
                session.state = AgentState.CANCELLED
                emit(session, on_event, ErrorEvent(message="Agent cancelled", recoverable=False))
                if extension_manager is not None:
                    await extension_manager.fire_event("session_end", SessionEvent(action="ended"))
                return session

            if config.timeout > 0.0 and time.monotonic() - start_time > config.timeout:
                session.state = AgentState.TIMED_OUT
                emit(
                    session,
                    on_event,
                    ErrorEvent(
                        message=f"Agent timed out after {config.timeout}s", recoverable=False
                    ),
                )
                if extension_manager is not None:
                    await extension_manager.fire_event("session_end", SessionEvent(action="ended"))
                return session

            session.increment_step()
            messages = session.to_messages()
            _ctx_window = config.effective_context_window()
            _msgs_before = len(messages)
            if _ctx_window > 0 and config.context_strategy == "summarize":
                if config.compaction.enabled:
                    try:
                        from agent.core.compaction.compaction import compact_session

                        result = await compact_session(
                            session, provider, _ctx_window, config.compaction
                        )
                        if result:
                            messages = session.to_messages()
                            log.info(
                                "Compacted context: %d tokens before, %d events removed",
                                result.tokens_before,
                                result.events_removed,
                                extra=log_extra,
                            )
                    except Exception:
                        log.exception("Compaction failed, falling back to trim", extra=log_extra)
                # Safety net: trim if still over budget (or if compaction disabled)
                messages = trim_to_token_budget(messages, _ctx_window)
            elif _ctx_window > 0 and config.context_strategy == "sliding_window":
                messages = trim_to_token_budget(messages, _ctx_window)
            elif _ctx_window > 0 and config.context_strategy == "compact":
                messages = compact_to_token_budget(messages, _ctx_window)

            # Age old tool results to reduce context growth
            if config.compaction.truncate_old_results:
                messages = truncate_old_tool_results(
                    messages,
                    keep_recent=config.compaction.truncate_keep_recent,
                    max_chars=config.compaction.truncate_max_chars,
                )

            # Emit a context-window fill event so the UI can show a live indicator.
            # Fired unconditionally when a context window is configured so the bar
            # updates every turn, not only when messages are dropped.
            if _ctx_window > 0:
                _ctx_tokens = estimate_token_count(messages)
                emit(
                    session,
                    on_event,
                    ContextWindowEvent(
                        ctx_tokens=_ctx_tokens,
                        ctx_window=_ctx_window,
                        msgs_before=_msgs_before,
                        msgs_after=len(messages),
                        msgs_dropped=max(0, _msgs_before - len(messages)),
                        strategy=config.context_strategy,
                    ),
                )

            if extension_manager is not None:
                await extension_manager.fire_event("before_turn", None)

            tool_schemas = tool_executor.registry.to_provider_schemas() or None
            try:
                response, provider_ms = await provider_request(
                    provider=provider,
                    messages=messages,
                    tool_schemas=tool_schemas,
                    system_prompt=config.system_prompt,
                    session=session,
                    on_event=on_event,
                    config=config,
                    use_streaming=config.streaming and provider.supports_streaming,
                    log=log,
                    log_extra=log_extra,
                )
            except ProviderRequestFailed:
                if extension_manager is not None:
                    await extension_manager.fire_event("session_end", SessionEvent(action="ended"))
                return session

            emit_provider_observation(session, on_event, response, provider_ms)
            if apply_usage_and_budget(session, on_event, response, config):
                if extension_manager is not None:
                    await extension_manager.fire_event("session_end", SessionEvent(action="ended"))
                return session

            if extension_manager is not None:
                await extension_manager.fire_event("after_turn", response)

            _token_budget = config.effective_token_budget()
            _cost_limit = config.effective_cost_limit()
            if guardrails.check_near_budget(session, _token_budget, _cost_limit):
                log.warning(
                    "Near budget at step %d (tokens=%d budget=%d cost=%.4f limit=%.4f)",
                    session.step_count,
                    session.total_tokens,
                    _token_budget,
                    session.total_cost,
                    _cost_limit,
                    extra=log_extra,
                )
                emit(
                    session,
                    on_event,
                    ErrorEvent(
                        message="Approaching budget limit — stopping soon", recoverable=True
                    ),
                )

            log.info(
                "step=%d provider_ms=%.0f tool_calls=%d",
                session.step_count,
                provider_ms,
                len(response.tool_calls),
                extra=log_extra,
            )

            # --- Detect max_tokens-induced tool-argument truncation ---
            # Some providers return stop_reason="tool_use" with truncated,
            # unparsable argument JSON when the response hit the max_tokens
            # cap.  Route this through the same recovery path as a real
            # ``stop_reason="max_tokens"`` event so we don't silently dispatch
            # a broken tool call (and burn the token budget retrying it).
            _max_tokens_cap = config.resolve_provider().max_tokens
            _truncated = detect_truncated_tool_call(response, _max_tokens_cap)
            if _truncated is not None:
                _bad_tc, _raw_payload = _truncated
                _out_tokens = (
                    response.meta.usage.get("output_tokens", 0)
                    if response.meta and response.meta.usage
                    else 0
                )
                _clipped = _raw_payload[:500] + ("…[clipped]" if len(_raw_payload) > 500 else "")
                log.warning(
                    "Truncated tool-call detected at step %d: tool=%s "
                    "output_tokens=%d max_tokens=%d raw=%r",
                    session.step_count,
                    _bad_tc.tool_name,
                    _out_tokens,
                    _max_tokens_cap,
                    _clipped,
                    extra=log_extra,
                )
                # Emit a synthetic AssistantMessage so the rest of the
                # loop machinery (and any persisted session) sees a
                # MAX_TOKENS stop, but DO NOT emit ToolCall events for
                # the broken call — we never want to dispatch it.
                emit(
                    session,
                    on_event,
                    AssistantMessage(content=response.content, stop_reason=StopReason.MAX_TOKENS),
                )
                if guardrails.should_continue_after_max_tokens(session):
                    append_internal_user_message(
                        session,
                        on_event,
                        guardrails.max_tokens_followup(),
                        reason="max_tokens_recovery",
                    )
                    continue
                # Recoveries exhausted — surface a clear, actionable error.
                _err_msg = (
                    f"{_bad_tc.tool_name} argument JSON truncated at "
                    f"output_tokens={_out_tokens} (max_tokens={_max_tokens_cap}); "
                    f"aborting after {guardrails.config.max_tokens_recoveries} recoveries"
                )
                session.state = AgentState.ERROR
                emit(session, on_event, ErrorEvent(message=_err_msg, recoverable=False))
                if extension_manager is not None:
                    await extension_manager.fire_event("session_end", SessionEvent(action="ended"))
                return session

            if response.tool_calls:
                # --- Extension: tool_call filtering ---
                # #2 — Don't mutate ``response.tool_calls`` and don't emit the
                # blocked ToolCall/ToolResult events before the AssistantMessage.
                # ``events_to_messages`` flushes any pending ``ToolResult``s as a
                # ``user(tool_result)`` block when it next sees an
                # ``AssistantMessage``; if we emitted blocked results first, the
                # provider would see ``user(tool_result) → assistant(tool_use)``
                # with the tool_result orphaned from its tool_use, which
                # Anthropic and OpenAI reject with a 400 on the next call.
                #
                # The correct order is:
                #   ToolCall* (all originals, blocked + unblocked)
                #   → AssistantMessage (pairs the tool_use blocks)
                #   → ToolResult* (blocked synthesized first, then executed)
                effective_tool_calls = response.tool_calls
                blocked_results: list[ToolResult] = []
                if extension_manager is not None:
                    effective_tool_calls = []
                    for tc in response.tool_calls:
                        rv = await extension_manager.fire_event("tool_call", tc)
                        if isinstance(rv, BlockResult):
                            blocked_results.append(
                                ToolResult(
                                    tool_call_id=tc.tool_call_id,
                                    tool_name=tc.tool_name,
                                    output=f"Blocked by extension: {rv.reason}",
                                    is_error=True,
                                )
                            )
                        else:
                            effective_tool_calls.append(tc)
                    if not effective_tool_calls:
                        # All tool calls were blocked. Still emit every original
                        # tool_call + the AssistantMessage that pairs with them,
                        # then the synthesized blocked results, so the next
                        # provider request is well-formed.
                        for tc in response.tool_calls:
                            spec = tool_executor.registry.get(tc.tool_name)
                            if spec:
                                tc.data["side_effects"] = [e.value for e in spec.side_effects]
                            emit(session, on_event, tc)
                        emit(
                            session,
                            on_event,
                            AssistantMessage(
                                content=response.content, stop_reason=StopReason.TOOL_USE
                            ),
                        )
                        for tr in blocked_results:
                            emit(session, on_event, tr)
                        continue

                guardrails.observe_tool_calls(session, effective_tool_calls)
                if guardrails.is_stuck(session):
                    log.warning(
                        "Repetition guard triggered at step %d",
                        session.step_count,
                        extra=log_extra,
                    )
                    emit(
                        session,
                        on_event,
                        ErrorEvent(
                            message=(
                                "Agent stuck in a loop — same tool calls repeated too many times"
                            ),
                            recoverable=False,
                        ),
                    )
                    session.state = AgentState.ERROR
                    if extension_manager is not None:
                        await extension_manager.fire_event(
                            "session_end", SessionEvent(action="ended")
                        )
                    return session

                # Emit ToolCall events BEFORE AssistantMessage so that
                # session.to_messages() sees the correct order:
                #   ToolCall… → AssistantMessage → ToolResult…
                # and can bundle the tool_calls onto the assistant message.
                # Iterate the original ``response.tool_calls`` so blocked
                # tool_uses still appear on the assistant message (paired with
                # the synthesized blocked ToolResult below).
                for tc in response.tool_calls:
                    spec = tool_executor.registry.get(tc.tool_name)
                    if spec:
                        tc.data["side_effects"] = [e.value for e in spec.side_effects]
                    emit(session, on_event, tc)
                emit(
                    session,
                    on_event,
                    AssistantMessage(content=response.content, stop_reason=StopReason.TOOL_USE),
                )

                # Emit blocked tool_results synthesized by the extension before
                # invoking the executor on the unblocked ones, keeping the
                # original tool_use ↔ tool_result pairing order intact.
                for tr in blocked_results:
                    emit(session, on_event, tr)

                session.state = AgentState.WAITING_FOR_TOOL
                results = await tool_executor.execute(effective_tool_calls)

                # --- Extension: tool_result post-processing ---
                if extension_manager is not None:
                    for tr in results:
                        replacement = await extension_manager.fire_event("tool_result", tr)
                        if isinstance(replacement, str):
                            tr.output = replacement

                for tr in results:
                    emit(session, on_event, tr)

                # --- Guardrail: bash→acp_terminal pivot hint ---
                hint = guardrails.observe_tool_results(
                    session, results, set(tool_executor.registry.names())
                )
                if hint:
                    append_internal_user_message(session, on_event, hint, reason="bash_pivot_hint")

                # --- Guardrail: read-only loop nudge ---
                nudge = guardrails.get_read_only_nudge(session)
                if nudge:
                    log.info(
                        "Read-only loop detected at step %d — injecting nudge",
                        session.step_count,
                        extra=log_extra,
                    )
                    append_internal_user_message(
                        session, on_event, nudge, reason="read_only_loop_nudge"
                    )

                session.state = AgentState.RUNNING
                continue

            stop = parse_stop(response.stop_reason)
            assistant_msg = AssistantMessage(content=response.content, stop_reason=stop)
            if response.stop_details:
                assistant_msg.data["stop_details"] = dict(response.stop_details)
            emit(session, on_event, assistant_msg)

            if extension_manager is not None:
                await extension_manager.fire_event("assistant_message", session.events[-1])

            if stop == StopReason.REFUSAL:
                # A provider safety classifier declined the request. Surface it
                # and stop: the response is empty, so without this the
                # premature-end guardrail below would mistake it for the model
                # giving up and burn its recovery turns re-asking a question
                # that cannot be answered.
                _refusal = format_refusal(response.stop_details)
                log.warning("Provider refused the request: %s", _refusal, extra=log_extra)
                emit(
                    session,
                    on_event,
                    ErrorEvent(
                        message=f"Request declined by the provider ({_refusal})",
                        recoverable=False,
                    ),
                )

            if stop == StopReason.MAX_TOKENS and guardrails.should_continue_after_max_tokens(
                session
            ):
                append_internal_user_message(
                    session,
                    on_event,
                    guardrails.max_tokens_followup(),
                    reason="max_tokens_recovery",
                )
                continue

            if stop == StopReason.END_TURN and guardrails.should_continue_after_premature_end(
                session, response.content
            ):
                log.info(
                    "Premature end_turn detected at step %d — injecting continuation",
                    session.step_count,
                    extra=log_extra,
                )
                append_internal_user_message(
                    session,
                    on_event,
                    guardrails.premature_end_followup(),
                    reason="premature_end_recovery",
                )
                continue

            if stop in {StopReason.END_TURN, StopReason.MAX_TOKENS, StopReason.REFUSAL}:
                done = True

        if session.step_count >= config.max_steps and not done:
            session.state = AgentState.MAX_STEPS
            emit(
                session,
                on_event,
                ErrorEvent(message=f"Reached max steps ({config.max_steps})", recoverable=False),
            )

        if session.state == AgentState.RUNNING:
            session.state = AgentState.COMPLETED

        if extension_manager is not None:
            await extension_manager.fire_event("session_end", SessionEvent(action="ended"))

    except asyncio.CancelledError:
        session.state = AgentState.CANCELLED
        emit(session, on_event, ErrorEvent(message="Agent cancelled", recoverable=False))
        if extension_manager is not None:
            await extension_manager.fire_event("session_end", SessionEvent(action="ended"))
        raise

    return session
