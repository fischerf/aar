"""Anthropic Claude provider adapter."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from agent.core.config import ProviderConfig
from agent.core.events import ProviderMeta, ReasoningBlock, StopReason, ToolCall
from agent.providers.base import FRAMEWORK_EXTRA_KEYS, Provider, ProviderResponse, StreamDelta
from agent.providers.errors import translate_provider_errors

logger = logging.getLogger(__name__)

# Extra keys that enable prompt caching when set in provider config
_CACHE_EXTRA_KEY = "prompt_caching"


def _apply_prompt_caching(
    kwargs: dict[str, Any],
    enabled: bool,
) -> None:
    """Rewrite ``system`` and ``tools`` in *kwargs* for Anthropic prompt caching.

    Anthropic caches everything from the start of the request up to a
    ``cache_control`` breakpoint.  We mark two breakpoints:

    1. The **last system content block** — caches the full system prompt.
    2. The **last tool definition** — caches the tool schemas too.

    On turn 2+ the API returns ``cache_read_input_tokens`` instead of
    re-processing the prefix, cutting input costs by ~90% for the static part.

    Enable via ``config.json``::

        "extra": { "prompt_caching": true }

    #8 — Never mutates caller-owned lists/dicts. ``tools`` is the same list
    object the caller passed to ``complete()`` / ``stream()`` (re-shared
    across turns via the registry); the previous in-place ``tools[-1] = ...``
    leaked a ``cache_control`` marker onto the registry's schema, which then
    showed up in subsequent providers (e.g. after ``/model openai``) where
    the field is invalid. Same story for the system block list. We now build
    fresh top-level lists; the inner dicts are still shallow-copied via
    spread before the marker is attached.
    """
    if not enabled:
        return

    cache_marker = {"type": "ephemeral"}

    # System prompt: convert plain string → list-of-blocks with cache marker
    system = kwargs.get("system")
    if isinstance(system, str) and system:
        kwargs["system"] = [{"type": "text", "text": system, "cache_control": cache_marker}]
    elif isinstance(system, list) and system:
        # Already a list of blocks — build a fresh list with the last block
        # carrying the marker. Don't mutate the caller's list.
        new_system = list(system)
        new_system[-1] = {**new_system[-1], "cache_control": cache_marker}
        kwargs["system"] = new_system

    # Tools: mark the last tool so the entire tools array is cached.
    # Build a fresh list — the caller's list is shared with the registry.
    tools = kwargs.get("tools")
    if tools:
        new_tools = list(tools)
        new_tools[-1] = {**new_tools[-1], "cache_control": cache_marker}
        kwargs["tools"] = new_tools


class AnthropicProvider(Provider):
    """Adapter for the Anthropic Messages API."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        try:
            import anthropic
        except ImportError:
            raise ImportError(
                "The 'anthropic' package is required. Install with: pip install aar-agent[anthropic]"
            )
        timeout: float | None = config.extra.get("timeout", None)
        self._client = anthropic.AsyncAnthropic(
            api_key=config.api_key or None,
            base_url=config.base_url or None,
            timeout=timeout,
        )

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def supports_reasoning(self) -> bool:
        return True

    @property
    def supports_vision(self) -> bool:
        # All modern Claude models (claude-3+) support image input.
        return True

    @translate_provider_errors
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
    ) -> ProviderResponse:
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "messages": _convert_messages_for_anthropic(messages),
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        if self.config.temperature > 0:
            kwargs["temperature"] = self.config.temperature
        kwargs.update({k: v for k, v in self.config.extra.items() if k not in FRAMEWORK_EXTRA_KEYS})

        _apply_prompt_caching(kwargs, bool(self.config.extra.get(_CACHE_EXTRA_KEY)))

        response = await self._client.messages.create(**kwargs)

        # Parse response
        content_text = ""
        tool_calls: list[ToolCall] = []
        reasoning_blocks: list[ReasoningBlock] = []

        for block in response.content:
            if block.type == "text":
                content_text += block.text
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        tool_name=block.name,
                        tool_call_id=block.id,
                        arguments=block.input if isinstance(block.input, dict) else {},
                    )
                )
            elif block.type == "thinking":
                reasoning_blocks.append(ReasoningBlock(content=block.thinking))

        # Map stop reason
        stop_reason = _map_stop_reason(response.stop_reason)

        usage: dict[str, int] = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        # Capture cache tokens when available (prompt caching)
        if hasattr(response.usage, "cache_read_input_tokens"):
            cache_read = response.usage.cache_read_input_tokens
            if cache_read:
                usage["cache_read_tokens"] = cache_read
        if hasattr(response.usage, "cache_creation_input_tokens"):
            cache_write = response.usage.cache_creation_input_tokens
            if cache_write:
                usage["cache_write_tokens"] = cache_write

        meta = ProviderMeta(
            provider="anthropic",
            model=response.model,
            usage=usage,
            request_id=response.id,
        )

        return ProviderResponse(
            content=content_text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            reasoning=reasoning_blocks,
            meta=meta,
        )

    @translate_provider_errors
    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
    ) -> AsyncIterator[StreamDelta]:
        """Stream response deltas using the Anthropic SDK's streaming API.

        Anthropic streams typed events: ``content_block_start``,
        ``content_block_delta``, ``content_block_stop``, and ``message_stop``.
        Text and thinking blocks are emitted as deltas; tool_use blocks are
        accumulated and emitted at the end.
        """
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "messages": _convert_messages_for_anthropic(messages),
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        if self.config.temperature > 0:
            kwargs["temperature"] = self.config.temperature
        kwargs.update({k: v for k, v in self.config.extra.items() if k not in FRAMEWORK_EXTRA_KEYS})

        _apply_prompt_caching(kwargs, bool(self.config.extra.get(_CACHE_EXTRA_KEY)))

        # Track active content blocks by index
        active_blocks: dict[int, dict[str, Any]] = {}
        emitted_done = False

        def _flush_tool_calls() -> list[StreamDelta]:
            """Build StreamDelta(s) for every accumulated tool_use block."""
            out: list[StreamDelta] = []
            for block_info in active_blocks.values():
                if block_info.get("type") != "tool_use":
                    continue
                raw_args = block_info.get("arguments", "{}")
                try:
                    parsed_args = json.loads(raw_args) if raw_args else {}
                except (json.JSONDecodeError, TypeError):
                    parsed_args = {"raw": raw_args}
                out.append(
                    StreamDelta(
                        tool_call_delta={
                            "tool_call_id": block_info.get("id", ""),
                            "tool_name": block_info.get("name", ""),
                            "arguments": parsed_args,
                        }
                    )
                )
            return out

        stream_cm = self._client.messages.stream(**kwargs)
        async with stream_cm as stream:
            async for event in stream:
                event_type = event.type

                if event_type == "content_block_start":
                    idx = event.index
                    block = event.content_block
                    active_blocks[idx] = {"type": block.type}
                    if block.type == "tool_use":
                        active_blocks[idx]["id"] = block.id
                        active_blocks[idx]["name"] = block.name
                        active_blocks[idx]["arguments"] = ""

                elif event_type == "content_block_delta":
                    idx = event.index
                    delta = event.delta

                    if delta.type == "text_delta":
                        yield StreamDelta(text=delta.text)
                    elif delta.type == "thinking_delta":
                        yield StreamDelta(reasoning_delta=delta.thinking)
                    elif delta.type == "input_json_delta":
                        if idx in active_blocks:
                            active_blocks[idx]["arguments"] += delta.partial_json

                elif event_type == "message_stop":
                    for delta in _flush_tool_calls():
                        yield delta
                    # Build usage metadata from the final message
                    stream_meta: ProviderMeta | None = None
                    try:
                        final_msg = await stream.get_final_message()
                        usage: dict[str, int] = {
                            "input_tokens": final_msg.usage.input_tokens,
                            "output_tokens": final_msg.usage.output_tokens,
                        }
                        if hasattr(final_msg.usage, "cache_read_input_tokens"):
                            cache_read = final_msg.usage.cache_read_input_tokens
                            if cache_read:
                                usage["cache_read_tokens"] = cache_read
                        if hasattr(final_msg.usage, "cache_creation_input_tokens"):
                            cache_write = final_msg.usage.cache_creation_input_tokens
                            if cache_write:
                                usage["cache_write_tokens"] = cache_write
                        stream_meta = ProviderMeta(
                            provider="anthropic",
                            model=final_msg.model,
                            usage=usage,
                            request_id=final_msg.id,
                        )
                    except Exception:
                        # Don't silently swallow — surfaces SDK breakage in logs. (#6)
                        logger.debug("Failed to build Anthropic stream meta", exc_info=True)
                    yield StreamDelta(done=True, meta=stream_meta)
                    emitted_done = True
                    return

        # Stream exited normally without a message_stop (older SDKs / early
        # close): flush any accumulated tool calls + terminal sentinel so the
        # consumer doesn't lose them. Errors propagate as exceptions instead. (#6)
        if not emitted_done:
            for delta in _flush_tool_calls():
                yield delta
            yield StreamDelta(done=True)


