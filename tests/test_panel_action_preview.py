"""UIAction.preview — what a destructive panel action would do, before it runs.

Covers:
- run_ui_preview: no hook, sync / async hooks, a failing hook never blocks
- fixed TUI: the ConfirmModal message carries the preview (and renders "[" literally)
- ACP stdio ``_aar/panel_action`` with ``preview: true`` → preview only, no action
- ACP HTTP ``POST …/actions/{action}`` with ``"preview": true`` → same
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent.extensions.api import UIAction, UINode, run_ui_preview
from tests.conftest import MockProvider
from tests.test_extension_panels import _make_app, make_manager, make_panel


def _panel_with_preview(calls: list[Any]) -> tuple[Any, dict[str, int]]:
    panel, state = make_panel(calls)
    undo = panel.action("undo")
    assert undo is not None
    undo.preview = lambda node, _ctx: f"drops {node.data['i']} [checkpoint] · +3 −1"
    return panel, state


class TestRunUiPreview:
    async def test_none_sync_async_and_failure(self) -> None:
        node = UINode("n", "n", "checkpoint")

        def handler(_inv: Any) -> None:
            return None

        action = UIAction("a", "a", "a", ("checkpoint",), handler)
        assert await run_ui_preview(action, node, None) == ""

        action.preview = lambda n, _ctx: f"preview {n.id}"
        assert await run_ui_preview(action, node, None) == "preview n"

        async def async_preview(n: UINode, _ctx: Any) -> str:
            return "async"

        action.preview = async_preview
        assert await run_ui_preview(action, node, None) == "async"

        def broken(_n: UINode, _ctx: Any) -> str:
            raise RuntimeError("boom")

        action.preview = broken
        assert await run_ui_preview(action, node, None) == ""
        assert action.to_dict()["preview"] is True


class TestFixedTuiConfirm:
    async def test_confirm_modal_shows_preview(self) -> None:
        from agent.transports.tui_widgets.extension_panel import ConfirmModal, ExtensionPanel

        calls: list[Any] = []
        panel, _ = _panel_with_preview(calls)
        app = _make_app(panel)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+b")
            await pilot.press("down")  # → cp:2
            await pilot.pause()
            assert app.query_one(ExtensionPanel).selected_node().id == "cp:2"  # type: ignore[union-attr]
            await pilot.press("u")
            await pilot.pause()
            await asyncio.sleep(0.05)
            modal = app.screen
            assert isinstance(modal, ConfirmModal)
            text = str(modal.query_one(".message").render())
            assert "Undo cp 2?" in text
            assert "drops 2 [checkpoint] · +3 −1" in text  # literal, not markup
            await pilot.press("n")
            await pilot.pause()
            assert calls == []


class TestAcpStdioPreview:
    @pytest.mark.asyncio
    async def test_preview_does_not_run_the_action(self, tmp_path) -> None:
        from tests.test_acp_panels import _ExtCaptureClient, _session_with_panel
        from tests.test_acp_wire import _AcpPair, _make_aar_sdk_agent, _make_config

        calls: list[Any] = []
        panel, state = _panel_with_preview(calls)
        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        async with _AcpPair(agent, _ExtCaptureClient()) as (_, client_side):
            sid = await _session_with_panel(agent, client_side, panel)
            rpc = client_side._conn  # type: ignore[attr-defined]
            listed = await rpc.send_request("_aar/panel_list", {"sessionId": sid})
            assert listed["panels"][0]["actions"][0]["preview"] is True
            out = await rpc.send_request(
                "_aar/panel_action",
                {
                    "sessionId": sid,
                    "panel": "demo",
                    "action": "undo",
                    "nodeId": "cp:1",
                    "preview": True,
                },
            )
            assert out == {
                "panel": "demo",
                "action": "undo",
                "preview": "drops 1 [checkpoint] · +3 −1",
            }
            assert calls == [] and state["n"] == 2


class TestAcpHttpPreview:
    @pytest.mark.asyncio
    async def test_preview_does_not_run_the_action(self) -> None:
        from tests.test_acp_http_panels import _call_asgi, _start_session
        from tests.test_acp_http_panels import _make_app as make_http_app

        provider = MockProvider()
        app = make_http_app(provider)
        sid = await _start_session(app, provider)
        calls: list[Any] = []
        panel, state = _panel_with_preview(calls)
        app.transport._agents[sid]._extension_manager = make_manager(panel)

        status, out = await _call_asgi(
            app,
            "POST",
            f"/sessions/{sid}/panels/demo/actions/undo",
            {"node_id": "cp:2", "preview": True},
        )
        assert status == 200, out
        assert out["preview"] == "drops 2 [checkpoint] · +3 −1"
        assert "message" not in out
        assert calls == [] and state["n"] == 2
