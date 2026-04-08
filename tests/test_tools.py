"""Tool system tests — registry, schema inference, execution."""

from __future__ import annotations

import asyncio

import pytest

from agent.core.config import SafetyConfig, ToolConfig
from agent.core.events import ToolCall
from agent.tools.execution import ToolExecutor
from agent.tools.registry import ToolRegistry, _infer_schema
from agent.tools.schema import SideEffect, ToolSpec


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestToolRegistry:
    def test_register_by_decorator(self):
        reg = ToolRegistry()

        @reg.register(name="greet", description="Say hello")
        async def greet(name: str) -> str:
            return f"Hello, {name}!"

        assert "greet" in reg
        assert len(reg) == 1
        spec = reg.get("greet")
        assert spec.name == "greet"
        assert spec.handler is greet

    def test_register_infers_name_from_function(self):
        reg = ToolRegistry()

        @reg.register(description="Add two numbers")
        async def add_numbers(a: int, b: int) -> str:
            return str(a + b)

        assert "add_numbers" in reg

    def test_register_by_add(self):
        reg = ToolRegistry()

        async def handler(x: str) -> str:
            return x

        spec = ToolSpec(name="my_tool", description="test", handler=handler)
        reg.add(spec)
        assert "my_tool" in reg
        assert reg.get("my_tool") is spec

    def test_get_nonexistent(self):
        reg = ToolRegistry()
        assert reg.get("nope") is None

    def test_list_tools(self):
        reg = ToolRegistry()

        async def a() -> str:
            return ""

        async def b() -> str:
            return ""

        reg.add(ToolSpec(name="a", description="a", handler=a))
        reg.add(ToolSpec(name="b", description="b", handler=b))
        tools = reg.list_tools()
        assert len(tools) == 2
        names = {t.name for t in tools}
        assert names == {"a", "b"}

    def test_names(self):
        reg = ToolRegistry()

        async def x() -> str:
            return ""

        reg.add(ToolSpec(name="x", description="x", handler=x))
        assert reg.names() == ["x"]

    def test_to_provider_schemas(self):
        reg = ToolRegistry()

        async def tool(arg: str) -> str:
            return arg

        reg.add(
            ToolSpec(
                name="tool",
                description="A test tool",
                input_schema={
                    "type": "object",
                    "properties": {"arg": {"type": "string"}},
                    "required": ["arg"],
                },
                handler=tool,
            )
        )
        schemas = reg.to_provider_schemas()
        assert len(schemas) == 1
        assert schemas[0]["name"] == "tool"
        assert schemas[0]["description"] == "A test tool"
        assert schemas[0]["input_schema"]["properties"]["arg"]["type"] == "string"

    def test_contains(self):
        reg = ToolRegistry()

        async def x() -> str:
            return ""

        reg.add(ToolSpec(name="x", description="x", handler=x))
        assert "x" in reg
        assert "y" not in reg


# ---------------------------------------------------------------------------
# Schema inference
# ---------------------------------------------------------------------------


class TestSchemaInference:
    def test_infer_from_simple_function(self):
        def func(name: str, count: int) -> str:
            return ""

        schema = _infer_schema(func)
        assert schema["type"] == "object"
        assert schema["properties"]["name"]["type"] == "string"
        assert schema["properties"]["count"]["type"] == "integer"
        assert set(schema["required"]) == {"name", "count"}

    def test_infer_with_defaults(self):
        def func(name: str, verbose: bool = False) -> str:
            return ""

        schema = _infer_schema(func)
        assert "name" in schema["required"]
        assert "verbose" not in schema["required"]

    def test_infer_with_float(self):
        def func(value: float) -> str:
            return ""

        schema = _infer_schema(func)
        assert schema["properties"]["value"]["type"] == "number"

    def test_infer_unannotated_defaults_to_string(self):
        def func(x) -> str:
            return ""

        schema = _infer_schema(func)
        assert schema["properties"]["x"]["type"] == "string"


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------


