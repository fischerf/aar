"""Tests for agent/extensions/mcp.py

All tests mock the `mcp` SDK so a real MCP server is never required.
The mock is injected via sys.modules before each import of the extension.
"""

from __future__ import annotations

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
            from agent.tools.schema import ToolSpec, SideEffect

            return [
                ToolSpec(
                    name="echo",
                    description="Echo",
                    handler=AsyncMock(return_value=""),
                    side_effects=[SideEffect.EXTERNAL],
                )
            ]

        async def fake_list_tools_b(self_):
            from agent.tools.schema import ToolSpec, SideEffect

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

        from agent.tools.schema import ToolSpec, SideEffect

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
