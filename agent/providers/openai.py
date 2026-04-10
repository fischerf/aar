"""OpenAI provider adapter (GPT-4o, o1, o3, etc.)."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from agent.core.config import ProviderConfig
from agent.core.events import ProviderMeta, StopReason, ToolCall
from agent.providers.base import Provider, ProviderResponse, StreamDelta


class OpenAIProvider(Provider):
    """Adapter for the OpenAI Chat Completions API."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        try:
            import openai
        except ImportError:
            raise ImportError(
                "The 'openai' package is required. Install with: pip install aar-agent[openai]"
            )
        kwargs: dict[str, Any] = {}
        if config.api_key:
            kwargs["api_key"] = config.api_key
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self._client = openai.AsyncOpenAI(**kwargs)

    @property
    def name(self) -> str:
        return "openai"

    @property
    def supports_reasoning(self) -> bool:
        # o1/o3 models support reasoning, but it's surfaced differently
        model = self.config.model.lower()
        return model.startswith(("o1", "o3"))

    @property
    def supports_structured_output(self) -> bool:
        return True

    @property
    def supports_vision(self) -> bool:
        # GPT-4o, GPT-4-vision, and similar models support image input.
        # Override via extra={"supports_vision": True/False}.
        if "supports_vision" in self.config.extra:
            return bool(self.config.extra["supports_vision"])
        model = self.config.model.lower()
        return (
            "vision" in model
            or "4o" in model
            or model.startswith("gpt-4")
            or model.startswith("o1")
            or model.startswith("o3")
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
    ) -> ProviderResponse:
        # Build the message list with system prompt prepended
        api_messages = _build_messages(messages, system)

        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": api_messages,
        }

        # Convert tool schemas to OpenAI function-calling format
        if tools:
            kwargs["tools"] = _convert_tools(tools)

        if self.config.max_tokens:
            kwargs["max_tokens"] = self.config.max_tokens
        if self.config.temperature > 0:
            kwargs["temperature"] = self.config.temperature

        # Structured output
        fmt = self.config.response_format
        if fmt == "json":
            kwargs["response_format"] = {"type": "json_object"}
        elif fmt == "json_schema" and self.config.json_schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": self.config.json_schema,
            }

        kwargs.update(self.config.extra)

        response = await self._client.chat.completions.create(**kwargs)

        choice = response.choices[0]
        message = choice.message

        # Parse tool calls
        tool_calls: list[ToolCall] = []
        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    arguments = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    arguments = {"raw": tc.function.arguments}
                tool_calls.append(
                    ToolCall(
                        tool_name=tc.function.name,
                        tool_call_id=tc.id,
                        arguments=arguments,
                    )
                )

        stop_reason = _map_stop_reason(choice.finish_reason)

        # Build usage metadata
        usage: dict[str, int] = {}
        if response.usage:
            usage = {
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        meta = ProviderMeta(
            provider="openai",
            model=response.model,
            usage=usage,
            request_id=response.id or "",
        )

        return ProviderResponse(
            content=message.content or "",
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            meta=meta,
        )

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
    ) -> AsyncIterator[StreamDelta]:
        """Stream response deltas using the OpenAI SDK's streaming API."""
        api_messages = _build_messages(messages, system)

        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": api_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        if tools:
            kwargs["tools"] = _convert_tools(tools)
        if self.config.max_tokens:
            kwargs["max_tokens"] = self.config.max_tokens
        if self.config.temperature > 0:
            kwargs["temperature"] = self.config.temperature

        # Structured output
        fmt = self.config.response_format
        if fmt == "json":
            kwargs["response_format"] = {"type": "json_object"}
        elif fmt == "json_schema" and self.config.json_schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": self.config.json_schema,
            }

        kwargs.update(self.config.extra)

        # Accumulators for tool call fragments
        tool_acc: dict[int, dict[str, str]] = {}
        stream_usage: dict[str, int] = {}

        stream_resp = await self._client.chat.completions.create(**kwargs)

        async for chunk in stream_resp:
            # Capture usage from the final chunk (when stream_options.include_usage=True)
            if hasattr(chunk, "usage") and chunk.usage:
                stream_usage = {
                    "input_tokens": chunk.usage.prompt_tokens or 0,
                    "output_tokens": chunk.usage.completion_tokens or 0,
                    "total_tokens": chunk.usage.total_tokens or 0,
                }

            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            delta = choice.delta

            # Text delta
            text = delta.content or "" if delta else ""
            if text:
                yield StreamDelta(text=text)

            # Tool call argument fragments
            if delta and delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_acc:
                        tool_acc[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc_delta.id:
                        tool_acc[idx]["id"] = tc_delta.id
                    if tc_delta.function and tc_delta.function.name:
                        tool_acc[idx]["name"] = tc_delta.function.name
                    if tc_delta.function and tc_delta.function.arguments:
                        tool_acc[idx]["arguments"] += tc_delta.function.arguments

            # Finish
            if choice.finish_reason:
                for acc in tool_acc.values():
                    try:
                        parsed_args = json.loads(acc["arguments"] or "{}")
                    except (json.JSONDecodeError, TypeError):
                        parsed_args = {"raw": acc["arguments"]}
                    yield StreamDelta(
                        tool_call_delta={
                            "tool_call_id": acc["id"],
                            "tool_name": acc["name"],
                            "arguments": parsed_args,
                        }
                    )

        # Build meta from captured usage (arrives after finish_reason)
        stream_meta: ProviderMeta | None = None
        if stream_usage:
            stream_meta = ProviderMeta(
                provider="openai",
                model=self.config.model,
                usage=stream_usage,
            )
        yield StreamDelta(done=True, meta=stream_meta)


def _build_messages(messages: list[dict[str, Any]], system: str) -> list[dict[str, Any]]:
    """Convert internal messages to OpenAI Chat Completions format.

    Multimodal user messages (those whose ``content`` is a list of blocks)
    are forwarded as a content array so that ``image_url`` blocks reach the
    model intact.  A list that contains only a single text block is unwrapped
    back to a plain string to keep the wire format clean for text-only turns.
    """
    api_messages: list[dict[str, Any]] = []

    if system:
        api_messages.append({"role": "system", "content": system})

    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if isinstance(content, str):
            api_messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            # Handle structured content blocks
            if role == "assistant":
                api_msg = _convert_assistant_blocks(content)
                api_messages.append(api_msg)
            elif role == "user":
                # Separate tool-result blocks from regular content blocks.
                tool_results = [b for b in content if b.get("type") == "tool_result"]
                if tool_results:
                    for tr in tool_results:
                        api_messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tr["tool_use_id"],
                                "content": tr.get("content", ""),
                            }
                        )
                else:
                    # Build an OpenAI-compatible content array, preserving
                    # image_url blocks alongside text blocks.
                    oai_parts: list[dict[str, Any]] = []
                    for b in content:
                        btype = b.get("type")
                        if btype == "text":
                            oai_parts.append({"type": "text", "text": b["text"]})
                        elif btype == "image_url":
                            # Already in the correct OpenAI format; pass through.
                            oai_parts.append(b)
                        # Unknown block types are silently dropped.

                    if not oai_parts:
                        continue

                    # Unwrap single-text-block lists to a plain string for
                    # cleaner serialization when there are no images.
                    if len(oai_parts) == 1 and oai_parts[0].get("type") == "text":
                        api_messages.append({"role": "user", "content": oai_parts[0]["text"]})
                    else:
                        api_messages.append({"role": "user", "content": oai_parts})

    return api_messages


def _convert_assistant_blocks(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert internal assistant content blocks to OpenAI format."""
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for block in blocks:
        if block.get("type") == "text":
            text_parts.append(block["text"])
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": block["id"],
                    "type": "function",
                    "function": {
                        "name": block["name"],
                        "arguments": json.dumps(block.get("input", {})),
                    },
                }
            )

    msg: dict[str, Any] = {"role": "assistant"}
    msg["content"] = " ".join(text_parts) if text_parts else None
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert internal tool schemas to OpenAI function-calling format."""
    openai_tools = []
    for tool in tools:
        openai_tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
        )
    return openai_tools


def _map_stop_reason(reason: str | None) -> str:
    mapping = {
        "stop": StopReason.END_TURN,
        "tool_calls": StopReason.TOOL_USE,
        "length": StopReason.MAX_TOKENS,
    }
    if reason and reason in mapping:
        return mapping[reason].value
    return reason or StopReason.END_TURN.value
