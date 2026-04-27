from __future__ import annotations

import asyncio
import logging
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agent.extensions.api import BlockResult, ExtensionAPI, ExtensionContext, ExtensionEventBus
from agent.extensions.loader import (
    ExtensionInfo,
    discover_extensions,
    load_all_extensions,
    load_extension,
)
from agent.extensions.manager import ExtensionManager
from agent.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(**overrides: Any) -> ExtensionContext:
    defaults: dict[str, Any] = {
        "session": MagicMock(),
        "config": MagicMock(),
        "signal": asyncio.Event(),
        "logger": logging.getLogger("test"),
    }
    defaults.update(overrides)
    return ExtensionContext(**defaults)


def _write_extension(path: Path, code: str) -> Path:
    path.write_text(textwrap.dedent(code), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. BlockResult
# ---------------------------------------------------------------------------


class TestBlockResult:
    def test_creation(self) -> None:
        br = BlockResult(reason="not allowed")
        assert br.reason == "not allowed"

    def test_frozen(self) -> None:
        br = BlockResult(reason="x")
        with pytest.raises(AttributeError):
            br.reason = "y"  # type: ignore[misc]

    def test_equality(self) -> None:
        assert BlockResult(reason="a") == BlockResult(reason="a")
        assert BlockResult(reason="a") != BlockResult(reason="b")


# ---------------------------------------------------------------------------
# 2. ExtensionEventBus
# ---------------------------------------------------------------------------


class TestExtensionEventBus:
    def test_sync_emit(self) -> None:
        bus = ExtensionEventBus()
        received: list[Any] = []

        @bus.on("ping")
        def handler(payload: Any) -> None:
            received.append(payload)

        bus.emit("ping", 42)
        assert received == [42]

    async def test_emit_async(self) -> None:
        bus = ExtensionEventBus()
        received: list[Any] = []

        @bus.on("ping")
        async def handler(payload: Any) -> None:
            received.append(payload)

        await bus.emit_async("ping", "hello")
        assert received == ["hello"]

    def test_decorator_returns_function(self) -> None:
        bus = ExtensionEventBus()

        @bus.on("x")
        def my_fn(payload: Any) -> None:
            pass

        assert callable(my_fn)

    def test_emit_no_handlers(self) -> None:
        bus = ExtensionEventBus()
        bus.emit("nonexistent")  # should not raise


# ---------------------------------------------------------------------------
# 3. ExtensionAPI
# ---------------------------------------------------------------------------


class TestExtensionAPI:
    def test_on_registers_handler(self) -> None:
        api = ExtensionAPI(name="test")

        @api.on("tool_call")
        def h(event: Any, ctx: Any) -> None:
            pass

        assert len(api._event_handlers["tool_call"]) == 1

    def test_tool_decorator_creates_spec(self) -> None:
        api = ExtensionAPI(name="test")

        @api.tool(name="greet", description="Say hi", input_schema={"type": "object"})
        def greet(ctx: Any) -> str:
            return "hi"

        assert len(api._tools) == 1
        spec = api._tools[0]
        assert spec.name == "greet"
        assert spec.description == "Say hi"
        assert spec.handler is greet

    def test_command_decorator(self) -> None:
        api = ExtensionAPI(name="test")

        @api.command("foo", description="do foo")
        def foo_cmd() -> None:
            pass

        assert "foo" in api._commands
        desc, fn = api._commands["foo"]
        assert desc == "do foo"
        assert fn is foo_cmd

    def test_append_system_prompt(self) -> None:
        api = ExtensionAPI(name="test")
        api.append_system_prompt("line1")
        api.append_system_prompt("line2")
        assert api._system_prompt_parts == ["line1", "line2"]

    def test_block_static_method(self) -> None:
        br = ExtensionAPI.block("nope")
        assert isinstance(br, BlockResult)
        assert br.reason == "nope"

    def test_unknown_event_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        api = ExtensionAPI(name="test")
        with caplog.at_level(logging.WARNING):

            @api.on("totally_bogus")
            def h(event: Any, ctx: Any) -> None:
                pass

        assert "unknown event" in caplog.text


# ---------------------------------------------------------------------------
# 4. Loader — discover_extensions
# ---------------------------------------------------------------------------


class TestDiscoverExtensions:
    def test_discovers_py_files(self, tmp_path: Path) -> None:
        user_dir = tmp_path / "user"
        user_dir.mkdir()
        (user_dir / "alpha.py").write_text("def register(api): pass")
        (user_dir / "_hidden.py").write_text("")

        with patch("agent.extensions.loader.importlib.metadata.entry_points", return_value=[]):
            infos = discover_extensions(user_dir=user_dir, project_dir=tmp_path / "nope")
        names = [i.name for i in infos]
        assert "alpha" in names
        assert "_hidden" not in names

    def test_project_shadows_user(self, tmp_path: Path) -> None:
        user_dir = tmp_path / "user"
        proj_dir = tmp_path / "proj"
        user_dir.mkdir()
        proj_dir.mkdir()
        (user_dir / "ext.py").write_text("# user")
        (proj_dir / "ext.py").write_text("# proj")

        with patch("agent.extensions.loader.importlib.metadata.entry_points", return_value=[]):
            infos = discover_extensions(user_dir=user_dir, project_dir=proj_dir)
        assert len(infos) == 1
        assert infos[0].source == "project"

    def test_entrypoint_discovery(self, tmp_path: Path) -> None:
        mock_ep = MagicMock()
        mock_ep.name = "ep_ext"
        mock_ep.value = "some.module:register"

        with patch(
            "agent.extensions.loader.importlib.metadata.entry_points", return_value=[mock_ep]
        ):
            infos = discover_extensions(user_dir=tmp_path / "u", project_dir=tmp_path / "p")

        assert any(i.name == "ep_ext" and i.source == "entrypoint" for i in infos)


# ---------------------------------------------------------------------------
# 5. Loader — load_extension
# ---------------------------------------------------------------------------


class TestLoadExtension:
    async def test_sync_register(self, tmp_path: Path) -> None:
        ext_file = tmp_path / "myext.py"
        _write_extension(
            ext_file,
            """\
            def register(api):
                @api.tool(name="t1", description="d", input_schema={})
                def _t(ctx):
                    return "ok"

                @api.on("tool_call")
                def _h(event, ctx):
                    pass
        """,
        )
        info = ExtensionInfo(name="myext", source="user", path=str(ext_file))
        api = await load_extension(info)
        assert len(api._tools) == 1
        assert api._tools[0].name == "t1"
        assert len(api._event_handlers["tool_call"]) == 1

    async def test_async_register(self, tmp_path: Path) -> None:
        ext_file = tmp_path / "asyncext.py"
        _write_extension(
            ext_file,
            """\
            async def register(api):
                api.append_system_prompt("hello from async")
        """,
        )
        info = ExtensionInfo(name="asyncext", source="user", path=str(ext_file))
        api = await load_extension(info)
        assert api._system_prompt_parts == ["hello from async"]

    async def test_missing_register_raises(self, tmp_path: Path) -> None:
        ext_file = tmp_path / "bad.py"
        ext_file.write_text("x = 1")
        info = ExtensionInfo(name="bad", source="user", path=str(ext_file))
        with pytest.raises(RuntimeError, match="Failed to import"):
            await load_extension(info)


# ---------------------------------------------------------------------------
# 6. Loader — load_all_extensions
# ---------------------------------------------------------------------------


class TestLoadAllExtensions:
    async def test_mixed_valid_and_invalid(self, tmp_path: Path) -> None:
        d = tmp_path / "exts"
        d.mkdir()
        (d / "good.py").write_text("def register(api): api.append_system_prompt('ok')")
        (d / "bad.py").write_text("# no register function")

        infos = await load_all_extensions(user_dir=d, project_dir=tmp_path / "nope")
        good = [i for i in infos if i.name == "good"]
        bad = [i for i in infos if i.name == "bad"]

        assert len(good) == 1 and good[0].api is not None
        assert len(bad) == 1 and bad[0].error is not None


# ---------------------------------------------------------------------------
# 7. ExtensionManager.initialize
# ---------------------------------------------------------------------------


class TestExtensionManagerInitialize:
    async def test_creates_context(self, tmp_path: Path) -> None:
        mgr = ExtensionManager()
        session = MagicMock()
        config = MagicMock()
        await mgr.initialize(session, config, user_dir=tmp_path / "u", project_dir=tmp_path / "p")
        assert mgr._context is not None
        assert mgr._context.session is session


# ---------------------------------------------------------------------------
# 8. ExtensionManager.register_tools
# ---------------------------------------------------------------------------


class TestExtensionManagerRegisterTools:
    async def test_tools_appear_in_registry(self, tmp_path: Path) -> None:
        d = tmp_path / "exts"
        d.mkdir()
        _write_extension(
            d / "toolext.py",
            """\
            def register(api):
                @api.tool(name="ext_tool", description="d", input_schema={})
                def _t(ctx):
                    return "ok"
        """,
        )
        mgr = ExtensionManager()
        await mgr.initialize(MagicMock(), MagicMock(), user_dir=d, project_dir=tmp_path / "np")

        registry = ToolRegistry()
        count = mgr.register_tools(registry)
        assert count == 1
        assert "ext_tool" in registry


# ---------------------------------------------------------------------------
# 9. ExtensionManager.fire_event
# ---------------------------------------------------------------------------


class TestExtensionManagerFireEvent:
    def _build_manager(self, handlers: dict[str, list]) -> ExtensionManager:
        """Build a manager with pre-wired extension info (no disk I/O)."""
        mgr = ExtensionManager()
        mgr._context = _make_context()
        api = ExtensionAPI(name="inline")
        for event_name, fns in handlers.items():
            api._event_handlers[event_name] = list(fns)
        info = ExtensionInfo(name="inline", source="user", path=None, api=api)
        mgr._extensions = [info]
        return mgr

    async def test_tool_call_block(self) -> None:
        def blocker(event: Any, ctx: Any) -> BlockResult:
            return BlockResult(reason="blocked")

        mgr = self._build_manager({"tool_call": [blocker]})
        result = await mgr.fire_event("tool_call", {"tool": "rm"})
        assert isinstance(result, BlockResult)
        assert result.reason == "blocked"

    async def test_user_message_transform(self) -> None:
        def transform(event: Any, ctx: Any) -> str:
            return "transformed"

        mgr = self._build_manager({"user_message": [transform]})
        result = await mgr.fire_event("user_message", "original")
        assert result == "transformed"

    async def test_passthrough_returns_none(self) -> None:
        def noop(event: Any, ctx: Any) -> None:
            return None

        mgr = self._build_manager({"before_turn": [noop]})
        result = await mgr.fire_event("before_turn", {})
        assert result is None

    async def test_async_handler(self) -> None:
        async def async_block(event: Any, ctx: Any) -> BlockResult:
            return BlockResult(reason="async block")

        mgr = self._build_manager({"tool_call": [async_block]})
        result = await mgr.fire_event("tool_call", {})
        assert isinstance(result, BlockResult)


# ---------------------------------------------------------------------------
# 10. ExtensionManager.commands
# ---------------------------------------------------------------------------


class TestExtensionManagerCommands:
    def test_merges_commands(self) -> None:
        mgr = ExtensionManager()
        api1 = ExtensionAPI(name="a")
        api2 = ExtensionAPI(name="b")

        @api1.command("cmd1", description="one")
        def c1() -> None:
            pass

        @api2.command("cmd2", description="two")
        def c2() -> None:
            pass

        mgr._extensions = [
            ExtensionInfo(name="a", source="user", path=None, api=api1),
            ExtensionInfo(name="b", source="user", path=None, api=api2),
        ]
        cmds = mgr.commands
        assert "cmd1" in cmds and "cmd2" in cmds


# ---------------------------------------------------------------------------
# 11. ExtensionManager.get_system_prompt_additions
# ---------------------------------------------------------------------------


class TestExtensionManagerSystemPrompt:
    def test_concatenates_parts(self) -> None:
        mgr = ExtensionManager()
        api1 = ExtensionAPI(name="a")
        api2 = ExtensionAPI(name="b")
        api1.append_system_prompt("part1")
        api2.append_system_prompt("part2")

        mgr._extensions = [
            ExtensionInfo(name="a", source="user", path=None, api=api1),
            ExtensionInfo(name="b", source="user", path=None, api=api2),
        ]
        result = mgr.get_system_prompt_additions()
        assert result == "part1\npart2"

    def test_skips_extensions_without_api(self) -> None:
        mgr = ExtensionManager()
        mgr._extensions = [ExtensionInfo(name="broken", source="user", path=None, api=None)]
        assert mgr.get_system_prompt_additions() == ""


# ---------------------------------------------------------------------------
# 12. Companion extension
# ---------------------------------------------------------------------------


class TestCompanionExtension:
    def test_register_populates_api(self) -> None:
        from agent.extensions.contrib.companion import register

        api = ExtensionAPI(name="companion")
        register(api)

        # Should have event handlers for the documented events
        assert "session_start" in api._event_handlers
        assert "tool_call" in api._event_handlers
        assert "stream_chunk" in api._event_handlers
        assert "error" in api._event_handlers
        assert "session_end" in api._event_handlers

        # Should have the companion_status tool
        tool_names = [t.name for t in api._tools]
        assert "companion_status" in tool_names
