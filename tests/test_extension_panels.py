"""Extension UI panels — contract, manager merge, and the fixed-TUI widget.

Covers:
- UINode/UIAction/UIPanel helpers (to_dict, find, applies_to, actions_for)
- ExtensionAPI.register_panel + ExtensionManager.panels
- run_ui_action / run_ui_snapshot with sync (threaded) and async handlers
- _sync_right_col: the right column collapses only when every child is hidden
- ExtensionPanel in AarFixedApp: hidden by default, ctrl+b toggle, escape
  hides, keys ignored without focus, destructive action → ConfirmModal,
  non-mutating actions allowed while the agent runs, rebuild keeps the
  cursor, header chip from UIPanel.status, Mutated → transcript re-render
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from textual.widgets import Button

from agent.core.config import AgentConfig
from agent.core.events import UserMessage
from agent.core.session import Session
from agent.extensions.api import (
    ExtensionAPI,
    ExtensionContext,
    UIAction,
    UIInvocation,
    UINode,
    UIPanel,
    run_ui_action,
    run_ui_snapshot,
)
from agent.extensions.loader import ExtensionInfo
from agent.extensions.manager import ExtensionManager
from agent.transports.tui_fixed import AarFixedApp, _sync_right_col
from agent.transports.tui_widgets.bars import HeaderBar
from agent.transports.tui_widgets.chat_body import ChatBody
from agent.transports.tui_widgets.extension_panel import (
    BUSY_HINT,
    ConfirmModal,
    ExtensionPanel,
    PanelTitle,
    PanelWindow,
)
from agent.transports.tui_widgets.input import HistoryTextArea
from agent.transports.tui_widgets.thinking_panel import ThinkingPanel
from tests.test_prompt_queue_tui import _make_mock_agent

# ---------------------------------------------------------------------------
# Fake panel
# ---------------------------------------------------------------------------


def make_panel(calls: list[tuple[str, str, dict[str, Any]]]) -> tuple[UIPanel, dict[str, int]]:
    """A panel with a branch of N checkpoints; ``undo`` shrinks N by one."""
    state = {"n": 2}

    def snapshot(_ctx: Any) -> UINode:
        kids = [
            UINode(f"cp:{i}", f"cp {i}", "checkpoint", data={"i": i})
            for i in range(state["n"], 0, -1)
        ]
        return UINode(
            "root",
            "root",
            "root",
            children=[
                UINode("br", "branch", "branch", children=kids),
                UINode("info", "clean", "info", style="dim"),
            ],
        )

    def undo(inv: UIInvocation) -> str:
        calls.append(("undo", inv.node.id, dict(inv.args)))
        state["n"] = max(0, state["n"] - 1)
        return f"undone {inv.node.id}"

    async def refresh(inv: UIInvocation) -> None:
        calls.append(("refresh", inv.node.id, {}))
        return None

    panel = UIPanel(
        name="demo",
        title="Demo",
        snapshot=snapshot,
        status=lambda _ctx: f"demo {state['n']}",
        actions=[
            UIAction(
                "undo",
                "undo",
                "u",
                ("checkpoint",),
                undo,
                destructive=True,
                confirm="Undo {label}?",
                inputs=("force",),
            ),
            UIAction(
                "refresh",
                "refresh",
                "r",
                ("root", "branch", "checkpoint", "info"),
                refresh,
                mutates=False,
            ),
        ],
    )
    return panel, state


def make_manager(*panels: UIPanel) -> ExtensionManager:
    api = ExtensionAPI("demo")
    for p in panels:
        api.register_panel(p)
    mgr = ExtensionManager()
    mgr._extensions = [ExtensionInfo(name="demo", source="user", path=None, api=api)]
    mgr._context = ExtensionContext(
        session=Session(),
        config=AgentConfig(),
        signal=asyncio.Event(),
        logger=logging.getLogger("test.panels"),
    )
    return mgr


def _make_app(panel: UIPanel) -> AarFixedApp:
    agent = _make_mock_agent()
    agent._extension_manager = make_manager(panel)
    return AarFixedApp(agent=agent, config=AgentConfig())


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class TestContract:
    def test_register_panel_and_manager_merge(self) -> None:
        p1, _ = make_panel([])
        p2 = UIPanel(name="other", title="Other", snapshot=lambda ctx: UINode("r", "r"))
        mgr = make_manager(p1, p2)
        assert set(mgr.panels) == {"demo", "other"}
        assert mgr.panels["demo"] is p1

    def test_manager_skips_failed_extensions(self) -> None:
        mgr = ExtensionManager()
        mgr._extensions = [ExtensionInfo(name="broken", source="user", path=None, api=None)]
        assert mgr.panels == {}

    def test_register_panel_twice_replaces(self) -> None:
        api = ExtensionAPI("demo")
        a = UIPanel(name="x", title="A", snapshot=lambda ctx: UINode("r", "r"))
        b = UIPanel(name="x", title="B", snapshot=lambda ctx: UINode("r", "r"))
        api.register_panel(a)
        api.register_panel(b)
        assert [p.title for p in api._panels] == ["B"]

    def test_uinode_to_dict_and_find(self) -> None:
        leaf = UINode("leaf", "Leaf", "checkpoint", data={"n": 1}, style="active")
        root = UINode("root", "Root", "root", children=[UINode("mid", "Mid", "branch", [leaf])])
        assert root.find("leaf") is leaf
        assert root.find("nope") is None
        d = root.to_dict()
        assert d["children"][0]["children"][0] == {
            "id": "leaf",
            "label": "Leaf",
            "detail": "",
            "kind": "checkpoint",
            "expanded": True,
            "style": "active",
            "data": {"n": 1},
            "children": [],
        }

    def test_actions_for_and_applies_to(self) -> None:
        panel, _ = make_panel([])
        cp = UINode("cp:1", "cp", "checkpoint")
        assert [a.id for a in panel.actions_for(cp)] == ["undo", "refresh"]
        assert [a.id for a in panel.actions_for(UINode("i", "i", "info"))] == ["refresh"]
        assert panel.actions_for(None) == []
        assert panel.action("undo").applies_to(cp)  # type: ignore[union-attr]
        assert panel.action("missing") is None
        assert panel.action("undo").to_dict()["inputs"] == ["force"]  # type: ignore[union-attr]

    def test_status_text_swallows_errors(self) -> None:
        def boom(_ctx: Any) -> str:
            raise RuntimeError("nope")

        panel = UIPanel(name="x", title="X", snapshot=lambda ctx: UINode("r", "r"), status=boom)
        assert panel.status_text(None) == ""
        assert (
            UIPanel(name="y", title="Y", snapshot=lambda ctx: UINode("r", "r")).status_text(None)
            == ""
        )

    async def test_run_ui_action_sync_and_async(self) -> None:
        calls: list[tuple[str, str, dict[str, Any]]] = []
        panel, _ = make_panel(calls)
        node = UINode("cp:2", "cp 2", "checkpoint")
        msg = await run_ui_action(
            panel.action("undo"),  # type: ignore[arg-type]
            UIInvocation(node=node, ctx=None, args={"force": True}),
        )
        assert msg == "undone cp:2"
        assert calls[-1] == ("undo", "cp:2", {"force": True})
        assert (
            await run_ui_action(
                panel.action("refresh"),  # type: ignore[arg-type]
                UIInvocation(node=node, ctx=None),
            )
            is None
        )
        assert calls[-1][0] == "refresh"

    async def test_run_ui_snapshot_sync_async_and_type_error(self) -> None:
        panel, _ = make_panel([])
        root = await run_ui_snapshot(panel, None)
        assert root.id == "root"

        async def async_snap(_ctx: Any) -> UINode:
            return UINode("async", "async")

        assert (
            await run_ui_snapshot(UIPanel(name="a", title="A", snapshot=async_snap), None)
        ).id == "async"
        with pytest.raises(TypeError):
            await run_ui_snapshot(UIPanel(name="b", title="B", snapshot=lambda ctx: "x"), None)


# ---------------------------------------------------------------------------
# Right column visibility
# ---------------------------------------------------------------------------


class TestRightCol:
    @staticmethod
    def _col(*displays: str) -> SimpleNamespace:
        children = [SimpleNamespace(styles=SimpleNamespace(display=d)) for d in displays]
        return SimpleNamespace(children=children, styles=SimpleNamespace(display="block"))

    def test_collapses_only_when_all_hidden(self) -> None:
        col = self._col("none", "block")
        _sync_right_col(col)
        assert col.styles.display == "block"
        col = self._col("none", "none")
        _sync_right_col(col)
        assert col.styles.display == "none"
        col = self._col("block")
        col.styles.display = "none"
        _sync_right_col(col)
        assert col.styles.display == "block"

    def test_tolerates_garbage(self) -> None:
        _sync_right_col(object())  # no children / styles — must not raise


# ---------------------------------------------------------------------------
# Widget inside AarFixedApp
# ---------------------------------------------------------------------------


class TestExtensionPanelWidget:
    async def test_no_panels_means_no_widget(self) -> None:
        app = AarFixedApp(agent=_make_mock_agent(), config=AgentConfig())
        async with app.run_test(size=(120, 40)):
            assert list(app.query(ExtensionPanel)) == []
            assert app.query_one(HeaderBar).panel_status == ""

    async def test_sidebar_shown_by_default_toggle_and_escape(self) -> None:
        panel, _ = make_panel([])
        app = _make_app(panel)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            widget = app.query_one(ExtensionPanel)
            left_col = app.query_one("#left-col")
            # The sidebar sits left of the chat body, in its own column.
            assert widget.parent is left_col
            assert widget.mode == "sidebar"
            order = [c.id for c in app.query_one("#body-split").children]
            assert order.index("left-col") < order.index("chat-body")
            assert widget.styles.display == "block"
            assert left_col.styles.display != "none"
            assert widget.root is not None and widget.root.id == "root"
            assert not widget.has_focus_within

            await pilot.press("ctrl+b")  # visible, unfocused → focus
            await pilot.pause()
            assert widget.has_focus_within

            await pilot.press("ctrl+b")  # focused → back to input
            await pilot.pause()
            assert widget.styles.display == "block"
            assert app.query_one("#user-input", HistoryTextArea).has_focus

            await pilot.press("ctrl+b")  # visible, unfocused → focus again
            await pilot.pause()
            assert widget.has_focus_within

            await pilot.press("escape")
            await pilot.pause()
            assert widget.styles.display == "none"
            assert left_col.styles.display == "none", "nothing left in the sidebar"
            assert app.query_one("#user-input", HistoryTextArea).has_focus

            await pilot.press("ctrl+b")  # hidden → shown + focused
            await pilot.pause()
            assert widget.styles.display == "block"
            assert left_col.styles.display == "block"
            assert widget.has_focus_within

    async def test_layout_config_can_hide_sidebar(self) -> None:
        from agent.transports.themes.models import LayoutConfig, SectionConfig

        panel, _ = make_panel([])
        agent = _make_mock_agent()
        agent._extension_manager = make_manager(panel)
        layout = LayoutConfig(extensions={"demo": SectionConfig(visible=False)})
        app = AarFixedApp(agent=agent, config=AgentConfig(), layout_config=layout)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.query_one(ExtensionPanel).styles.display == "none"
            assert app.query_one("#left-col").styles.display == "none"

    async def test_ctrl_k_and_sidebar_are_independent(self) -> None:
        panel, _ = make_panel([])
        app = _make_app(panel)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            right_col = app.query_one("#right-col")
            left_col = app.query_one("#left-col")
            thinking = app.query_one(ThinkingPanel)

            await pilot.press("ctrl+k")
            await pilot.pause()
            assert thinking.styles.display == "none"
            assert right_col.styles.display == "none", "only the thinking panel lived there"
            assert left_col.styles.display == "block", "sidebar unaffected"

    async def test_header_chip_from_status(self) -> None:
        panel, _ = make_panel([])
        app = _make_app(panel)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app._refresh_panels(force=True)
            assert app.query_one(HeaderBar).panel_status == "demo 2"

    async def test_keys_ignored_without_focus(self) -> None:
        calls: list[tuple[str, str, dict[str, Any]]] = []
        panel, _ = make_panel(calls)
        app = _make_app(panel)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("ctrl+b")
            await pilot.press("ctrl+b")  # back to input, panel still visible
            await pilot.pause()
            await pilot.press("r")
            await pilot.press("u")
            await pilot.pause()
            assert calls == []

    async def test_rebuild_keeps_cursor(self) -> None:
        panel, _ = make_panel([])
        app = _make_app(panel)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("ctrl+b")
            await pilot.pause()
            widget = app.query_one(ExtensionPanel)
            await pilot.press("down")  # branch → cp:2
            await pilot.press("down")  # cp:2 → cp:1
            await pilot.pause()
            assert widget.selected_node().id == "cp:1"  # type: ignore[union-attr]
            await widget.refresh_tree()
            await pilot.pause()
            assert widget.selected_node().id == "cp:1"  # type: ignore[union-attr]

    async def test_destructive_action_confirms(self) -> None:
        calls: list[tuple[str, str, dict[str, Any]]] = []
        panel, state = make_panel(calls)
        app = _make_app(panel)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("ctrl+b")
            await pilot.pause()
            widget = app.query_one(ExtensionPanel)
            await pilot.press("down")
            await pilot.pause()
            assert widget.selected_node().kind == "checkpoint"  # type: ignore[union-attr]

            await pilot.press("u")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("n")
            await pilot.pause()
            assert calls == [] and state["n"] == 2

            await pilot.press("u")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("f")  # toggle force
            await pilot.press("y")
            await pilot.pause()
            await asyncio.sleep(0.1)
            assert calls == [("undo", "cp:2", {"force": True})]
            assert state["n"] == 1
            body = app.query_one("#chat-body", ChatBody)
            assert "undone cp:2" in body.get_all_text()
            # Tree was refreshed: only one checkpoint left
            assert [c.id for c in widget.root.children[0].children] == ["cp:1"]  # type: ignore[union-attr]

    async def test_mutating_action_blocked_while_agent_runs(self) -> None:
        calls: list[tuple[str, str, dict[str, Any]]] = []
        panel, _ = make_panel(calls)
        app = _make_app(panel)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("ctrl+b")
            await pilot.pause()
            app._agent_running = True
            await pilot.press("down")
            await pilot.press("r")  # non-mutating → allowed
            await pilot.pause()
            await asyncio.sleep(0.05)
            assert calls == [("refresh", "cp:2", {})]
            await pilot.press("u")  # mutating → refused, no modal
            await pilot.pause()
            await asyncio.sleep(0.05)
            assert not isinstance(app.screen, ConfirmModal)
            assert len(calls) == 1
            assert BUSY_HINT in app.query_one("#chat-body", ChatBody).get_all_text()

    async def test_mutated_rerenders_transcript(self) -> None:
        panel, _ = make_panel([])
        app = _make_app(panel)
        async with app.run_test(size=(120, 40)) as pilot:
            app._session = Session(events=[UserMessage(content="hello from history")])
            widget = app.query_one(ExtensionPanel)
            widget.post_message(ExtensionPanel.Mutated("demo", "undo"))
            await pilot.pause()
            await asyncio.sleep(0.05)
            text = app.query_one("#chat-body", ChatBody).get_all_text()
            assert "hello from history" in text
            assert "transcript reloaded (1 events)" in text


# ---------------------------------------------------------------------------
# Zoomed panel window, describe hook, Rich tree rendering
# ---------------------------------------------------------------------------


class TestDescribe:
    async def test_describe_hook_sync_and_fallback(self) -> None:
        from agent.extensions.api import run_ui_describe

        node = UINode("cp:1", "cp 1", "checkpoint", detail="3 files", data={"i": 1})
        bare, _ = make_panel([])
        text = await run_ui_describe(bare, node, None)
        assert text.splitlines() == ["cp 1", "3 files", "i: 1"]

        bare.describe = lambda n, _ctx: f"described {n.id}"
        assert await run_ui_describe(bare, node, None) == "described cp:1"

        async def empty(_n: UINode, _ctx: Any) -> None:
            return None

        bare.describe = empty  # falls back to the generic summary
        assert (await run_ui_describe(bare, node, None)).startswith("cp 1")


class TestRenderUiTree:
    def _render(self, renderable: Any) -> str:
        from rich.console import Console

        console = Console(width=100, record=True, color_system=None)
        console.print(renderable)
        return console.export_text()

    def test_tree_shows_labels_details_status_and_collapsed(self) -> None:
        from agent.transports.tui_utils.ui_tree import render_ui_tree

        root = UINode(
            "root",
            "session s1",
            "root",
            children=[
                UINode(
                    "br",
                    "shadow",
                    "branch",
                    detail="3 cp",
                    children=[
                        UINode(f"cp:{i}", f"t{i} write_file", "checkpoint") for i in range(8)
                    ],
                ),
                UINode(
                    "sib",
                    "branch-1",
                    "branch",
                    expanded=False,
                    children=[UINode("cp:x", "t1", "checkpoint")],
                ),
            ],
        )
        out = self._render(
            render_ui_tree(root, title="⎇ Shadow [x]", status="2 cp", max_children=5)
        )
        assert "⎇ Shadow [x]" in out  # literal, not parsed as markup
        assert "2 cp" in out
        assert "shadow  3 cp" in out
        assert "t4 write_file" in out and "t5 write_file" not in out
        assert "… 3 more" in out
        assert "branch-1  ▸ 1 hidden" in out

    def test_no_detail_when_disabled(self) -> None:
        from agent.transports.tui_utils.ui_tree import node_text

        node = UINode("n", "label", detail="extra", style="warn")
        assert node_text(node).plain == "label  extra"
        assert node_text(node, with_detail=False).plain == "label"


class TestPanelWindow:
    async def test_z_zooms_and_escape_closes(self) -> None:
        panel, _ = make_panel([])
        panel.describe = lambda n, _ctx: f"about {n.id}"
        app = _make_app(panel)
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+b")
            await pilot.press("down")  # branch → cp:2
            await pilot.pause()
            await pilot.press("z")
            await pilot.pause()
            await asyncio.sleep(0.15)
            await pilot.pause()
            assert isinstance(app.screen, PanelWindow)
            win = app.screen.panel_widget
            assert win.mode == "window"
            # The window opens on the node that was selected in the sidebar
            # and describes it in the detail pane.
            assert win.selected_node().id == "cp:2"  # type: ignore[union-attr]
            detail = str(win.query_one("#panel-detail").render())
            assert "about cp:2" in detail
            # One button per applicable action.
            ids = sorted(b.id for b in win.query(Button))
            assert ids == ["panel-act-refresh", "panel-act-undo"]

            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, PanelWindow)
            assert app._panel_window is None
            assert app.query_one(ExtensionPanel).has_focus_within

    async def test_title_click_zooms_and_ctrl_b_closes(self) -> None:
        panel, _ = make_panel([])
        app = _make_app(panel)
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.pause()
            await pilot.click(PanelTitle)
            await pilot.pause()
            assert isinstance(app.screen, PanelWindow)
            await pilot.press("ctrl+b")
            await pilot.pause()
            assert not isinstance(app.screen, PanelWindow)

    async def test_button_click_runs_action_and_updates_both_views(self) -> None:
        calls: list[tuple[str, str, dict[str, Any]]] = []
        panel, state = make_panel(calls)
        app = _make_app(panel)
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.pause()
            app.query_one(ExtensionPanel).post_message(
                ExtensionPanel.Zoom(app.query_one(ExtensionPanel))
            )
            await pilot.pause()
            await pilot.press("down")  # → cp:2
            await pilot.pause()
            await asyncio.sleep(0.1)
            await pilot.click("#panel-act-undo")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("y")
            await pilot.pause()
            await asyncio.sleep(0.1)
            await pilot.pause()
            assert calls == [("undo", "cp:2", {"force": False})]
            assert state["n"] == 1
            assert isinstance(app.screen, PanelWindow)
            # The result line reached the chat body under the modal.
            body = app.screen_stack[0].query_one("#chat-body", ChatBody)
            assert "undone cp:2" in body.get_all_text()
            await pilot.press("escape")
            await pilot.pause()
            await asyncio.sleep(0.05)
            sidebar = app.query_one(ExtensionPanel)
            assert [c.id for c in sidebar.root.children[0].children] == ["cp:1"]  # type: ignore[union-attr]

    async def test_sidebar_title_shows_status(self) -> None:
        panel, _ = make_panel([])
        app = _make_app(panel)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app._refresh_panels(force=True)
            widget = app.query_one(ExtensionPanel)
            assert widget.status == "demo 2"
            title = str(widget.query_one(PanelTitle).render())
            assert "Demo" in title and "demo 2" in title


class TestInlineRenderUiPanels:
    def _renderer(self, layout: Any = None) -> tuple[Any, Any]:
        from io import StringIO

        from rich.console import Console

        from agent.transports.themes.models import LayoutConfig
        from agent.transports.tui import TUIRenderer

        buf = StringIO()
        console = Console(file=buf, force_terminal=False, width=100, color_system=None)
        return TUIRenderer(console=console, layout=layout or LayoutConfig()), buf

    async def test_panel_command_renders_full_tree(self) -> None:
        panel, _ = make_panel([])
        renderer, buf = self._renderer()
        shown = await renderer.render_ui_panels({"demo": panel}, None)
        out = buf.getvalue()
        assert shown == 1
        assert "Demo" in out and "demo 2" in out  # title + status chip
        assert "cp 2" in out and "cp 1" in out and "clean" in out
        assert "/panel demo" not in out  # the hint is for the compact auto-render

    async def test_unknown_name_shows_nothing(self) -> None:
        panel, _ = make_panel([])
        renderer, buf = self._renderer()
        assert await renderer.render_ui_panels({"demo": panel}, None, name="nope") == 0
        assert buf.getvalue() == ""
        assert await renderer.render_ui_panels({"demo": panel}, None, name="Demo") == 1

    async def test_only_changed_renders_once_and_clears(self) -> None:
        panel, _ = make_panel([])
        renderer, buf = self._renderer()
        assert await renderer.render_ui_panels({"demo": panel}, None, only_changed=True) == 0
        panel.changed.set()
        assert await renderer.render_ui_panels({"demo": panel}, None, only_changed=True) == 1
        assert not panel.changed.is_set()
        assert "cp 2" in buf.getvalue()
        assert "/panel demo — full tree" in buf.getvalue()
        assert await renderer.render_ui_panels({"demo": panel}, None, only_changed=True) == 0

    async def test_layout_hides_auto_render_but_not_panel_command(self) -> None:
        from agent.transports.themes.models import LayoutConfig, SectionConfig

        panel, _ = make_panel([])
        renderer, _buf = self._renderer(
            LayoutConfig(extensions={"demo": SectionConfig(visible=False)})
        )
        panel.changed.set()
        assert await renderer.render_ui_panels({"demo": panel}, None, only_changed=True) == 0
        assert not panel.changed.is_set()
        assert await renderer.render_ui_panels({"demo": panel}, None) == 1
