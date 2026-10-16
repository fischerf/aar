"""Tool-result notes, command argument hints, and Markdown-safe ACP replies.

Covers:
- add_tool_result_note / tool_result_notes on ToolResult (and refusal on str)
- ExtensionAPI.command(hint=...) → ExtensionManager.command_hints
- ACP: _available_commands sends hints as AvailableCommand.input
- ACP: format_command_reply fences plain-text trees, leaves the rest alone
- ACP stdio: an extension's tool_result note rides on the tool-call card;
  an extension command's tree reply arrives fenced
- inline TUI / fixed TUI: notes shown under the tool result (also when the
  result itself is hidden by the layout)
"""

from __future__ import annotations

import asyncio
import logging
from io import StringIO
from typing import Any
from unittest.mock import AsyncMock

import pytest
from rich.console import Console

from agent.core.config import AgentConfig
from agent.core.events import ToolResult
from agent.core.session import Session
from agent.extensions.api import (
    ExtensionAPI,
    ExtensionContext,
    add_tool_result_note,
    tool_result_notes,
)
from agent.extensions.loader import ExtensionInfo
from agent.extensions.manager import ExtensionManager
from agent.transports.acp.common import _available_commands, format_command_reply
from agent.transports.themes.models import LayoutConfig, SectionConfig
from agent.transports.tui import TUIRenderer
from tests.conftest import MockProvider


def _manager(*apis: ExtensionAPI) -> ExtensionManager:
    mgr = ExtensionManager()
    mgr._extensions = [ExtensionInfo(name=a.name, source="user", path=None, api=a) for a in apis]
    mgr._context = ExtensionContext(
        session=Session(),
        config=AgentConfig(),
        signal=asyncio.Event(),
        logger=logging.getLogger("test.notes"),
    )
    return mgr


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class TestNotesContract:
    def test_add_and_read_notes(self) -> None:
        tr = ToolResult(tool_name="write_file", output="ok")
        assert tool_result_notes(tr) == []
        assert add_tool_result_note(tr, "⎇ checkpoint t1")
        assert add_tool_result_note(tr, "second")
        assert tool_result_notes(tr) == ["⎇ checkpoint t1", "second"]
        assert tr.data["notes"] == ["⎇ checkpoint t1", "second"]

    def test_refuses_non_events_and_empty_text(self) -> None:
        assert add_tool_result_note("replaced output", "x") is False
        tr = ToolResult()
        assert add_tool_result_note(tr, "") is False
        assert tool_result_notes("replaced output") == []

    def test_notes_are_not_sent_to_the_model(self) -> None:
        from agent.core.events import AssistantMessage, StopReason, ToolCall

        session = Session()
        session.append(ToolCall(tool_name="t", tool_call_id="c1", arguments={}))
        session.append(AssistantMessage(content="", stop_reason=StopReason.TOOL_USE))
        tr = ToolResult(tool_name="t", tool_call_id="c1", output="done")
        add_tool_result_note(tr, "SECRET-NOTE")
        session.append(tr)
        assert "SECRET-NOTE" not in str(session.to_messages())


class TestCommandHints:
    def test_hint_registered_and_merged(self) -> None:
        a = ExtensionAPI("a")

        @a.command("undo", description="Undo", hint="[N] [--force]")
        def _undo(args: str, ctx: Any) -> str:
            return ""

        @a.command("plain", description="No hint")
        def _plain(args: str, ctx: Any) -> str:
            return ""

        assert _manager(a).command_hints == {"undo": "[N] [--force]"}

        # A later extension redefining /undo without a hint drops the old one.
        b = ExtensionAPI("b")
        b.command("undo", description="Other undo")(lambda args, ctx: "")
        assert _manager(a, b).command_hints == {}

    def test_available_commands_carry_input_hint(self) -> None:
        cmds = _available_commands({"undo": "Undo", "branches": "List"}, {"undo": "[N]"})
        by_name = {c.name: c for c in cmds}
        assert by_name["undo"].input is not None
        assert by_name["undo"].input.root.hint == "[N]"
        assert by_name["branches"].input is None
        assert by_name["status"].input is None


class TestFormatCommandReply:
    def test_tree_is_fenced(self) -> None:
        tree = "⎇ session s1\n├─ ● shadow\n│  └─ t1 write_file\n└─ ✎ pending"
        out = format_command_reply(tree)
        assert out.startswith("```text\n") and out.endswith("\n```")
        assert tree in out

    def test_indented_listing_is_fenced(self) -> None:
        assert format_command_reply("branches:\n  a\n  b").startswith("```text")

    @pytest.mark.parametrize(
        "text",
        [
            "↩ reverted 1 checkpoint(s) → abc1234",
            "**Active:** x\n- item\n- item",
            "```text\n├─ already fenced\n```",
            "line one\nline two",
        ],
    )
    def test_other_replies_unchanged(self, text: str) -> None:
        assert format_command_reply(text) == text


# ---------------------------------------------------------------------------
# Inline TUI
# ---------------------------------------------------------------------------


def _renderer(layout: LayoutConfig | None = None) -> tuple[TUIRenderer, StringIO]:
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    return TUIRenderer(console=console, layout=layout or LayoutConfig()), buf


