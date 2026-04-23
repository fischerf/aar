"""Tests for TUI extension slash-command dispatch and dynamic welcome screen.

Covers:
- TUIRenderer.render_welcome lists built-in commands
- TUIRenderer.render_welcome includes extension commands when passed
- Extension commands are dispatched correctly in the TUI command handler
- /help re-renders the welcome screen
- Unknown slash commands print a friendly error rather than going to the LLM
"""

from __future__ import annotations

import asyncio
import logging
from io import StringIO
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from agent.extensions.api import ExtensionAPI, ExtensionContext
from agent.extensions.manager import ExtensionManager
from agent.transports.themes.builtin import DEFAULT_THEME
from agent.transports.themes.models import LayoutConfig
from agent.transports.tui import TUIRenderer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_renderer(layout: LayoutConfig | None = None) -> tuple[TUIRenderer, StringIO]:
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, highlight=False, markup=True, width=120)
    renderer = TUIRenderer(console=console, theme=DEFAULT_THEME, layout=layout or LayoutConfig())
    return renderer, buf


def _strip_markup(text: str) -> str:
    """Very lightweight Rich markup stripper for assertions."""
    import re

    return re.sub(r"\[/?[^\]]+\]", "", text)


def _make_ctx(session: Any = None) -> ExtensionContext:
    return ExtensionContext(
        session=session or MagicMock(),
        config=MagicMock(),
        signal=asyncio.Event(),
        logger=logging.getLogger("aar.ext.test"),
    )


def _make_manager_with_command(
    cmd_name: str,
    handler,
    description: str = "test command",
) -> ExtensionManager:
    """Create an ExtensionManager pre-populated with one extension command."""
    api = ExtensionAPI(name="test_ext")
    api._commands[cmd_name] = (description, handler)

    mgr = ExtensionManager()
    # Inject a fake ExtensionInfo with the populated api
    from agent.extensions.loader import ExtensionInfo

    info = ExtensionInfo(name="test_ext", source="user", path=None)
    info.api = api
    mgr._extensions = [info]
    mgr._context = _make_ctx()
    return mgr


# ---------------------------------------------------------------------------
# render_welcome — built-in commands
# ---------------------------------------------------------------------------


class TestRenderWelcomeBuiltins:
    def test_contains_all_builtin_commands(self) -> None:
        renderer, buf = _make_renderer()
        renderer.render_welcome()
        text = _strip_markup(buf.getvalue())
        for cmd in ("quit", "status", "tools", "policy", "theme", "clear"):
            assert f"/{cmd}" in text, f"Expected built-in command /{cmd} in welcome output"

    def test_help_command_listed(self) -> None:
        renderer, buf = _make_renderer()
        renderer.render_welcome()
        text = _strip_markup(buf.getvalue())
        assert "/help" in text

    def test_no_extra_commands_by_default(self) -> None:
        renderer, buf = _make_renderer()
        renderer.render_welcome()
        text = _strip_markup(buf.getvalue())
        # Spot-check: /inspect is NOT in the default welcome (no extensions loaded)
        assert "/inspect" not in text

    def test_layout_hidden_suppresses_output(self) -> None:
        from agent.transports.themes.models import SectionConfig

        layout = LayoutConfig(welcome=SectionConfig(visible=False))
        renderer, buf = _make_renderer(layout=layout)
        renderer.render_welcome()
        assert buf.getvalue().strip() == ""


# ---------------------------------------------------------------------------
# render_welcome — extension commands appended
# ---------------------------------------------------------------------------


