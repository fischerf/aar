"""Tests for agent/extensions/mcp.py

All tests mock the `mcp` SDK so a real MCP server is never required.
The mock is injected via sys.modules before each import of the extension.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.tools.registry import ToolRegistry
from agent.tools.schema import SideEffect

# ---------------------------------------------------------------------------
# Helpers — build a fake `mcp` module tree
# ---------------------------------------------------------------------------


def _make_fake_tool(name: str, description: str, input_schema: dict | None = None):
    tool = MagicMock()
    tool.name = name
    tool.description = description
    tool.inputSchema = input_schema or {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    }
    return tool


def _make_fake_text_content(text: str):
    block = MagicMock()
    block.text = text
    del block.resource  # ensure hasattr(block, "resource") is False
    del block.data
    return block


def _make_fake_image_content(mime: str = "image/png"):
    block = MagicMock(spec=["data", "mimeType"])
    block.data = b"..."
    block.mimeType = mime
    return block


def _make_fake_embedded_text(text: str):
    resource = MagicMock(spec=["text"])
    resource.text = text
    block = MagicMock(spec=["resource"])
    block.resource = resource
    return block


def _make_mcp_sys_modules(tools: list | None = None, call_result_content=None):
    """Return a dict of fake mcp modules to inject into sys.modules."""
    tools = tools or []
    call_result_content = call_result_content or [_make_fake_text_content("ok")]

    # --- session mock ---
    session = AsyncMock()
    session.initialize = AsyncMock()

    list_tools_resp = MagicMock()
    list_tools_resp.tools = tools
    session.list_tools = AsyncMock(return_value=list_tools_resp)

    call_tool_resp = MagicMock()
    call_tool_resp.content = call_result_content
    session.call_tool = AsyncMock(return_value=call_tool_resp)

    # ClientSession is an async context manager
    client_session_cls = MagicMock()
    client_session_instance = AsyncMock()
    client_session_instance.__aenter__ = AsyncMock(return_value=session)
    client_session_instance.__aexit__ = AsyncMock(return_value=False)
    client_session_cls.return_value = client_session_instance

    # StdioServerParameters
    stdio_params_cls = MagicMock()

    # stdio_client is an async context manager yielding (read, write)
    read_mock, write_mock = MagicMock(), MagicMock()

    @asynccontextmanager
    async def fake_stdio_client(params):
        yield read_mock, write_mock

    # streamablehttp_client yields (read, write, _)
    @asynccontextmanager
    async def fake_http_client(url, headers=None):
        yield read_mock, write_mock, None

    # Build module tree
    mcp_mod = MagicMock()
    mcp_mod.ClientSession = client_session_cls
    mcp_mod.StdioServerParameters = stdio_params_cls

    mcp_client_stdio = MagicMock()
    mcp_client_stdio.stdio_client = fake_stdio_client

    mcp_client_http = MagicMock()
    mcp_client_http.streamablehttp_client = fake_http_client

    # Also expose on mcp.client.streamable_http
    mcp_client_streamable_http = MagicMock()
    mcp_client_streamable_http.streamablehttp_client = fake_http_client

    mcp_client = MagicMock()
    mcp_client.stdio = mcp_client_stdio
    mcp_client.http = mcp_client_http
    mcp_client.streamable_http = mcp_client_streamable_http

    return {
        "mcp": mcp_mod,
        "mcp.client": mcp_client,
        "mcp.client.stdio": mcp_client_stdio,
        "mcp.client.http": mcp_client_http,
        "mcp.client.streamable_http": mcp_client_streamable_http,
    }, session


@pytest.fixture()
def fake_mcp(monkeypatch):
    """Inject a fake mcp module into sys.modules for the duration of the test."""
    tools = [
        _make_fake_tool(
            "echo",
            "Echo a message",
            {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        ),
        _make_fake_tool(
            "reverse",
            "Reverse a string",
            {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        ),
    ]
    modules, session = _make_mcp_sys_modules(tools=tools)
    for key, mod in modules.items():
        monkeypatch.setitem(sys.modules, key, mod)
    return session


# ---------------------------------------------------------------------------
# _content_to_str
# ---------------------------------------------------------------------------


class TestContentToStr:
    def test_text_content(self):
        from agent.extensions.mcp import _content_to_str

        block = _make_fake_text_content("hello world")
        assert _content_to_str([block]) == "hello world"

    def test_multiple_text_blocks(self):
        from agent.extensions.mcp import _content_to_str

        blocks = [_make_fake_text_content("line1"), _make_fake_text_content("line2")]
        assert _content_to_str(blocks) == "line1\nline2"

    def test_empty_content(self):
        from agent.extensions.mcp import _content_to_str

        assert _content_to_str([]) == ""

    def test_image_content(self):
        from agent.extensions.mcp import _content_to_str

        block = _make_fake_image_content("image/jpeg")
        result = _content_to_str([block])
        assert result == "[image: image/jpeg]"

    def test_embedded_text_resource(self):
        from agent.extensions.mcp import _content_to_str

        block = _make_fake_embedded_text("embedded text")
        assert _content_to_str([block]) == "embedded text"

    def test_mixed_content(self):
        from agent.extensions.mcp import _content_to_str

        blocks = [
            _make_fake_text_content("before"),
            _make_fake_image_content("image/png"),
            _make_fake_text_content("after"),
        ]
        result = _content_to_str(blocks)
        assert "before" in result
        assert "[image: image/png]" in result
        assert "after" in result


# ---------------------------------------------------------------------------
# MCPServerConfig
# ---------------------------------------------------------------------------


class TestMCPServerConfig:
    def test_stdio_defaults(self):
        from agent.extensions.mcp import MCPServerConfig

        cfg = MCPServerConfig(name="test", command="python")
        assert cfg.transport == "stdio"
        assert cfg.prefix_tools is False
        assert cfg.args == []
        assert cfg.env == {}

    def test_http_config(self):
        from agent.extensions.mcp import MCPServerConfig

        cfg = MCPServerConfig(
            name="remote",
            transport="http",
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer tok"},
        )
        assert cfg.transport == "http"
        assert cfg.url == "https://example.com/mcp"
        assert cfg.headers["Authorization"] == "Bearer tok"

    def test_prefix_tools(self):
        from agent.extensions.mcp import MCPServerConfig

        cfg = MCPServerConfig(name="srv", transport="stdio", command="x", prefix_tools=True)
        assert cfg.prefix_tools is True


# ---------------------------------------------------------------------------
# MCPClient
# ---------------------------------------------------------------------------


class TestMCPClient:
    @pytest.mark.asyncio
    async def test_list_tools_returns_tool_specs(self, fake_mcp):
        from agent.extensions.mcp import MCPClient, MCPServerConfig

        cfg = MCPServerConfig(name="srv", transport="stdio", command="python")
        async with MCPClient(cfg) as client:
            specs = await client.list_tools()

        assert len(specs) == 2
        names = [s.name for s in specs]
        assert "echo" in names
        assert "reverse" in names

    @pytest.mark.asyncio
    async def test_tool_spec_fields(self, fake_mcp):
        from agent.extensions.mcp import MCPClient, MCPServerConfig

        cfg = MCPServerConfig(name="srv", transport="stdio", command="python")
        async with MCPClient(cfg) as client:
            specs = await client.list_tools()

        echo = next(s for s in specs if s.name == "echo")
        assert echo.description == "Echo a message"
        assert echo.input_schema["properties"]["message"]["type"] == "string"
        assert SideEffect.EXTERNAL in echo.side_effects
        assert echo.handler is not None

    @pytest.mark.asyncio
    async def test_prefix_tools(self, fake_mcp):
        from agent.extensions.mcp import MCPClient, MCPServerConfig

        cfg = MCPServerConfig(
            name="myserver", transport="stdio", command="python", prefix_tools=True
        )
        async with MCPClient(cfg) as client:
            specs = await client.list_tools()

        names = [s.name for s in specs]
        assert "myserver__echo" in names
        assert "myserver__reverse" in names

    @pytest.mark.asyncio
    async def test_call_tool_returns_string(self, fake_mcp):
        from agent.extensions.mcp import MCPClient, MCPServerConfig

        cfg = MCPServerConfig(name="srv", transport="stdio", command="python")
        async with MCPClient(cfg) as client:
            result = await client.call_tool("echo", {"message": "hi"})

        assert result == "ok"
        fake_mcp.call_tool.assert_called_once_with("echo", {"message": "hi"})

    @pytest.mark.asyncio
    async def test_call_tool_passes_arguments(self, fake_mcp):
        from agent.extensions.mcp import MCPClient, MCPServerConfig

        cfg = MCPServerConfig(name="srv", transport="stdio", command="python")
        async with MCPClient(cfg) as client:
            await client.call_tool("echo", {"message": "test", "count": 3})

        fake_mcp.call_tool.assert_called_once_with("echo", {"message": "test", "count": 3})

    @pytest.mark.asyncio
    async def test_tool_handler_calls_mcp(self, fake_mcp):
        """The generated handler closure should call the MCP session."""
        from agent.extensions.mcp import MCPClient, MCPServerConfig

        cfg = MCPServerConfig(name="srv", transport="stdio", command="python")
        async with MCPClient(cfg) as client:
            specs = await client.list_tools()
            echo_spec = next(s for s in specs if s.name == "echo")
            result = await echo_spec.handler(message="hello")

        assert result == "ok"

    @pytest.mark.asyncio
    async def test_not_connected_raises(self):
        from agent.extensions.mcp import MCPClient, MCPServerConfig

        cfg = MCPServerConfig(name="srv", transport="stdio", command="python")
        client = MCPClient(cfg)  # not entered as context manager
        with pytest.raises(RuntimeError, match="not connected"):
            await client.list_tools()

    @pytest.mark.asyncio
    async def test_missing_command_raises(self, fake_mcp):
        from agent.extensions.mcp import MCPClient, MCPServerConfig

        cfg = MCPServerConfig(name="srv", transport="stdio", command="")
        with pytest.raises(ValueError, match="command"):
            async with MCPClient(cfg):
                pass

    @pytest.mark.asyncio
    async def test_missing_url_raises(self, fake_mcp):
        from agent.extensions.mcp import MCPClient, MCPServerConfig

        cfg = MCPServerConfig(name="srv", transport="http", url="")
        with pytest.raises(ValueError, match="url"):
            async with MCPClient(cfg):
                pass

    @pytest.mark.asyncio
    async def test_unknown_transport_raises(self, fake_mcp):
        from agent.extensions.mcp import MCPClient, MCPServerConfig

        cfg = MCPServerConfig(name="srv", transport="grpc", command="x")
        with pytest.raises(ValueError, match="transport"):
            async with MCPClient(cfg):
                pass

    @pytest.mark.asyncio
    async def test_http_transport(self, fake_mcp):
        from agent.extensions.mcp import MCPClient, MCPServerConfig

        cfg = MCPServerConfig(name="srv", transport="http", url="https://example.com/mcp")
        async with MCPClient(cfg) as client:
            specs = await client.list_tools()
        assert len(specs) == 2

    @pytest.mark.asyncio
    async def test_mcp_not_installed_raises(self, monkeypatch):
        # Simulate mcp not being installed
        monkeypatch.setitem(sys.modules, "mcp", None)  # type: ignore[arg-type]
        from agent.extensions.mcp import MCPClient, MCPServerConfig

        cfg = MCPServerConfig(name="srv", transport="stdio", command="python")
        with pytest.raises(ImportError, match="mcp"):
            async with MCPClient(cfg):
                pass

    @pytest.mark.asyncio
    async def test_aexit_from_different_task_succeeds(self, monkeypatch):
        """Regression for the anyio "different task" error during MCP teardown.

        Mimics ``mcp.client.stdio.stdio_client`` by wrapping the yield in a real
        :func:`anyio.create_task_group` — the same construct that produces::

            RuntimeError: Attempted to exit cancel scope in a different task
                          than it was entered in

        when ``__aenter__`` and ``__aexit__`` run in different tasks (the
        actual ACP failure mode where ``session/new`` and ``session/close``
        are dispatched on different handler tasks). MCPClient's dedicated
        lifetime task keeps both ends of the cancel scope in the same task.
        """
        anyio = pytest.importorskip("anyio")

        @asynccontextmanager
        async def stdio_with_task_group(params):
            async with anyio.create_task_group():
                yield MagicMock(), MagicMock()

        @asynccontextmanager
        async def http_with_task_group(url, headers=None):
            async with anyio.create_task_group():
                yield MagicMock(), MagicMock(), None

        mods, _ = _make_mcp_sys_modules()
        mods["mcp.client.stdio"].stdio_client = stdio_with_task_group
        mods["mcp.client.http"].streamablehttp_client = http_with_task_group
        mods["mcp.client.streamable_http"].streamablehttp_client = http_with_task_group
        for key, mod in mods.items():
            monkeypatch.setitem(sys.modules, key, mod)

        from agent.extensions.mcp import MCPClient, MCPServerConfig

        cfg = MCPServerConfig(name="srv", transport="stdio", command="python")
        client = MCPClient(cfg)

        # Enter in the current task...
        await client.__aenter__()
        assert client._session is not None

        # ...exit from a different task. Without the dedicated-lifetime-task
        # fix this raises the anyio RuntimeError.
        await asyncio.create_task(client.__aexit__(None, None, None))

        assert client._session is None
        assert client._lifetime_task is None
        assert client._stop_event is None

    @pytest.mark.asyncio
    async def test_bridge_aexit_from_different_task_succeeds(self, monkeypatch):
        """MCPBridge inherits MCPClient's cross-task safety — same scenario,
        one layer up. Exercised because ACP's ``_setup_mcp`` /
        ``_teardown_mcp`` operate on the bridge, not on individual clients.
        """
        anyio = pytest.importorskip("anyio")

        @asynccontextmanager
        async def stdio_with_task_group(params):
            async with anyio.create_task_group():
                yield MagicMock(), MagicMock()

        mods, _ = _make_mcp_sys_modules()
        mods["mcp.client.stdio"].stdio_client = stdio_with_task_group
        for key, mod in mods.items():
            monkeypatch.setitem(sys.modules, key, mod)

        from agent.extensions.mcp import MCPBridge, MCPServerConfig

        cfgs = [
            MCPServerConfig(name="a", transport="stdio", command="x"),
            MCPServerConfig(name="b", transport="stdio", command="y"),
        ]
        bridge = MCPBridge(cfgs)

        await bridge.__aenter__()
        assert len(bridge.clients) == 2

        await asyncio.create_task(bridge.__aexit__(None, None, None))

        assert bridge.clients == []


# ---------------------------------------------------------------------------
# MCPBridge
# ---------------------------------------------------------------------------


class TestMCPBridge:
    @pytest.mark.asyncio
    async def test_register_all_returns_count(self, fake_mcp):
        from agent.extensions.mcp import MCPBridge, MCPServerConfig

        cfg = MCPServerConfig(name="srv", transport="stdio", command="python")
        registry = ToolRegistry()

        async with MCPBridge([cfg]) as bridge:
            count = await bridge.register_all(registry)

        assert count == 2
        assert len(registry) == 2

    @pytest.mark.asyncio
    async def test_tools_callable_after_registration(self, fake_mcp):
        from agent.extensions.mcp import MCPBridge, MCPServerConfig

        cfg = MCPServerConfig(name="srv", transport="stdio", command="python")
        registry = ToolRegistry()

        async with MCPBridge([cfg]) as bridge:
            await bridge.register_all(registry)
            spec = registry.get("echo")
            assert spec is not None
            result = await spec.handler(message="hello")

        assert result == "ok"

    @pytest.mark.asyncio
    async def test_multiple_servers_no_collision(self, monkeypatch):
        # Server A: echo + reverse; Server B: fetch (different names)
        tools_a = [_make_fake_tool("echo", "Echo")]
        tools_b = [_make_fake_tool("fetch", "Fetch")]

        mods_a, _ = _make_mcp_sys_modules(tools=tools_a)
        mods_b, _ = _make_mcp_sys_modules(tools=tools_b)

        # We can only inject one fake mcp at a time in sys.modules, so test via MCPClient mocking
        from agent.extensions.mcp import MCPBridge, MCPClient, MCPServerConfig

        async def fake_list_tools_a(self_):
            from agent.tools.schema import SideEffect, ToolSpec

            return [
                ToolSpec(
                    name="echo",
                    description="Echo",
                    handler=AsyncMock(return_value=""),
                    side_effects=[SideEffect.EXTERNAL],
                )
            ]

        async def fake_list_tools_b(self_):
            from agent.tools.schema import SideEffect, ToolSpec

            return [
                ToolSpec(
                    name="fetch",
                    description="Fetch",
                    handler=AsyncMock(return_value=""),
                    side_effects=[SideEffect.EXTERNAL],
                )
            ]

        cfgs = [
            MCPServerConfig(name="a", transport="stdio", command="x"),
            MCPServerConfig(name="b", transport="stdio", command="y"),
        ]
        registry = ToolRegistry()

        # Patch __aenter__ to avoid real connections
        with (
            patch.object(MCPClient, "__aenter__", return_value=None) as mock_enter,
            patch.object(MCPClient, "__aexit__", return_value=False),
        ):
            mock_enter.side_effect = lambda self_: (
                self_.__dict__.update({"_session": AsyncMock()}) or self_
            )

            bridge = MCPBridge(cfgs)
            bridge._clients = []

            client_a = MCPClient(cfgs[0])
            client_a._session = AsyncMock()
            client_b = MCPClient(cfgs[1])
            client_b._session = AsyncMock()

            with (
                patch.object(client_a, "list_tools", lambda: fake_list_tools_a(client_a)),
                patch.object(client_b, "list_tools", lambda: fake_list_tools_b(client_b)),
            ):
                bridge._clients = [client_a, client_b]
                count = await bridge.register_all(registry)

        assert count == 2
        assert "echo" in registry
        assert "fetch" in registry

    @pytest.mark.asyncio
    async def test_name_collision_raises(self, monkeypatch):
        """Two servers exposing the same tool name without prefix_tools should raise."""
        # Both servers expose "echo"
        tools = [_make_fake_tool("echo", "Echo")]
        mods, session = _make_mcp_sys_modules(tools=tools)
        for key, mod in mods.items():
            monkeypatch.setitem(sys.modules, key, mod)

        from agent.extensions.mcp import MCPBridge, MCPServerConfig

        cfgs = [
            MCPServerConfig(name="a", transport="stdio", command="x"),
            MCPServerConfig(name="b", transport="stdio", command="y"),
        ]
        registry = ToolRegistry()

        with pytest.raises(ValueError, match="collision"):
            async with MCPBridge(cfgs) as bridge:
                await bridge.register_all(registry)

    @pytest.mark.asyncio
    async def test_skip_failed_keeps_first_server_on_collision(self, monkeypatch):
        tools = [_make_fake_tool("echo", "Echo")]
        mods, _ = _make_mcp_sys_modules(tools=tools)
        for key, mod in mods.items():
            monkeypatch.setitem(sys.modules, key, mod)

        from agent.extensions.mcp import MCPBridge, MCPServerConfig

        cfgs = [
            MCPServerConfig(name="a", transport="stdio", command="x"),
            MCPServerConfig(name="b", transport="stdio", command="y"),
        ]
        registry = ToolRegistry()

        async with MCPBridge(cfgs, skip_failed=True) as bridge:
            count = await bridge.register_all(registry)
        assert count == 1
        assert "echo" in registry
        assert bridge.failures == [("b", "tool 'echo' already exists")]

    @pytest.mark.asyncio
    async def test_skip_failed_skips_server_that_cannot_start(self, monkeypatch):
        tools = [_make_fake_tool("echo", "Echo")]
        mods, _ = _make_mcp_sys_modules(tools=tools)
        for key, mod in mods.items():
            monkeypatch.setitem(sys.modules, key, mod)

        from agent.extensions.mcp import MCPBridge, MCPClient, MCPServerConfig

        real_enter = MCPClient.__aenter__

        async def enter(self_):
            if self_.config.name == "broken":
                raise OSError("no such program")
            return await real_enter(self_)

        monkeypatch.setattr(MCPClient, "__aenter__", enter)
        cfgs = [
            MCPServerConfig(name="broken", transport="stdio", command="missing"),
            MCPServerConfig(name="ok", transport="stdio", command="x"),
        ]
        registry = ToolRegistry()

        async with MCPBridge(cfgs, skip_failed=True) as bridge:
            count = await bridge.register_all(registry)
            assert [c.config.name for c in bridge.clients] == ["ok"]
        assert count == 1
        assert bridge.failures == [("broken", "could not start: no such program")]

        with pytest.raises(OSError):
            async with MCPBridge(cfgs):
                pass

    @pytest.mark.asyncio
    async def test_prefix_tools_avoids_collision(self, monkeypatch):
        """prefix_tools=True should namespace tools and prevent collisions."""
        tools = [_make_fake_tool("echo", "Echo")]
        mods, _ = _make_mcp_sys_modules(tools=tools)
        for key, mod in mods.items():
            monkeypatch.setitem(sys.modules, key, mod)

        from agent.extensions.mcp import MCPBridge, MCPServerConfig

        cfgs = [
            MCPServerConfig(name="a", transport="stdio", command="x", prefix_tools=True),
            MCPServerConfig(name="b", transport="stdio", command="y", prefix_tools=True),
        ]
        registry = ToolRegistry()

        async with MCPBridge(cfgs) as bridge:
            count = await bridge.register_all(registry)

        assert count == 2
        assert "a__echo" in registry
        assert "b__echo" in registry

    @pytest.mark.asyncio
    async def test_clients_property(self, fake_mcp):
        from agent.extensions.mcp import MCPBridge, MCPServerConfig

        cfg = MCPServerConfig(name="srv", transport="stdio", command="python")
        async with MCPBridge([cfg]) as bridge:
            assert len(bridge.clients) == 1

    @pytest.mark.asyncio
    async def test_empty_bridge(self):
        from agent.extensions.mcp import MCPBridge

        registry = ToolRegistry()
        async with MCPBridge([]) as bridge:
            count = await bridge.register_all(registry)
        assert count == 0
        assert len(registry) == 0


# ---------------------------------------------------------------------------
# load_mcp_config
# ---------------------------------------------------------------------------


class TestLoadMCPConfig:
    def test_load_with_servers_key(self, tmp_path):
        from agent.extensions.mcp import load_mcp_config

        f = tmp_path / "mcp.json"
        f.write_text('{"servers": [{"name": "test", "transport": "stdio", "command": "echo"}]}')
        configs = load_mcp_config(str(f))
        assert len(configs) == 1
        assert configs[0].name == "test"
        assert configs[0].command == "echo"

    def test_load_bare_array(self, tmp_path):
        from agent.extensions.mcp import load_mcp_config

        f = tmp_path / "mcp.json"
        f.write_text('[{"name": "t", "transport": "stdio", "command": "x"}]')
        configs = load_mcp_config(str(f))
        assert len(configs) == 1
        assert configs[0].name == "t"

    def test_load_http_server(self, tmp_path):
        from agent.extensions.mcp import load_mcp_config

        f = tmp_path / "mcp.json"
        f.write_text(
            '{"servers": [{"name": "api", "transport": "http",'
            ' "url": "https://example.com/mcp", "prefix_tools": true}]}'
        )
        configs = load_mcp_config(str(f))
        assert configs[0].transport == "http"
        assert configs[0].url == "https://example.com/mcp"
        assert configs[0].prefix_tools is True

    def test_load_multiple_servers(self, tmp_path):
        from agent.extensions.mcp import load_mcp_config

        f = tmp_path / "mcp.json"
        f.write_text(
            '{"servers": ['
            '{"name": "a", "transport": "stdio", "command": "x"},'
            '{"name": "b", "transport": "stdio", "command": "y"}'
            "]}"
        )
        configs = load_mcp_config(str(f))
        assert len(configs) == 2
        assert configs[0].name == "a"
        assert configs[1].name == "b"

    def test_load_validates_fields(self, tmp_path):
        from pydantic import ValidationError

        from agent.extensions.mcp import load_mcp_config

        f = tmp_path / "mcp.json"
        f.write_text('[{"transport": "stdio"}]')  # missing required "name"
        with pytest.raises(ValidationError):
            load_mcp_config(str(f))

    def test_load_file_not_found(self):
        from agent.extensions.mcp import load_mcp_config

        with pytest.raises(FileNotFoundError):
            load_mcp_config("/nonexistent/mcp.json")

    def test_load_invalid_json(self, tmp_path):
        from agent.extensions.mcp import load_mcp_config

        f = tmp_path / "mcp.json"
        f.write_text("not valid json")
        with pytest.raises(Exception):
            load_mcp_config(str(f))

    def test_load_bad_shape(self, tmp_path):
        from agent.extensions.mcp import load_mcp_config

        f = tmp_path / "mcp.json"
        f.write_text('"just a string"')
        with pytest.raises(ValueError, match="Expected"):
            load_mcp_config(str(f))


# ---------------------------------------------------------------------------
# _register_builtins preserves pre-existing tools
# ---------------------------------------------------------------------------


class TestRegisterBuiltinsPreservesExternal:
    def test_external_tools_not_pruned(self):
        """MCP tools already in the registry should survive _register_builtins."""
        from agent.core.agent import Agent
        from agent.core.config import AgentConfig

        registry = ToolRegistry()

        async def dummy(**kwargs):
            return "ok"

        from agent.tools.schema import SideEffect, ToolSpec

        registry.add(
            ToolSpec(
                name="mcp_external_tool",
                description="An external MCP tool",
                side_effects=[SideEffect.EXTERNAL],
                handler=dummy,
            )
        )

        agent = Agent(config=AgentConfig(), registry=registry)
        assert "mcp_external_tool" in agent.registry
        # Built-in tools should also be present
        assert "read_file" in agent.registry


# ---------------------------------------------------------------------------
# Tool annotations -> side effects -> approval
# ---------------------------------------------------------------------------

_ABSENT = object()


def _annotated_tool(name: str, read_only=_ABSENT, destructive=_ABSENT):
    """A fake MCP tool; ``_ABSENT`` for both hints means no annotations at all."""
    from types import SimpleNamespace

    tool = _make_fake_tool(name, f"{name} tool", {"type": "object", "properties": {}})
    if read_only is _ABSENT and destructive is _ABSENT:
        tool.annotations = None
    else:
        tool.annotations = SimpleNamespace(
            readOnlyHint=None if read_only is _ABSENT else read_only,
            destructiveHint=None if destructive is _ABSENT else destructive,
        )
    return tool


_EXT = [SideEffect.EXTERNAL]
_EXT_EXEC = [SideEffect.EXTERNAL, SideEffect.EXECUTE]


class TestAnnotationSideEffects:
    @pytest.mark.parametrize(
        "read_only, destructive, expected",
        [
            (_ABSENT, _ABSENT, _EXT),  # unannotated: unchanged behaviour
            (True, _ABSENT, _EXT),
            (True, True, _EXT),  # readOnlyHint wins (destructiveHint is meaningless then)
            (None, None, _EXT),
            (False, _ABSENT, _EXT_EXEC),
            (False, False, _EXT_EXEC),  # additive but still a change
            (None, True, _EXT_EXEC),
        ],
    )
    def test_mapping(self, read_only, destructive, expected):
        from agent.extensions.mcp import _side_effects_for

        assert _side_effects_for(_annotated_tool("t", read_only, destructive)) == expected

    def test_mock_attributes_are_not_hints(self):
        # A MagicMock (or any truthy non-bool) must not be mistaken for a hint.
        from agent.extensions.mcp import _side_effects_for

        assert _side_effects_for(_make_fake_tool("t", "t")) == _EXT

    @pytest.mark.parametrize(
        "read_only_hint, mode, expected",
        [
            (False, "review", "ask"),
            (True, "review", "allow"),
            (None, "review", "allow"),
            (False, "auto", "allow"),
            (False, "read-only", "deny"),
            (True, "read-only", "allow"),
        ],
    )
    def test_policy_decision(self, read_only_hint, mode, expected):
        from agent.extensions.mcp import _side_effects_for
        from agent.safety.policy import PolicyConfig, SafetyPolicy
        from agent.tools.schema import ToolSpec

        flags = {
            "review": {"require_approval_for_writes": True, "require_approval_for_execute": True},
            "auto": {},
            "read-only": {"read_only": True},
        }[mode]
        spec = ToolSpec(
            name="windows__click_element",
            description="",
            side_effects=_side_effects_for(_annotated_tool("t", read_only_hint)),
        )
        policy = SafetyPolicy(PolicyConfig(**flags))
        assert policy.check_tool(spec, {"window": "Notepad", "element": "e3"}).value == expected

    async def test_list_tools_uses_annotations(self, monkeypatch):
        mods, _ = _make_mcp_sys_modules(
            tools=[_annotated_tool("click", False, True), _annotated_tool("look", True)]
        )
        for name, mod in mods.items():
            monkeypatch.setitem(sys.modules, name, mod)
        from agent.extensions.mcp import MCPClient, MCPServerConfig

        cfg = MCPServerConfig(name="srv", transport="stdio", command="python")
        async with MCPClient(cfg) as client:
            click, look = await client.list_tools()
        assert click.side_effects == _EXT_EXEC
        assert look.side_effects == _EXT


class TestApprovalEndToEnd:
    """MCP bridge -> registry -> ToolExecutor -> SafetyPolicy -> approval callback."""

    @staticmethod
    async def _run(monkeypatch, tool_name, *, safety=None, answer=None, is_error=False):
        from agent.core.config import SafetyConfig, ToolConfig
        from agent.core.events import ToolCall
        from agent.extensions.mcp import MCPBridge, MCPServerConfig
        from agent.safety.permissions import ApprovalResult
        from agent.tools.execution import ToolExecutor

        mods, session = _make_mcp_sys_modules(
            tools=[
                _annotated_tool("click_element", False, True),
                _annotated_tool("read_text", True),
            ],
            call_result_content=[_make_fake_text_content("server says hi")],
        )
        session.call_tool.return_value.isError = is_error
        for name, mod in mods.items():
            monkeypatch.setitem(sys.modules, name, mod)

        asked: list[str] = []

        async def approve(spec, tc):
            asked.append(spec.name)
            return answer or ApprovalResult.APPROVED

        cfg = MCPServerConfig(name="windows", command="python", prefix_tools=True)
        async with MCPBridge([cfg]) as bridge:
            registry = ToolRegistry()
            await bridge.register_all(registry)
            executor = ToolExecutor(
                registry, ToolConfig(), safety or SafetyConfig(), approval_callback=approve
            )
            (result,) = await executor.execute(
                [ToolCall(tool_name=f"windows__{tool_name}", arguments={})]
            )
        return result, asked, session

    async def test_review_mode_asks_before_a_change(self, monkeypatch):
        result, asked, session = await self._run(monkeypatch, "click_element")
        assert asked == ["windows__click_element"]
        assert not result.is_error and result.output == "server says hi"
        session.call_tool.assert_awaited_once_with("click_element", {})

    async def test_denied_change_never_reaches_the_server(self, monkeypatch):
        from agent.safety.permissions import ApprovalResult

        result, asked, session = await self._run(
            monkeypatch, "click_element", answer=ApprovalResult.DENIED
        )
        assert asked == ["windows__click_element"]
        assert result.is_error and "denied by user" in result.output
        session.call_tool.assert_not_awaited()

    async def test_read_only_tool_runs_without_asking(self, monkeypatch):
        result, asked, _ = await self._run(monkeypatch, "read_text")
        assert asked == []
        assert result.output == "server says hi"

    async def test_auto_mode_does_not_ask(self, monkeypatch):
        from agent.core.config import SafetyConfig

        auto = SafetyConfig(
            require_approval_for_writes=False, require_approval_for_execute=False, allowed_paths=[]
        )
        result, asked, _ = await self._run(monkeypatch, "click_element", safety=auto)
        assert asked == [] and result.output == "server says hi"

    async def test_auto_mode_still_asks_when_allowed_paths_cannot_be_enforced(self, monkeypatch):
        # Like bash: with allowed_paths active and the non-isolating "local"
        # sandbox, aar cannot verify what a desktop action touches, so it asks.
        from agent.core.config import SafetyConfig

        auto = SafetyConfig(require_approval_for_writes=False, require_approval_for_execute=False)
        assert auto.allowed_paths and auto.sandbox.mode == "local"
        _, asked, _ = await self._run(monkeypatch, "click_element", safety=auto)
        assert asked == ["windows__click_element"]
        _, asked, _ = await self._run(monkeypatch, "read_text", safety=auto)
        assert asked == []

    async def test_read_only_mode_blocks_changes(self, monkeypatch):
        from agent.core.config import SafetyConfig

        ro = SafetyConfig(read_only=True)
        result, asked, session = await self._run(monkeypatch, "click_element", safety=ro)
        assert asked == []
        assert result.is_error and "blocked by safety policy" in result.output
        session.call_tool.assert_not_awaited()
        result, _, _ = await self._run(monkeypatch, "read_text", safety=ro)
        assert not result.is_error

    async def test_server_error_is_reported_as_failed_call(self, monkeypatch):
        result, _, _ = await self._run(monkeypatch, "read_text", is_error=True)
        assert result.is_error
        assert "MCPToolError: server says hi" in result.output
