"""Provider interface — all LLM adapters implement this."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from agent.core.config import ProviderConfig
from agent.core.events import AssistantMessage, ProviderMeta, ReasoningBlock, ToolCall


@dataclass
class ProviderResponse:
    """Normalized response from any provider."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""
    reasoning: list[ReasoningBlock] = field(default_factory=list)
    meta: ProviderMeta | None = None


@dataclass
class StreamDelta:
    """A single streaming chunk."""

    text: str = ""
    tool_call_delta: dict[str, Any] | None = None
    reasoning_delta: str = ""
    done: bool = False


@dataclass
class ProviderCapabilities:
    """Introspectable summary of what a provider supports."""

    name: str = ""
    tools: bool = True
    reasoning: bool = False
    streaming: bool = True
    structured_output: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tools": self.tools,
            "reasoning": self.reasoning,
            "streaming": self.streaming,
            "structured_output": self.structured_output,
        }


class Provider(ABC):
    """Abstract base for LLM providers."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    def supports_tools(self) -> bool:
        return True

    @property
    def supports_reasoning(self) -> bool:
        return False

    @property
    def supports_streaming(self) -> bool:
        return True

    @property
    def supports_structured_output(self) -> bool:
        return False

    def capabilities(self) -> ProviderCapabilities:
        """Return a snapshot of this provider's capabilities."""
        return ProviderCapabilities(
            name=self.name,
            tools=self.supports_tools,
            reasoning=self.supports_reasoning,
            streaming=self.supports_streaming,
            structured_output=self.supports_structured_output,
        )

    @abstractmethod
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
    ) -> ProviderResponse:
        """Send messages and return a complete response."""
        ...

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
    ) -> AsyncIterator[StreamDelta]:
        """Stream response deltas. Default falls back to complete()."""
        response = await self.complete(messages, tools, system)
        yield StreamDelta(text=response.content, done=True)
