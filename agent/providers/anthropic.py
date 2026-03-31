"""Anthropic Claude provider adapter."""

from __future__ import annotations

from typing import Any

from agent.core.config import ProviderConfig
from agent.core.events import ProviderMeta, ReasoningBlock, StopReason, ToolCall
from agent.providers.base import Provider, ProviderResponse


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
        self._client = anthropic.AsyncAnthropic(
            api_key=config.api_key or None,
            base_url=config.base_url or None,
        )

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def supports_reasoning(self) -> bool:
        return True

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
    ) -> ProviderResponse:
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        if self.config.temperature > 0:
            kwargs["temperature"] = self.config.temperature
        kwargs.update(self.config.extra)

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

        meta = ProviderMeta(
            provider="anthropic",
            model=response.model,
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            request_id=response.id,
        )

        return ProviderResponse(
            content=content_text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            reasoning=reasoning_blocks,
            meta=meta,
        )


def _map_stop_reason(reason: str | None) -> str:
    mapping = {
        "end_turn": StopReason.END_TURN,
        "tool_use": StopReason.TOOL_USE,
        "max_tokens": StopReason.MAX_TOKENS,
    }
    if reason and reason in mapping:
        return mapping[reason].value
    return reason or StopReason.END_TURN.value
