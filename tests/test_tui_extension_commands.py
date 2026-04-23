"""Tests for TUI extension slash-command dispatch, dynamic welcome screen,
and the /inspect extension command.

Covers:
- TUIRenderer.render_welcome lists all built-in commands (including /help)
- TUIRenderer.render_welcome includes extension commands when passed
- Extension commands are dispatched correctly from the TUI
- /help re-renders the welcome screen
- Unknown slash commands produce a friendly error, not an LLM call
- ExtensionManager.commands merges / shadows correctly
- /inspect reports real session data (regression: stale bootstrap session)
- /inspect counts events by type correctly
- /inspect verbose mode includes event detail
- update_session() refreshes context with live session
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from io import StringIO
from typing import Any
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from agent.core.events import (
    AssistantMessage,
    ErrorEvent,
    ProviderMeta,
    SessionEvent,
    ToolCall,
    ToolResult,
    UserMessage,
)
from agent.core.session import Session
from agent.extensions.api import ExtensionAPI, ExtensionContext
from agent.extensions.loader import ExtensionInfo
from agent.extensions.manager import ExtensionManager
from agent.transports.themes.builtin import DEFAULT_THEME
from agent.transports.themes.models import LayoutConfig, SectionConfig
from agent.transports.tui import TUIRenderer

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_renderer(layout: LayoutConfig | None = None) -> tuple[TUIRenderer, StringIO]:
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, highlight=False, markup=True, width=120)
    renderer = TUIRenderer(console=console, theme=DEFAULT_THEME, layout=layout or LayoutConfig())
    return renderer, buf


def _strip_markup(text: str) -> str:
    import re

    return re.sub(r"\[/?[^\]]+\]", "", text)


def _make_ctx(session: Session | None = None) -> ExtensionContext:
    return ExtensionContext(
        session=session or Session(),
        config=MagicMock(),
        signal=asyncio.Event(),
        logger=logging.getLogger("aar.ext.test"),
    )


def _make_manager(cmds: dict[str, tuple[str, Any]] | None = None) -> ExtensionManager:
    """Build an ExtensionManager with an optional dict of {name: (desc, handler)}."""
    api = ExtensionAPI(name="test_ext")
    for name, (desc, fn) in (cmds or {}).items():
        api._commands[name] = (desc, fn)
    mgr = ExtensionManager()
    info = ExtensionInfo(name="test_ext", source="user", path=None)
    info.api = api
    mgr._extensions = [info]
    mgr._context = _make_ctx()
    return mgr


def _dispatch(
    input_text: str,
    mgr: ExtensionManager,
    renderer: TUIRenderer,
) -> bool:
    """Simulate the extension slash-command dispatch block from run_tui.
    Returns True if the command was handled."""
    stripped = input_text.strip()
    if not stripped.startswith("/"):
        return False
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


def _build_session(*events: Any) -> Session:
    """Create a Session pre-populated with the given events."""
    s = Session()
    for ev in events:
        s.append(ev)
    return s


# ---------------------------------------------------------------------------
# render_welcome — built-in commands
# ---------------------------------------------------------------------------


class TestRenderWelcomeBuiltins:
    def test_contains_all_builtin_commands(self) -> None:
        renderer, buf = _make_renderer()
        renderer.render_welcome()
        text = _strip_markup(buf.getvalue())
        for cmd in ("help", "quit", "status", "tools", "policy", "theme", "clear"):
            assert f"/{cmd}" in text, f"Expected built-in command /{cmd} in welcome output"

    def test_help_listed(self) -> None:
        renderer, buf = _make_renderer()
        renderer.render_welcome()
        assert "/help" in _strip_markup(buf.getvalue())

    def test_no_extension_commands_by_default(self) -> None:
        renderer, buf = _make_renderer()
        renderer.render_welcome()
        assert "/inspect" not in _strip_markup(buf.getvalue())

    def test_hidden_layout_suppresses_output(self) -> None:
        layout = LayoutConfig(welcome=SectionConfig(visible=False))
        renderer, buf = _make_renderer(layout=layout)
        renderer.render_welcome()
        assert buf.getvalue().strip() == ""

    def test_idempotent_multiple_calls(self) -> None:
        renderer, buf = _make_renderer()
        renderer.render_welcome()
        first = buf.getvalue()
        buf.truncate(0)
        buf.seek(0)
        renderer.render_welcome()
        second = buf.getvalue()
        assert _strip_markup(first) == _strip_markup(second)


# ---------------------------------------------------------------------------
# render_welcome — extension commands appended
# ---------------------------------------------------------------------------


class TestRenderWelcomeExtensionCommands:
    def test_extra_commands_appear(self) -> None:
        renderer, buf = _make_renderer()
        renderer.render_welcome(extra_commands=["inspect", "git_checkpoint"])
        text = _strip_markup(buf.getvalue())
        assert "/inspect" in text
        assert "/git_checkpoint" in text

    def test_extra_commands_after_builtins(self) -> None:
        renderer, buf = _make_renderer()
        renderer.render_welcome(extra_commands=["inspect"])
        text = _strip_markup(buf.getvalue())
        assert "/quit" in text and "/inspect" in text
        assert text.index("/inspect") > text.index("/quit")

    def test_none_same_as_empty_list(self) -> None:
        r1, b1 = _make_renderer()
        r1.render_welcome(extra_commands=None)
        r2, b2 = _make_renderer()
        r2.render_welcome(extra_commands=[])
        assert _strip_markup(b1.getvalue()) == _strip_markup(b2.getvalue())

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
    def test_registered_command_dispatched(self) -> None:
        called: list[tuple[str, Any]] = []
        mgr = _make_manager({"inspect": ("desc", lambda a, c: called.append((a, c)))})
        renderer, _ = _make_renderer()
        handled = _dispatch("/inspect", mgr, renderer)
        assert handled is True
        assert len(called) == 1
        assert called[0][0] == ""

    def test_args_forwarded(self) -> None:
        received: list[str] = []
        mgr = _make_manager({"inspect": ("", lambda a, c: received.append(a))})
        _dispatch("/inspect verbose", mgr, _make_renderer()[0])
        assert received == ["verbose"]

    def test_multi_word_args(self) -> None:
        received: list[str] = []
        mgr = _make_manager({"cmd": ("", lambda a, c: received.append(a))})
        _dispatch("/cmd foo bar baz", mgr, _make_renderer()[0])
        assert received == ["foo bar baz"]

    def test_return_value_printed(self) -> None:
        mgr = _make_manager({"myext": ("", lambda a, c: "hello from extension")})
        renderer, buf = _make_renderer()
        _dispatch("/myext", mgr, renderer)
        assert "hello from extension" in buf.getvalue()

    def test_none_return_prints_nothing(self) -> None:
        mgr = _make_manager({"myext": ("", lambda a, c: None)})
        renderer, buf = _make_renderer()
        _dispatch("/myext", mgr, renderer)
        assert buf.getvalue().strip() == ""

    def test_unknown_command_not_handled(self) -> None:
        mgr = _make_manager({"known": ("", lambda a, c: None)})
        handled = _dispatch("/unknown_xyz", mgr, _make_renderer()[0])
        assert handled is False

    def test_exception_in_handler_no_crash(self) -> None:
        def bad(a: str, c: Any) -> None:
            raise ValueError("boom")

        mgr = _make_manager({"badcmd": ("", bad)})
        renderer, buf = _make_renderer()
        handled = _dispatch("/badcmd", mgr, renderer)
        assert handled is True
        out = buf.getvalue().lower()
        assert "boom" in out or "error" in out

    def test_case_insensitive(self) -> None:
        called: list[bool] = []
        mgr = _make_manager({"inspect": ("", lambda a, c: called.append(True))})
        _dispatch("/INSPECT", mgr, _make_renderer()[0])
        assert called == [True]

    def test_mixed_case_with_args(self) -> None:
        received: list[str] = []
        mgr = _make_manager({"cmd": ("", lambda a, c: received.append(a))})
        _dispatch("/CMD some args", mgr, _make_renderer()[0])
        assert received == ["some args"]

    def test_multiple_commands_independent(self) -> None:
        calls: dict[str, int] = {"a": 0, "b": 0}
        api = ExtensionAPI(name="multi")
        api._commands["cmda"] = ("A", lambda a, c: calls.__setitem__("a", calls["a"] + 1))
        api._commands["cmdb"] = ("B", lambda a, c: calls.__setitem__("b", calls["b"] + 1))
        mgr = ExtensionManager()
        info = ExtensionInfo(name="multi", source="user", path=None)
        info.api = api
        mgr._extensions = [info]
        mgr._context = _make_ctx()
        renderer, _ = _make_renderer()
        _dispatch("/cmda", mgr, renderer)
        _dispatch("/cmdb", mgr, renderer)
        _dispatch("/cmdb", mgr, renderer)
        assert calls == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# ExtensionManager.commands property
# ---------------------------------------------------------------------------


class TestExtensionManagerCommandsProperty:
    def test_empty_when_no_extensions(self) -> None:
        assert ExtensionManager().commands == {}

    def test_single_extension(self) -> None:
        fn = lambda a, c: None
        api = ExtensionAPI(name="e1")
        api._commands["foo"] = ("Foo", fn)
        mgr = ExtensionManager()
        info = ExtensionInfo(name="e1", source="user", path=None)
        info.api = api
        mgr._extensions = [info]
        cmds = mgr.commands
        assert "foo" in cmds
        desc, handler = cmds["foo"]
        assert desc == "Foo"
        assert handler is fn

    def test_merged_from_multiple_extensions(self) -> None:
        api1, api2 = ExtensionAPI(name="e1"), ExtensionAPI(name="e2")
        api1._commands["cmd1"] = ("one", lambda a, c: None)
        api2._commands["cmd2"] = ("two", lambda a, c: None)
        mgr = ExtensionManager()
        for name, api in [("e1", api1), ("e2", api2)]:
            info = ExtensionInfo(name=name, source="user", path=None)
            info.api = api
            mgr._extensions.append(info)
        cmds = mgr.commands
        assert "cmd1" in cmds and "cmd2" in cmds

    def test_later_extension_shadows_earlier(self) -> None:
        winner = lambda a, c: "winner"
        loser = lambda a, c: "loser"
        api1 = ExtensionAPI(name="first")
        api1._commands["shared"] = ("first", loser)
        api2 = ExtensionAPI(name="second")
        api2._commands["shared"] = ("second", winner)
        mgr = ExtensionManager()
        for name, api in [("first", api1), ("second", api2)]:
            info = ExtensionInfo(name=name, source="user", path=None)
            info.api = api
            mgr._extensions.append(info)
        _, fn = mgr.commands["shared"]
        assert fn is winner

    def test_broken_extension_skipped(self) -> None:
        mgr = ExtensionManager()
        info = ExtensionInfo(name="broken", source="user", path=None)
        info.api = None
        mgr._extensions = [info]
        assert mgr.commands == {}


# ---------------------------------------------------------------------------
# update_session() — stale-context regression
# ---------------------------------------------------------------------------


class TestUpdateSession:
    def test_update_session_replaces_context_session(self) -> None:
        mgr = ExtensionManager()
        bootstrap = Session()
        mgr._context = _make_ctx(bootstrap)

        live = _build_session(
            UserMessage(content="hello"),
            AssistantMessage(content="hi"),
        )
        live.step_count = 1
        mgr.update_session(live)

        assert mgr._context is not None
        assert mgr._context.session is live
        assert mgr._context.session.step_count == 1
        assert len(mgr._context.session.events) == 2

    def test_update_session_noop_when_no_context(self) -> None:
        mgr = ExtensionManager()
        mgr._context = None
        # Should not raise
        mgr.update_session(Session())

    def test_update_session_preserves_other_fields(self) -> None:
        original_config = MagicMock()
        original_signal = asyncio.Event()
        original_logger = logging.getLogger("aar.ext.original")
        mgr = ExtensionManager()
        mgr._context = ExtensionContext(
            session=Session(),
            config=original_config,
            signal=original_signal,
            logger=original_logger,
        )
        new_session = Session()
        mgr.update_session(new_session)
        assert mgr._context.config is original_config
        assert mgr._context.signal is original_signal
        assert mgr._context.logger is original_logger

    def test_stale_session_regression(self) -> None:
        """Regression: /inspect must see the live session, not the bootstrap."""
        bootstrap = Session()  # empty — as created before the loop
        mgr = ExtensionManager()
        mgr._context = _make_ctx(bootstrap)

        # Simulate a few turns of conversation
        live = Session()
        live.append(UserMessage(content="what is 2+2?"))
        live.append(AssistantMessage(content="4"))
        live.append(UserMessage(content="and 3+3?"))
        live.append(AssistantMessage(content="6"))
        live.step_count = 2

        # This is what agent.run() now calls before dispatching commands
        mgr.update_session(live)

        # Verify the inspect command sees the live data
        seen_steps: list[int] = []
        seen_event_count: list[int] = []

        def spy_inspect(args: str, ctx: ExtensionContext) -> None:
            seen_steps.append(ctx.session.step_count)
            seen_event_count.append(len(ctx.session.events))

        mgr_api = ExtensionAPI(name="inspect_test")
        mgr_api._commands["inspect"] = ("inspect", spy_inspect)
        info = ExtensionInfo(name="inspect_test", source="user", path=None)
        info.api = mgr_api
        mgr._extensions = [info]

        _dispatch("/inspect", mgr, _make_renderer()[0])
        assert seen_steps == [2], "inspect should see live step_count=2, not bootstrap 0"
        assert seen_event_count == [4], "inspect should see 4 events, not 0"


# ---------------------------------------------------------------------------
# /inspect extension integration
# ---------------------------------------------------------------------------


def _run_inspect(session: Session, args: str = "") -> str:
    """Run the /inspect extension command against the given session and return
    the logged output (joined into one string)."""
    from aar_ext_inspect import register

    api = ExtensionAPI(name="inspect")
    register(api)

    log_records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            log_records.append(record.getMessage())

    handler = _Capture()
    log = logging.getLogger("aar.ext.test_inspect")
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)

    ctx = ExtensionContext(
        session=session,
        config=MagicMock(provider=MagicMock(name="anthropic", model="claude-3-5-sonnet")),
        signal=asyncio.Event(),
        logger=log,
    )

    _, fn = api._commands["inspect"]
    fn(args, ctx)

    log.removeHandler(handler)
    return "\n".join(log_records)


class TestInspectExtensionOutput:
    def test_empty_session_shows_zeros(self) -> None:
        report = _run_inspect(Session())
        assert "Step count : 0" in report
        assert "Total events: 0" in report

    def test_step_count_reported(self) -> None:
        s = Session()
        s.step_count = 5
        report = _run_inspect(s)
        assert "Step count : 5" in report

    def test_session_id_present(self) -> None:
        s = Session()
        report = _run_inspect(s)
        assert s.session_id in report

    def test_user_message_counted(self) -> None:
        s = _build_session(
            UserMessage(content="hello"),
            UserMessage(content="how are you"),
        )
        report = _run_inspect(s)
        assert "User messages     : 2" in report

    def test_assistant_message_counted(self) -> None:
        s = _build_session(
            AssistantMessage(content="I am fine"),
            AssistantMessage(content="Indeed"),
        )
        report = _run_inspect(s)
        assert "Assistant messages: 2" in report

    def test_tool_call_counted(self) -> None:
        s = _build_session(
            ToolCall(tool_name="read_file", arguments={"path": "foo.txt"}),
            ToolCall(tool_name="read_file", arguments={"path": "bar.txt"}),
            ToolCall(tool_name="bash", arguments={"command": "ls"}),
        )
        report = _run_inspect(s)
        assert "Tool calls        : 3" in report

    def test_tool_result_counted(self) -> None:
        s = _build_session(
            ToolResult(tool_name="read_file", output="content"),
            ToolResult(tool_name="read_file", output="more"),
        )
        report = _run_inspect(s)
        assert "Tool results      : 2" in report

    def test_tool_call_name_breakdown(self) -> None:
        s = _build_session(
            ToolCall(tool_name="read_file", arguments={}),
            ToolCall(tool_name="read_file", arguments={}),
            ToolCall(tool_name="bash", arguments={}),
        )
        report = _run_inspect(s)
        assert "read_file: 2×" in report
        assert "bash: 1×" in report

    def test_error_counted(self) -> None:
        s = _build_session(
            ErrorEvent(message="timeout", recoverable=True),
            ErrorEvent(message="fatal", recoverable=False),
        )
        report = _run_inspect(s)
        assert "Errors            : 2" in report

    def test_tool_call_and_result_not_confused(self) -> None:
        """ToolCall and ToolResult both have tool_name — must count separately."""
        s = _build_session(
            ToolCall(tool_name="read_file", arguments={}),
            ToolResult(tool_name="read_file", output="contents"),
        )
        report = _run_inspect(s)
        assert "Tool calls        : 1" in report
        assert "Tool results      : 1" in report

    def test_token_usage_from_session_fields(self) -> None:
        s = Session()
        s.total_input_tokens = 500
        s.total_output_tokens = 200
        report = _run_inspect(s)
        assert "Input tokens : 500" in report
        assert "Output tokens: 200" in report
        assert "Total tokens : 700" in report

    def test_token_usage_from_provider_meta_fallback(self) -> None:
        """When session totals are zero, fall back to summing ProviderMeta events."""
        s = _build_session(
            ProviderMeta(
                provider="openai", model="gpt-4o", usage={"input_tokens": 100, "output_tokens": 50}
            ),
            ProviderMeta(
                provider="openai", model="gpt-4o", usage={"input_tokens": 80, "output_tokens": 40}
            ),
        )
        # session totals are 0 — the fallback path should sum events
        assert s.total_input_tokens == 0
        report = _run_inspect(s)
        assert "Input tokens : 180" in report
        assert "Output tokens: 90" in report

    def test_last_assistant_message_shown(self) -> None:
        s = _build_session(
            AssistantMessage(content="First reply"),
            AssistantMessage(content="Final reply here"),
        )
        report = _run_inspect(s)
        assert "Final reply here" in report
        assert "First reply" not in report

    def test_provider_info_shown(self) -> None:
        s = Session()
        report = _run_inspect(s)
        assert "anthropic" in report.lower()
        assert "claude" in report.lower()

    def test_verbose_includes_event_detail(self) -> None:
        s = _build_session(
            UserMessage(content="hi"),
            AssistantMessage(content="hello"),
        )
        report = _run_inspect(s, args="verbose")
        assert "Event detail" in report
        assert "user_message" in report
        assert "assistant_message" in report

    def test_verbose_not_in_non_verbose(self) -> None:
        s = _build_session(UserMessage(content="hi"))
        report = _run_inspect(s, args="")
        assert "Event detail" not in report

    def test_end_marker_present(self) -> None:
        report = _run_inspect(Session())
        assert "=== End Report ===" in report

    def test_mixed_session(self) -> None:
        """Full realistic session with all event types — smoke test."""
        s = _build_session(
            SessionEvent(action="started"),
            UserMessage(content="List files"),
            ToolCall(tool_name="bash", arguments={"command": "ls"}),
            ToolResult(tool_name="bash", output="README.md\nsrc/"),
            AssistantMessage(content="Here are your files: README.md, src/"),
            ProviderMeta(
                provider="anthropic",
                model="claude-3-5-sonnet",
                usage={"input_tokens": 120, "output_tokens": 60},
            ),
        )
        s.step_count = 1
        report = _run_inspect(s)
        assert "Session ID" in report
        assert "Step count : 1" in report
        assert "Tool calls        : 1" in report
        assert "bash: 1×" in report
        assert "Tool results      : 1" in report
        assert "Here are your files" in report

    def test_full_session_with_update_session(self) -> None:
        """Regression: manager.update_session must propagate to the inspect command."""
        from aar_ext_inspect import register

        api = ExtensionAPI(name="inspect")
        register(api)

        log_records: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, r: logging.LogRecord) -> None:
                log_records.append(r.getMessage())

        handler = _Capture()
        log = logging.getLogger("aar.ext.regression")
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)

        # Bootstrap session (empty — mimics what run_tui creates before loop)
        bootstrap = Session()

        mgr = ExtensionManager()
        mgr._context = ExtensionContext(
            session=bootstrap,
            config=MagicMock(provider=MagicMock(name="test", model="m")),
            signal=asyncio.Event(),
            logger=log,
        )
        info = ExtensionInfo(name="inspect", source="user", path=None)
        info.api = api
        mgr._extensions = [info]

        # Simulate several LLM turns
        live = _build_session(
            SessionEvent(action="started"),
            UserMessage(content="hello"),
            AssistantMessage(content="hi there"),
            UserMessage(content="what time is it?"),
            ToolCall(tool_name="bash", arguments={"command": "date"}),
            ToolResult(tool_name="bash", output="Mon Jun 02 10:00:00 UTC 2025"),
            AssistantMessage(content="It's 10:00 UTC."),
        )
        live.step_count = 2
        live.total_input_tokens = 300
        live.total_output_tokens = 150

        # This is the fix: update before dispatching the slash command
        mgr.update_session(live)

        _, fn = api._commands["inspect"]
        fn("", mgr._context)

        log.removeHandler(handler)
        report = "\n".join(log_records)

        assert "Step count : 2" in report, "Must see live step_count"
        assert "Total events: 7" in report, "Must see all 7 events"
        assert "User messages     : 2" in report
        assert "Assistant messages: 2" in report
        assert "Tool calls        : 1" in report
        assert "bash: 1×" in report
        assert "Input tokens : 300" in report
        assert "Output tokens: 150" in report
        assert "It's 10:00 UTC." in report