class TestInlineTuiNotes:
    def test_note_under_result(self) -> None:
        renderer, buf = _renderer()
        tr = ToolResult(tool_name="write_file", output="wrote a.txt")
        add_tool_result_note(tr, "⎇ checkpoint t3 · abc1234 · 1 file +2 −0")
        renderer.render_event(tr)
        out = buf.getvalue()
        assert "wrote a.txt" in out
        assert "⎇ checkpoint t3 · abc1234 · 1 file +2 −0" in out

    def test_note_shown_when_result_hidden(self) -> None:
        renderer, buf = _renderer(LayoutConfig(tool_result=SectionConfig(visible=False)))
        tr = ToolResult(tool_name="write_file", output="wrote a.txt")
        add_tool_result_note(tr, "⎇ checkpoint t1")
        renderer.render_event(tr)
        out = buf.getvalue()
        assert "wrote a.txt" not in out
        assert "⎇ checkpoint t1" in out


# ---------------------------------------------------------------------------
# Fixed TUI
# ---------------------------------------------------------------------------


class TestFixedTuiNotes:
    async def test_note_under_result(self) -> None:
        from agent.transports.tui_fixed import AarFixedApp
        from agent.transports.tui_widgets.chat_body import ChatBody
        from tests.test_prompt_queue_tui import _make_mock_agent

        app = AarFixedApp(agent=_make_mock_agent(), config=AgentConfig())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            tr = ToolResult(tool_name="write_file", output="wrote a.txt")
            add_tool_result_note(tr, "⎇ checkpoint t2 · def5678")
            app._renderer.render_event(tr)  # type: ignore[union-attr]
            await pilot.pause()
            text = app.query_one("#chat-body", ChatBody).get_all_text()
            assert "⎇ checkpoint t2 · def5678" in text


# ---------------------------------------------------------------------------
# ACP stdio
# ---------------------------------------------------------------------------


class TestAcpStdio:
    async def test_tool_result_note_on_tool_call_card(self, tmp_path) -> None:
        from acp.schema import ToolCallProgress

        from agent.memory.session_store import SessionStore
        from tests.test_acp import _make_config, _make_sdk_agent

        provider = MockProvider()
        provider.enqueue_tool_call("no_such_tool", {}, tool_call_id="tc_note")
        provider.enqueue_text("done")
        sdk_agent = _make_sdk_agent(provider)
        sdk_agent._config = _make_config().model_copy(update={"session_dir": tmp_path})
        sdk_agent._store = SessionStore(tmp_path)
        mock_conn = AsyncMock()
        sdk_agent._conn = mock_conn

        api = ExtensionAPI("noter")

        @api.on("tool_result")
        def _note(event: Any, ctx: Any) -> None:
            add_tool_result_note(event, "⎇ checkpoint t1 · abc1234")

        original_make = sdk_agent._make_aar_agent

        def make_with_ext(*args: Any, **kwargs: Any) -> Any:
            agent = original_make(*args, **kwargs)
            agent._extension_manager = _manager(api)
            return agent

        sdk_agent._make_aar_agent = make_with_ext  # type: ignore[method-assign]

        r = await sdk_agent.new_session()
        await sdk_agent.prompt(prompt=[{"text": "go"}], session_id=r.session_id)

        updates = [c.kwargs["update"] for c in mock_conn.session_update.call_args_list]
        final = [
            u
            for u in updates
            if isinstance(u, ToolCallProgress)
            and u.tool_call_id == "tc_note"
            and u.status in ("completed", "failed")
        ]
        assert final, "tool call never finished"
        texts = [getattr(getattr(c, "content", None), "text", "") for c in final[-1].content or []]
        assert texts[-1] == "⎇ checkpoint t1 · abc1234"

    async def test_extension_tree_reply_is_fenced(self, tmp_path) -> None:
        from acp.schema import AgentMessageChunk

        from agent.memory.session_store import SessionStore
        from tests.test_acp import _make_config, _make_sdk_agent

        sdk_agent = _make_sdk_agent(MockProvider())
        sdk_agent._config = _make_config().model_copy(update={"session_dir": tmp_path})
        sdk_agent._store = SessionStore(tmp_path)
        mock_conn = AsyncMock()
        sdk_agent._conn = mock_conn
        r = await sdk_agent.new_session()

        api = ExtensionAPI("tree")
        api.command("branches", description="tree")(lambda a, c: "⎇ s\n├─ one\n└─ two")
        sdk_agent._extension_managers[r.session_id] = _manager(api)

        await sdk_agent.prompt(prompt=[{"text": "/branches"}], session_id=r.session_id)
        updates = [c.kwargs["update"] for c in mock_conn.session_update.call_args_list]
        texts = [u.content.text for u in updates if isinstance(u, AgentMessageChunk)]
        assert "```text\n⎇ s\n├─ one\n└─ two\n```" in texts


class TestReplyRendering:
    def test_reply_lines_are_literal(self) -> None:
        from agent.transports.tui_utils.ui_tree import reply_lines

        lines = reply_lines("x = items[0]\n[bold]not markup[/]")
        assert [ln.plain for ln in lines] == ["x = items[0]", "[bold]not markup[/]"]
        assert all(not ln.spans and not ln.style for ln in lines)

    def test_diff_lines_are_coloured(self) -> None:
        from agent.transports.tui_utils.ui_tree import reply_lines

        diff = "t3 write_file\ndiff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n ctx"
        styles = {ln.plain: str(ln.style) for ln in reply_lines(diff)}
        assert styles["+new"] == "green" and styles["-old"] == "red"
        assert styles["@@ -1 +1 @@"] == "cyan" and styles["+++ b/x"] == "bold"
        assert styles[" ctx"] == "" and styles["t3 write_file"] == ""

    def test_acp_fences_diffs_and_stats(self) -> None:
        diff = "t3\ndiff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new"
        assert format_command_reply(diff) == f"```diff\n{diff}\n```"
        stat = 'p1 "x" · 2 checkpoint(s)\n a.py | 2 +-\n 1 file changed'
        assert format_command_reply(stat).startswith("```text\n")