def _convert_messages_for_anthropic(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert internal OpenAI-style messages to Anthropic wire format.

    The only transformation needed is for ``image_url`` content blocks, which
    Anthropic represents as ``{"type": "image", "source": {...}}`` rather than
    ``{"type": "image_url", "image_url": {"url": "..."}}``.

    * HTTP/HTTPS URLs  → ``{"type": "url", "url": "..."}``
    * ``data:`` URIs   → ``{"type": "base64", "media_type": "...", "data": "..."}``

    All other blocks (``text``, ``tool_use``, ``tool_result``) pass through
    unchanged.
    """
    result: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            result.append(msg)
            continue

        converted: list[dict[str, Any]] = []
        for block in content:
            if block.get("type") == "image_url":
                url_obj = block.get("image_url", {})
                url: str = url_obj.get("url", "")
                if url.startswith("data:"):
                    # data:<media_type>;base64,<payload>
                    try:
                        meta_part, b64_data = url.split(",", 1)
                        media_type = meta_part.split(":")[1].split(";")[0]
                    except (IndexError, ValueError):
                        media_type = "image/jpeg"
                        b64_data = url
                    converted.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64_data,
                            },
                        }
                    )
                else:
                    converted.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "url",
                                "url": url,
                            },
                        }
                    )
            else:
                converted.append(block)

        result.append({"role": msg["role"], "content": converted})

    return result


def _map_stop_reason(reason: str | None) -> str:
    mapping = {
        "end_turn": StopReason.END_TURN,
        "tool_use": StopReason.TOOL_USE,
        "max_tokens": StopReason.MAX_TOKENS,
    }
    if reason and reason in mapping:
        return mapping[reason].value
    return reason or StopReason.END_TURN.value
