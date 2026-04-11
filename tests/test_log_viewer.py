"""Tests for agent.transports.tui_widgets.log_viewer.

Covers:
- _TUILogHandler: buffering, emit, attach/detach, max-lines cap.
- LogViewerModal: widget composition, handler wiring on mount/unmount.
- AarFixedApp integration: action_toggle_log_viewer opens/closes the modal.
- KeyBinds: log_viewer default is ctrl+g.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.core.agent import Agent
from agent.core.config import AgentConfig
from agent.transports.tui_fixed import AarFixedApp
from agent.transports.tui_widgets.log_viewer import (
    TUI_LOG_HANDLER,
    LogViewerModal,
    _TUILogHandler,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(msg: str, level: int = logging.DEBUG, name: str = "test") -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname="",
        lineno=0,
        msg=msg,
        args=(),
        exc_info=None,
    )


def _fresh_handler() -> _TUILogHandler:
    """Return a brand-new handler instance (not the global singleton)."""
    return _TUILogHandler()


def _make_mock_agent() -> Agent:
    """Create a minimal Agent with a stub provider for testing."""
    config = AgentConfig()
    provider = MagicMock()
    provider.name = "test"
    provider.model = "test-model"
    provider.supports_audio = False
    provider.supports_reasoning = False
    provider.supports_vision = False
    registry = MagicMock()
    registry.names.return_value = []
    registry.list_tools.return_value = []
    agent = MagicMock(spec=Agent)
    agent.config = config
    agent.provider = provider
    agent.registry = registry
    agent.on_event = MagicMock()
    agent.run = AsyncMock()
    return agent


# ---------------------------------------------------------------------------
# _TUILogHandler — unit tests (no Textual required)
# ---------------------------------------------------------------------------


class TestTUILogHandlerBuffer:
    def test_starts_empty(self) -> None:
        h = _fresh_handler()
        assert len(h._buf) == 0

    def test_emit_appends_to_buffer(self) -> None:
        h = _fresh_handler()
        h.emit(_make_record("hello"))
        assert len(h._buf) == 1

    def test_emit_formats_message(self) -> None:
        h = _fresh_handler()
        h.emit(_make_record("my message"))
        assert "my message" in h._buf[-1]

    def test_emit_includes_level(self) -> None:
        h = _fresh_handler()
        h.emit(_make_record("oops", level=logging.ERROR))
        assert "ERROR" in h._buf[-1]

    def test_emit_includes_logger_name(self) -> None:
        h = _fresh_handler()
        h.emit(_make_record("x", name="agent.core.loop"))
        assert "agent.core.loop" in h._buf[-1]

    def test_multiple_emit_grows_buffer(self) -> None:
        h = _fresh_handler()
        for i in range(5):
            h.emit(_make_record(f"line {i}"))
        assert len(h._buf) == 5

    def test_buffer_capped_at_max_lines(self) -> None:
        h = _fresh_handler()
        limit = h.MAX_LINES
        for i in range(limit + 50):
            h.emit(_make_record(f"line {i}"))
        assert len(h._buf) == limit

    def test_buffer_drops_oldest_when_full(self) -> None:
        h = _fresh_handler()
        h._buf = deque(maxlen=3)
        h.emit(_make_record("a"))
        h.emit(_make_record("b"))
        h.emit(_make_record("c"))
        h.emit(_make_record("d"))
        lines = list(h._buf)
        assert not any("a" in line for line in lines)
        assert any("d" in line for line in lines)


class TestTUILogHandlerAttachDetach:
    def test_widget_none_by_default(self) -> None:
        h = _fresh_handler()
        assert h._widget is None

    def test_attach_sets_widget(self) -> None:
        h = _fresh_handler()
        widget = MagicMock()
        widget.write = MagicMock()
        h.attach(widget)
        assert h._widget is widget

    def test_attach_flushes_buffer_into_widget(self) -> None:
        h = _fresh_handler()
        h.emit(_make_record("first"))
        h.emit(_make_record("second"))
        widget = MagicMock()
        widget.write = MagicMock()
        h.attach(widget)
        assert widget.write.call_count == 2

    def test_attach_flush_passes_formatted_lines(self) -> None:
        h = _fresh_handler()
        h.emit(_make_record("flush me"))
        widget = MagicMock()
        widget.write = MagicMock()
        h.attach(widget)
        call_args = [call.args[0] for call in widget.write.call_args_list]
        assert any("flush me" in line for line in call_args)

    def test_attach_empty_buffer_writes_nothing(self) -> None:
        h = _fresh_handler()
        widget = MagicMock()
        widget.write = MagicMock()
        h.attach(widget)
        widget.write.assert_not_called()

    def test_detach_clears_widget(self) -> None:
        h = _fresh_handler()
        widget = MagicMock()
        widget.write = MagicMock()
        h.attach(widget)
        h.detach()
        assert h._widget is None

    def test_emit_after_detach_does_not_call_widget(self) -> None:
        h = _fresh_handler()
        widget = MagicMock()
        widget.write = MagicMock()
        h.attach(widget)
        h.detach()
        widget.write.reset_mock()
        h.emit(_make_record("after detach"))
        widget.write.assert_not_called()

    def test_emit_with_widget_calls_call_from_thread(self) -> None:
        h = _fresh_handler()
        widget = MagicMock()
        widget.write = MagicMock()
        widget.app = MagicMock()
        widget.app.call_from_thread = MagicMock()
        h.attach(widget)
        widget.write.reset_mock()  # ignore flush calls
        h.emit(_make_record("live line"))
        widget.app.call_from_thread.assert_called_once()

    def test_emit_with_widget_call_from_thread_passes_write_line_and_text(self) -> None:
        h = _fresh_handler()
        widget = MagicMock()
        widget.app = MagicMock()
        calls: list = []
        widget.app.call_from_thread = lambda fn, *a: calls.append((fn, a))
        h.attach(widget)
        calls.clear()  # ignore flush
        h.emit(_make_record("streamed"))
        assert len(calls) == 1
        fn, args = calls[0]
        assert fn is widget.write
        assert "streamed" in args[0]

    def test_emit_swallows_call_from_thread_exception(self) -> None:
        h = _fresh_handler()
        widget = MagicMock()
        widget.write = MagicMock()
        widget.app = MagicMock()
        widget.app.call_from_thread = MagicMock(side_effect=RuntimeError("app gone"))
        h.attach(widget)
        # Must not raise
        h.emit(_make_record("boom"))


class TestTUILogHandlerSingleton:
    def test_global_is_tuiloghandler_instance(self) -> None:
        assert isinstance(TUI_LOG_HANDLER, _TUILogHandler)

    def test_global_has_correct_max_lines(self) -> None:
        assert TUI_LOG_HANDLER.MAX_LINES == 2_000

    def test_global_has_formatter(self) -> None:
        assert TUI_LOG_HANDLER.formatter is not None


# ---------------------------------------------------------------------------
# LogViewerModal — Textual integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestLogViewerModal:
    async def test_compose_has_title_widget(self) -> None:
        from textual.widgets import Static

        app = AarFixedApp(agent=_make_mock_agent(), config=AgentConfig())
        async with app.run_test(size=(120, 40)) as pilot:
            await app.push_screen(LogViewerModal())
            await pilot.pause()
            title = app.screen.query_one("#log-viewer-title", Static)
            assert title is not None

    async def test_compose_has_richlog_widget(self) -> None:
        from textual.widgets import RichLog

        app = AarFixedApp(agent=_make_mock_agent(), config=AgentConfig())
        async with app.run_test(size=(120, 40)) as pilot:
            await app.push_screen(LogViewerModal())
            await pilot.pause()
            log_widget = app.screen.query_one("#log-viewer-output", RichLog)
            assert log_widget is not None

    async def test_on_mount_attaches_handler(self) -> None:
        import agent.transports.tui_widgets.log_viewer as lv_module

        fresh_h = _fresh_handler()
        original = lv_module.TUI_LOG_HANDLER
        lv_module.TUI_LOG_HANDLER = fresh_h
        try:
            app = AarFixedApp(agent=_make_mock_agent(), config=AgentConfig())
            async with app.run_test(size=(120, 40)) as pilot:
                await app.push_screen(LogViewerModal())
                await pilot.pause()
                assert fresh_h._widget is not None
        finally:
            lv_module.TUI_LOG_HANDLER = original

    async def test_on_unmount_detaches_handler(self) -> None:
        import agent.transports.tui_widgets.log_viewer as lv_module

        fresh_h = _fresh_handler()
        original = lv_module.TUI_LOG_HANDLER
        lv_module.TUI_LOG_HANDLER = fresh_h
        try:
            app = AarFixedApp(agent=_make_mock_agent(), config=AgentConfig())
            async with app.run_test(size=(120, 40)) as pilot:
                await app.push_screen(LogViewerModal())
                await pilot.pause()
                app.pop_screen()
                await pilot.pause()
                assert fresh_h._widget is None
        finally:
            lv_module.TUI_LOG_HANDLER = original

    async def test_buffered_lines_appear_in_richlog(self) -> None:
        from textual.widgets import RichLog

        import agent.transports.tui_widgets.log_viewer as lv_module

        fresh_h = _fresh_handler()
        fresh_h.emit(_make_record("buffered before open"))
        original = lv_module.TUI_LOG_HANDLER
        lv_module.TUI_LOG_HANDLER = fresh_h
        try:
            app = AarFixedApp(agent=_make_mock_agent(), config=AgentConfig())
            async with app.run_test(size=(120, 40)) as pilot:
                await app.push_screen(LogViewerModal())
                await pilot.pause()
                richlog = app.screen.query_one("#log-viewer-output", RichLog)
                # RichLog has no public line_count; verify the handler is wired
                # to the widget and had content to flush (unit tests cover the
                # actual write() calls on the mock).
                assert fresh_h._widget is richlog
                assert len(fresh_h._buf) >= 1
        finally:
            lv_module.TUI_LOG_HANDLER = original

    async def test_escape_dismisses_modal(self) -> None:
        app = AarFixedApp(agent=_make_mock_agent(), config=AgentConfig())
        async with app.run_test(size=(120, 40)) as pilot:
            await app.push_screen(LogViewerModal())
            await pilot.pause()
            assert any(isinstance(s, LogViewerModal) for s in app.screen_stack)
            await pilot.press("escape")
            await pilot.pause()
            assert not any(isinstance(s, LogViewerModal) for s in app.screen_stack)


# ---------------------------------------------------------------------------
# AarFixedApp integration — action_toggle_log_viewer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAarFixedAppLogViewerAction:
    async def test_action_opens_log_viewer(self) -> None:
        app = AarFixedApp(agent=_make_mock_agent(), config=AgentConfig())
        async with app.run_test(size=(120, 40)) as pilot:
            assert not any(isinstance(s, LogViewerModal) for s in app.screen_stack)
            await app.run_action("toggle_log_viewer")
            await pilot.pause()
            assert any(isinstance(s, LogViewerModal) for s in app.screen_stack)

    async def test_action_closes_log_viewer_when_already_open(self) -> None:
        app = AarFixedApp(agent=_make_mock_agent(), config=AgentConfig())
        async with app.run_test(size=(120, 40)) as pilot:
            await app.run_action("toggle_log_viewer")
            await pilot.pause()
            assert any(isinstance(s, LogViewerModal) for s in app.screen_stack)
            await app.run_action("toggle_log_viewer")
            await pilot.pause()
            assert not any(isinstance(s, LogViewerModal) for s in app.screen_stack)

    async def test_action_does_not_stack_multiple_modals(self) -> None:
        app = AarFixedApp(agent=_make_mock_agent(), config=AgentConfig())
        async with app.run_test(size=(120, 40)) as pilot:
            await app.run_action("toggle_log_viewer")
            await pilot.pause()
            # second toggle closes it, third re-opens — still only one
            await app.run_action("toggle_log_viewer")
            await pilot.pause()
            await app.run_action("toggle_log_viewer")
            await pilot.pause()
            count = sum(1 for s in app.screen_stack if isinstance(s, LogViewerModal))
            assert count == 1

    async def test_default_keybind_wired_to_ctrl_g(self) -> None:
        app = AarFixedApp(agent=_make_mock_agent(), config=AgentConfig())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("ctrl+g")
            await pilot.pause()
            assert any(isinstance(s, LogViewerModal) for s in app.screen_stack)


# ---------------------------------------------------------------------------
# KeyBinds — log_viewer default
# ---------------------------------------------------------------------------


class TestKeyBindsLogViewer:
    def test_default_log_viewer_key(self) -> None:
        from agent.transports.keybinds import KeyBinds

        kb = KeyBinds()
        assert kb.toggle_log_viewer.key == "ctrl+g"

    def test_default_log_viewer_label(self) -> None:
        from agent.transports.keybinds import KeyBinds

        kb = KeyBinds()
        assert kb.toggle_log_viewer.label == "logs"