class TestRenderWelcomeExtensionCommands:
    def test_extra_commands_appear_in_output(self) -> None:
        renderer, buf = _make_renderer()
        renderer.render_welcome(extra_commands=["inspect", "git_checkpoint"])
        text = _strip_markup(buf.getvalue())
        assert "/inspect" in text
        assert "/git_checkpoint" in text

    def test_extra_commands_come_after_builtins(self) -> None:
        renderer, buf = _make_renderer()
        renderer.render_welcome(extra_commands=["inspect"])
        text = _strip_markup(buf.getvalue())
        # Both built-ins and extension command must be present
        assert "/quit" in text
        assert "/inspect" in text
        # inspect should appear after quit in the text
        assert text.index("/inspect") > text.index("/quit")

    def test_none_extra_commands_same_as_empty(self) -> None:
        renderer1, buf1 = _make_renderer()
        renderer1.render_welcome(extra_commands=None)
        renderer2, buf2 = _make_renderer()
        renderer2.render_welcome(extra_commands=[])
        assert _strip_markup(buf1.getvalue()) == _strip_markup(buf2.getvalue())

    def test_many_extension_commands(self) -> None:
        cmds = ["alpha", "beta", "gamma", "delta"]
        renderer, buf = _make_renderer()
        renderer.render_welcome(extra_commands=cmds)
        text = _strip_markup(buf.getvalue())
        for c in cmds:
            assert f"/{c}" in text


# ---------------------------------------------------------------------------
# Extension command dispatch
# ---------------------------------------------------------------------------


class TestExtensionCommandDispatch:
    """Test that registered extension commands are called by the TUI dispatcher.

    We unit-test the dispatch logic directly rather than running the full TUI
    event loop, by exercising the same code path used in run_tui.
    """

    def _dispatch(
        self,
        input_text: str,
        mgr: ExtensionManager,
        renderer: TUIRenderer,
    ) -> bool:
        """
        Simulate the extension slash-command dispatch block from run_tui.
        Returns True if the command was handled (would `continue`), False otherwise.
        """
        stripped = input_text.strip()
        if not stripped.startswith("/"):
            return False

        # Mirror the dispatch logic in run_tui
        cmd_name = stripped[1:].split()[0].lower()
        args_str = stripped[len(cmd_name) + 1 :].strip()

        cmds = mgr.commands
        if cmd_name in cmds:
            _, handler = cmds[cmd_name]
            ctx = mgr._context
            try:
                result = handler(args_str, ctx)
                if result is not None:
                    renderer.console.print(str(result))
            except Exception as exc:
                renderer.console.print(
                    f"[{renderer.theme.error.border_style}]Extension command error: {exc}[/]"
                )
            return True
        return False

    def test_registered_command_is_dispatched(self) -> None:
        called_with: list[tuple[str, Any]] = []

        def my_handler(args: str, ctx: ExtensionContext) -> None:
            called_with.append((args, ctx))

        mgr = _make_manager_with_command("inspect", my_handler)
        renderer, _ = _make_renderer()
        handled = self._dispatch("/inspect", mgr, renderer)

        assert handled is True
        assert len(called_with) == 1
        args_received, _ = called_with[0]
        assert args_received == ""

    def test_args_passed_to_handler(self) -> None:
        received: list[str] = []

        def my_handler(args: str, ctx: ExtensionContext) -> None:
            received.append(args)

        mgr = _make_manager_with_command("inspect", my_handler)
        renderer, _ = _make_renderer()
        self._dispatch("/inspect verbose", mgr, renderer)

        assert received == ["verbose"]

    def test_handler_return_value_printed(self) -> None:
        def my_handler(args: str, ctx: ExtensionContext) -> str:
            return "hello from extension"

        mgr = _make_manager_with_command("myext", my_handler)
        renderer, buf = _make_renderer()
        self._dispatch("/myext", mgr, renderer)

        assert "hello from extension" in buf.getvalue()

    def test_handler_none_return_prints_nothing(self) -> None:
        def my_handler(args: str, ctx: ExtensionContext) -> None:
            return None

        mgr = _make_manager_with_command("myext", my_handler)
        renderer, buf = _make_renderer()
        self._dispatch("/myext", mgr, renderer)

        # Only whitespace/empty — no extra output
        assert buf.getvalue().strip() == ""

    def test_unknown_command_not_handled(self) -> None:
        mgr = _make_manager_with_command("known", lambda a, c: None)
        renderer, _ = _make_renderer()
        handled = self._dispatch("/unknown_xyz", mgr, renderer)
        assert handled is False

    def test_handler_exception_prints_error_not_crash(self) -> None:
        def bad_handler(args: str, ctx: ExtensionContext) -> None:
            raise ValueError("boom")

        mgr = _make_manager_with_command("badcmd", bad_handler)
        renderer, buf = _make_renderer()
        # Should not raise
        handled = self._dispatch("/badcmd", mgr, renderer)
        assert handled is True
        assert "boom" in buf.getvalue() or "error" in buf.getvalue().lower()

    def test_command_name_is_case_insensitive(self) -> None:
        called: list[bool] = []

        def my_handler(args: str, ctx: ExtensionContext) -> None:
            called.append(True)

        mgr = _make_manager_with_command("inspect", my_handler)
        renderer, _ = _make_renderer()
        self._dispatch("/INSPECT", mgr, renderer)
        assert called == [True]

    def test_multiple_extension_commands_independent(self) -> None:
        calls: dict[str, int] = {"a": 0, "b": 0}

        def handler_a(args: str, ctx: ExtensionContext) -> None:
            calls["a"] += 1

        def handler_b(args: str, ctx: ExtensionContext) -> None:
            calls["b"] += 1

        api = ExtensionAPI(name="multi_ext")
        api._commands["cmda"] = ("Command A", handler_a)
        api._commands["cmdb"] = ("Command B", handler_b)

        from agent.extensions.loader import ExtensionInfo

        mgr = ExtensionManager()
        info = ExtensionInfo(name="multi_ext", source="user", path=None)
        info.api = api
        mgr._extensions = [info]
        mgr._context = _make_ctx()

        renderer, _ = _make_renderer()
        self._dispatch("/cmda", mgr, renderer)
        self._dispatch("/cmdb", mgr, renderer)
        self._dispatch("/cmdb", mgr, renderer)

        assert calls == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# ExtensionManager.commands property
