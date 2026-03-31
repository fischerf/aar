"""OpenAI provider adapter (GPT-4o, o1, o3, etc.)."""

from __future__ import annotations

import json
from typing import Any

from agent.core.config import ProviderConfig
from agent.core.events import ProviderMeta, StopReason, ToolCall
from agent.providers.base import Provider, ProviderResponse


class OpenAIProvider(Provider):
    """Adapter for the OpenAI Chat Completions API."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        try:
            import openai
        except ImportError:
            raise ImportError(
                "The 'openai' package is required. Install with: pip install epa-agent[openai]"
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


def _build_messages(messages: list[dict[str, Any]], system: str) -> list[dict[str, Any]]:
    """Convert Anthropic-style messages to OpenAI format."""
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
                # Could be tool results or text
                tool_results = [b for b in content if b.get("type") == "tool_result"]
                if tool_results:
                    for tr in tool_results:
                        api_messages.append({
                            "role": "tool",
                            "tool_call_id": tr["tool_use_id"],
                            "content": tr.get("content", ""),
                        })
                else:
                    # Plain text blocks
                    text = " ".join(
                        b.get("text", "") for b in content if b.get("type") == "text"
                    )
                    api_messages.append({"role": "user", "content": text})

    return api_messages


def _convert_assistant_blocks(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert Anthropic-style assistant content blocks to OpenAI format."""
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for block in blocks:
        if block.get("type") == "text":
            text_parts.append(block["text"])
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block["id"],
                "type": "function",
                "function": {
                    "name": block["name"],
                    "arguments": json.dumps(block.get("input", {})),
                },
            })

    msg: dict[str, Any] = {"role": "assistant"}
    msg["content"] = " ".join(text_parts) if text_parts else None
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic tool schemas to OpenAI function-calling format."""
    openai_tools = []
    for tool in tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
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
