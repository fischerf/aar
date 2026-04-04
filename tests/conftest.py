"""Shared fixtures and mock provider for the test suite."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", default=False, help="Run live tests against real providers")


def pytest_configure(config):
    config.addinivalue_line("markers", "live: mark test as requiring a live provider (skipped by default)")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--live"):
        skip_live = pytest.mark.skip(reason="Pass --live to run live provider tests")
        for item in items:
            if item.get_closest_marker("live"):
                item.add_marker(skip_live)

from agent.core.config import AgentConfig, ProviderConfig, SafetyConfig, ToolConfig
from agent.core.events import (
    ProviderMeta,
    ToolCall,
)
from agent.core.session import Session
from agent.providers.base import Provider, ProviderResponse
from agent.tools.execution import ToolExecutor
from agent.tools.registry import ToolRegistry
from agent.tools.schema import SideEffect, ToolSpec


# ---------------------------------------------------------------------------
# Mock provider — the backbone of all non-live tests
# ---------------------------------------------------------------------------

class MockProvider(Provider):
    """Programmable mock provider for deterministic testing.

    Queue responses with `enqueue()` and they'll be returned in order.
    Tracks all calls for assertion.
    """

    def __init__(self, config: ProviderConfig | None = None) -> None:
        super().__init__(config or ProviderConfig(name="mock", model="mock-1"))
        self._responses: list[ProviderResponse] = []
        self.call_history: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "mock"

    @property
    def supports_reasoning(self) -> bool:
        return True

    def enqueue(self, *responses: ProviderResponse) -> None:
        """Add one or more responses to the queue."""
        self._responses.extend(responses)

    def enqueue_text(self, text: str, stop: str = "end_turn") -> None:
        """Shortcut: enqueue a plain text response."""
        self._responses.append(
            ProviderResponse(
                content=text,
                stop_reason=stop,
                meta=ProviderMeta(
                    provider="mock",
                    model="mock-1",
                    usage={"input_tokens": 10, "output_tokens": 5},
                ),
            )
        )

    def enqueue_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        tool_call_id: str = "tc_mock_1",
        text: str = "",
    ) -> None:
        """Shortcut: enqueue a response containing a tool call."""
        self._responses.append(
            ProviderResponse(
                content=text,
                tool_calls=[
                    ToolCall(
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                        arguments=arguments,
                    )
                ],
                stop_reason="tool_use",
                meta=ProviderMeta(
                    provider="mock",
                    model="mock-1",
                    usage={"input_tokens": 10, "output_tokens": 15},
                ),
            )
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
    ) -> ProviderResponse:
        self.call_history.append({
            "messages": messages,
            "tools": tools,
            "system": system,
        })
        if not self._responses:
            raise RuntimeError("MockProvider has no more queued responses")
        return self._responses.pop(0)


class ErrorProvider(Provider):
    """Provider that always raises an exception."""

    def __init__(self, error: Exception | None = None) -> None:
        super().__init__(ProviderConfig(name="error", model="error-1"))
        self._error = error or RuntimeError("Provider exploded")

    @property
    def name(self) -> str:
        return "error"

    async def complete(self, messages, tools=None, system=""):
        raise self._error


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_provider() -> MockProvider:
    return MockProvider()


@pytest.fixture
def error_provider() -> ErrorProvider:
    return ErrorProvider()


@pytest.fixture
def tool_registry() -> ToolRegistry:
    """Registry with a simple echo tool pre-registered."""
    reg = ToolRegistry()

    async def echo(message: str) -> str:
        return f"echo: {message}"

    reg.add(ToolSpec(
        name="echo",
        description="Echoes the input",
        input_schema={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
        side_effects=[SideEffect.NONE],
        handler=echo,
    ))
    return reg


@pytest.fixture
def slow_tool_registry() -> ToolRegistry:
    """Registry with a tool that takes a long time."""
    reg = ToolRegistry()

    async def slow_tool(seconds: int = 10) -> str:
        await asyncio.sleep(seconds)
        return "done"

    reg.add(ToolSpec(
        name="slow_tool",
        description="Sleeps for a while",
        input_schema={
            "type": "object",
            "properties": {"seconds": {"type": "integer"}},
            "required": [],
        },
        side_effects=[SideEffect.NONE],
        handler=slow_tool,
    ))
    return reg


@pytest.fixture
def failing_tool_registry() -> ToolRegistry:
    """Registry with a tool that raises an exception."""
    reg = ToolRegistry()

    async def bad_tool() -> str:
        raise ValueError("something went wrong")

    reg.add(ToolSpec(
        name="bad_tool",
        description="Always fails",
        input_schema={"type": "object", "properties": {}, "required": []},
        side_effects=[SideEffect.NONE],
        handler=bad_tool,
    ))
    return reg


@pytest.fixture
def default_config() -> AgentConfig:
    return AgentConfig(
        provider=ProviderConfig(name="mock", model="mock-1"),
        max_steps=10,
        timeout=30.0,
    )


@pytest.fixture
def tool_executor(tool_registry: ToolRegistry) -> ToolExecutor:
    return ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())


@pytest.fixture
def session() -> Session:
    return Session()


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)