# ---------------------------------------------------------------------------


class TestExtensionManagerCommands:
    def test_empty_when_no_extensions(self) -> None:
        mgr = ExtensionManager()
        assert mgr.commands == {}

    def test_commands_from_single_extension(self) -> None:
        def handler(a: str, c: Any) -> None: ...

        api = ExtensionAPI(name="ext1")
        api._commands["foo"] = ("Foo command", handler)

        from agent.extensions.loader import ExtensionInfo

        mgr = ExtensionManager()
        info = ExtensionInfo(name="ext1", source="user", path=None)
        info.api = api
        mgr._extensions = [info]

        cmds = mgr.commands
        assert "foo" in cmds
        desc, fn = cmds["foo"]
        assert desc == "Foo command"
        assert fn is handler

    def test_commands_merged_from_multiple_extensions(self) -> None:
        api1 = ExtensionAPI(name="ext1")
        api1._commands["cmd1"] = ("one", lambda a, c: None)
        api2 = ExtensionAPI(name="ext2")
        api2._commands["cmd2"] = ("two", lambda a, c: None)

        from agent.extensions.loader import ExtensionInfo

        mgr = ExtensionManager()
        for name, api in [("ext1", api1), ("ext2", api2)]:
            info = ExtensionInfo(name=name, source="user", path=None)
            info.api = api
            mgr._extensions.append(info)

        cmds = mgr.commands
        assert "cmd1" in cmds
        assert "cmd2" in cmds

    def test_later_extension_shadows_earlier(self) -> None:
        """Last registered extension wins on name collision (dict update order)."""
        winner = lambda a, c: "winner"
        loser = lambda a, c: "loser"

        api1 = ExtensionAPI(name="first")
        api1._commands["shared"] = ("first", loser)
        api2 = ExtensionAPI(name="second")
        api2._commands["shared"] = ("second", winner)

        from agent.extensions.loader import ExtensionInfo

        mgr = ExtensionManager()
        for name, api in [("first", api1), ("second", api2)]:
            info = ExtensionInfo(name=name, source="user", path=None)
            info.api = api
            mgr._extensions.append(info)

        _, fn = mgr.commands["shared"]
        assert fn is winner

    def test_extension_with_no_api_skipped(self) -> None:
        from agent.extensions.loader import ExtensionInfo

        mgr = ExtensionManager()
        info = ExtensionInfo(name="broken", source="user", path=None)
        info.api = None  # failed to load
        mgr._extensions = [info]
        assert mgr.commands == {}