class TestToolExecution:
    @pytest.mark.asyncio
    async def test_execute_simple_tool(self, tool_executor):
        tc = ToolCall(tool_name="echo", tool_call_id="tc_1", arguments={"message": "hello"})
        results = await tool_executor.execute([tc])
        assert len(results) == 1
        assert results[0].output == "echo: hello"
        assert not results[0].is_error
        assert results[0].tool_call_id == "tc_1"

    @pytest.mark.asyncio
    async def test_execute_unknown_tool(self, tool_executor):
        tc = ToolCall(tool_name="nonexistent", tool_call_id="tc_1", arguments={})
        results = await tool_executor.execute([tc])
        assert results[0].is_error
        assert "unknown tool" in results[0].output.lower()

    @pytest.mark.asyncio
    async def test_execute_multiple_tools(self, tool_executor):
        calls = [
            ToolCall(tool_name="echo", tool_call_id="tc_1", arguments={"message": "a"}),
            ToolCall(tool_name="echo", tool_call_id="tc_2", arguments={"message": "b"}),
        ]
        results = await tool_executor.execute(calls)
        assert len(results) == 2
        assert results[0].output == "echo: a"
        assert results[1].output == "echo: b"

    @pytest.mark.asyncio
    async def test_execute_failing_tool(self):
        reg = ToolRegistry()

        async def fail_tool() -> str:
            raise RuntimeError("intentional error")

        reg.add(
            ToolSpec(
                name="fail",
                description="fails",
                handler=fail_tool,
                input_schema={"type": "object", "properties": {}, "required": []},
            )
        )
        executor = ToolExecutor(reg, ToolConfig(), SafetyConfig())

        tc = ToolCall(tool_name="fail", tool_call_id="tc_1", arguments={})
        results = await executor.execute([tc])
        assert results[0].is_error
        assert "RuntimeError" in results[0].output

    @pytest.mark.asyncio
    async def test_execute_timeout(self):
        reg = ToolRegistry()

        async def slow() -> str:
            await asyncio.sleep(100)
            return "done"

        reg.add(
            ToolSpec(
                name="slow",
                description="slow",
                handler=slow,
                input_schema={"type": "object", "properties": {}, "required": []},
            )
        )
        config = ToolConfig(command_timeout=1)
        executor = ToolExecutor(reg, config, SafetyConfig())

        tc = ToolCall(tool_name="slow", tool_call_id="tc_1", arguments={})
        results = await executor.execute([tc])
        assert results[0].is_error
        assert "timed out" in results[0].output.lower()

    @pytest.mark.asyncio
    async def test_execute_sync_handler(self):
        """Sync (non-async) handlers should work via to_thread."""
        reg = ToolRegistry()

        def sync_tool(x: str) -> str:
            return f"sync: {x}"

        reg.add(
            ToolSpec(
                name="sync",
                description="sync tool",
                handler=sync_tool,
                input_schema={
                    "type": "object",
                    "properties": {"x": {"type": "string"}},
                    "required": ["x"],
                },
            )
        )
        executor = ToolExecutor(reg, ToolConfig(), SafetyConfig())

        tc = ToolCall(tool_name="sync", tool_call_id="tc_1", arguments={"x": "hello"})
        results = await executor.execute([tc])
        assert results[0].output == "sync: hello"

    @pytest.mark.asyncio
    async def test_execute_truncates_long_output(self):
        reg = ToolRegistry()

        async def verbose() -> str:
            return "x" * 100_000

        reg.add(
            ToolSpec(
                name="verbose",
                description="lots of output",
                handler=verbose,
                input_schema={"type": "object", "properties": {}, "required": []},
            )
        )
        config = ToolConfig(max_output_chars=100)
        executor = ToolExecutor(reg, config, SafetyConfig())

        tc = ToolCall(tool_name="verbose", tool_call_id="tc_1", arguments={})
        results = await executor.execute([tc])
        assert len(results[0].output) < 200
        assert "truncated" in results[0].output.lower()

    @pytest.mark.asyncio
    async def test_tool_with_no_handler(self):
        reg = ToolRegistry()
        reg.add(
            ToolSpec(
                name="empty",
                description="no handler",
                handler=None,
                input_schema={"type": "object", "properties": {}, "required": []},
            )
        )
        executor = ToolExecutor(reg, ToolConfig(), SafetyConfig())

        tc = ToolCall(tool_name="empty", tool_call_id="tc_1", arguments={})
        results = await executor.execute([tc])
        assert results[0].is_error
        assert "no handler" in results[0].output.lower()


# ---------------------------------------------------------------------------
# ToolSpec
# ---------------------------------------------------------------------------


class TestToolSpec:
    def test_to_provider_schema(self):
        spec = ToolSpec(
            name="test",
            description="A test",
            input_schema={"type": "object", "properties": {"a": {"type": "string"}}},
        )
        schema = spec.to_provider_schema()
        assert schema == {
            "name": "test",
            "description": "A test",
            "input_schema": {"type": "object", "properties": {"a": {"type": "string"}}},
        }

    def test_side_effects_default(self):
        spec = ToolSpec(name="x", description="x")
        assert spec.side_effects == [SideEffect.NONE]

    def test_handler_excluded_from_serialization(self):
        async def h() -> str:
            return ""

        spec = ToolSpec(name="x", description="x", handler=h)
        dumped = spec.model_dump()
        assert "handler" not in dumped


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    @pytest.mark.asyncio
    async def test_rejects_invalid_arguments(self):
        """Tool calls with arguments that don't match the schema should be rejected."""
        reg = ToolRegistry()

        async def typed_tool(name: str, count: int) -> str:
            return f"{name}: {count}"

        reg.add(
            ToolSpec(
                name="typed",
                description="needs name and count",
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "count": {"type": "integer"},
                    },
                    "required": ["name", "count"],
                },
                handler=typed_tool,
            )
        )
        executor = ToolExecutor(reg, ToolConfig(), SafetyConfig())

        tc = ToolCall(
            tool_name="typed",
            tool_call_id="tc_1",
            arguments={"name": "test"},  # missing required 'count'
        )
        results = await executor.execute([tc])
        assert results[0].is_error
        assert "invalid arguments" in results[0].output.lower()

    @pytest.mark.asyncio
    async def test_accepts_valid_arguments(self):
        """Valid arguments should pass validation and execute normally."""
        reg = ToolRegistry()

        async def typed_tool(name: str) -> str:
            return f"hello {name}"

        reg.add(
            ToolSpec(
                name="typed",
                description="greet",
                input_schema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
                handler=typed_tool,
            )
        )
        executor = ToolExecutor(reg, ToolConfig(), SafetyConfig())

        tc = ToolCall(tool_name="typed", tool_call_id="tc_1", arguments={"name": "world"})
        results = await executor.execute([tc])
        assert not results[0].is_error
        assert results[0].output == "hello world"


# ---------------------------------------------------------------------------
# Parallel tool execution
# ---------------------------------------------------------------------------


class TestParallelExecution:
    @pytest.mark.asyncio
    async def test_parallel_execution_produces_correct_results(self):
        """Multiple tool calls should execute in parallel and return correct results."""
        reg = ToolRegistry()

        async def echo(message: str) -> str:
            await asyncio.sleep(0.01)  # simulate work
            return f"echo: {message}"

        reg.add(
            ToolSpec(
                name="echo",
                description="echo",
                input_schema={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
                handler=echo,
            )
        )
        executor = ToolExecutor(reg, ToolConfig(), SafetyConfig())

        calls = [
            ToolCall(tool_name="echo", tool_call_id=f"tc_{i}", arguments={"message": f"msg{i}"})
            for i in range(3)
        ]
        results = await executor.execute(calls, parallel=True)
        assert len(results) == 3
        assert results[0].output == "echo: msg0"
        assert results[1].output == "echo: msg1"
        assert results[2].output == "echo: msg2"

    @pytest.mark.asyncio
    async def test_parallel_is_actually_concurrent(self):
        """Parallel execution should be faster than sequential for slow tools."""
        import time

        reg = ToolRegistry()

        async def slow(message: str) -> str:
            await asyncio.sleep(0.05)
            return message

        reg.add(
            ToolSpec(
                name="slow",
                description="slow",
                input_schema={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
                handler=slow,
            )
        )
        executor = ToolExecutor(reg, ToolConfig(), SafetyConfig())

        calls = [
            ToolCall(tool_name="slow", tool_call_id=f"tc_{i}", arguments={"message": f"m{i}"})
            for i in range(3)
        ]

        t0 = time.monotonic()
        await executor.execute(calls, parallel=True)
        parallel_time = time.monotonic() - t0

        # 3 calls at 50ms each: sequential ~150ms, parallel ~50ms
        # Allow generous margin but it should be well under 150ms
        assert parallel_time < 0.12
