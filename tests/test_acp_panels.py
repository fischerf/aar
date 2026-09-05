"""Wire-level tests for extension UI panels over ACP stdio.

Custom methods (the SDK adds/strips the ``_`` prefix):

* ``_aar/panel_list``      → panels + their actions
* ``_aar/panel_snapshot``  → the panel's UINode tree
* ``_aar/panel_action``    → run an action on a node, return message + new tree
* ``_aar/panel_changed``   ← notification after a prompt when a panel is stale

The extension manager is swapped for one holding the fake panel from
``tests.test_extension_panels`` so no real extension needs to be installed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.conftest import MockProvider
from tests.test_acp_wire import _AcpPair, _CaptureClient, _make_aar_sdk_agent, _make_config
from tests.test_extension_panels import make_manager, make_panel

pytest.importorskip("acp")

from acp.exceptions import RequestError  # noqa: E402
from acp.schema import TextContentBlock  # noqa: E402


class _ExtCaptureClient(_CaptureClient):
    """Also records custom ``_aar/*`` notifications."""

    def __init__(self) -> None:
        super().__init__()
        self.ext_notifications: list[tuple[str, dict[str, Any]]] = []

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        self.ext_notifications.append((method, params))


async def _wait(pred: Any, timeout: float = 2.0) -> bool:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


async def _session_with_panel(agent: Any, client_side: Any, panel: Any) -> str:
    await client_side.initialize(protocol_version=1)
    sess = await client_side.new_session(cwd="/ws", mcp_servers=[])
    agent._extension_managers[sess.session_id] = make_manager(panel)
    return sess.session_id


class TestPanelMethods:
    @pytest.mark.asyncio
    async def test_list_snapshot_action_roundtrip(self, tmp_path):
        calls: list[tuple[str, str, dict[str, Any]]] = []
        panel, state = make_panel(calls)
        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        client = _ExtCaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            sid = await _session_with_panel(agent, client_side, panel)
            rpc = client_side._conn  # type: ignore[attr-defined]

            listed = await rpc.send_request("_aar/panel_list", {"sessionId": sid})
            assert [p["name"] for p in listed["panels"]] == ["demo"]
            demo = listed["panels"][0]
            assert demo["title"] == "Demo" and demo["status"] == "demo 2"
            assert [a["id"] for a in demo["actions"]] == ["undo", "refresh"]
            assert demo["actions"][0]["destructive"] is True
            assert demo["actions"][0]["inputs"] == ["force"]

            snap = await rpc.send_request(
                "_aar/panel_snapshot", {"sessionId": sid, "panel": "demo"}
            )
            assert snap["root"]["id"] == "root"
            branch = snap["root"]["children"][0]
            assert [c["id"] for c in branch["children"]] == ["cp:2", "cp:1"]

            acted = await rpc.send_request(
                "_aar/panel_action",
                {
                    "sessionId": sid,
                    "panel": "demo",
                    "action": "undo",
                    "nodeId": "cp:2",
                    "args": {"force": True},
                },
            )
            assert acted["message"] == "undone cp:2"
            assert calls == [("undo", "cp:2", {"force": True})]
            assert state["n"] == 1
            assert [c["id"] for c in acted["root"]["children"][0]["children"]] == ["cp:1"]
            assert acted["status"] == "demo 1"

            # snake_case params are accepted too (handy for scripts).
            acted2 = await rpc.send_request(
                "_aar/panel_action",
                {"session_id": sid, "panel": "demo", "action": "refresh", "node_id": "root"},
            )
            assert acted2["message"] is None
            assert calls[-1][0] == "refresh"

    @pytest.mark.asyncio
    async def test_errors_are_invalid_params(self, tmp_path):
        panel, _ = make_panel([])
        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        client = _ExtCaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            sid = await _session_with_panel(agent, client_side, panel)
            rpc = client_side._conn  # type: ignore[attr-defined]

            async def expect_error(method: str, params: dict[str, Any]) -> None:
                with pytest.raises((RequestError, Exception)):
                    await asyncio.wait_for(rpc.send_request(method, params), timeout=2.0)

            await expect_error("_aar/panel_list", {})  # missing session
            await expect_error("_aar/panel_list", {"sessionId": "nope"})
            await expect_error("_aar/panel_snapshot", {"sessionId": sid, "panel": "ghost"})
            await expect_error(
                "_aar/panel_action",
                {"sessionId": sid, "panel": "demo", "action": "explode", "nodeId": "root"},
            )
            await expect_error(
                "_aar/panel_action",
                {"sessionId": sid, "panel": "demo", "action": "undo", "nodeId": "missing"},
            )
            # undo applies to checkpoints only
            await expect_error(
                "_aar/panel_action",
                {"sessionId": sid, "panel": "demo", "action": "undo", "nodeId": "root"},
            )
            await expect_error("_aar/other_thing", {"sessionId": sid})

    @pytest.mark.asyncio
    async def test_panel_changed_notification_after_prompt(self, tmp_path):
        panel, _ = make_panel([])
        provider = MockProvider()
        provider.enqueue_text("ok", stop="end_turn")
        agent = _make_aar_sdk_agent(_make_config(tmp_path), provider)
        client = _ExtCaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            sid = await _session_with_panel(agent, client_side, panel)
            panel.changed.set()  # the extension would do this from a hook

            resp = await client_side.prompt(
                session_id=sid, prompt=[TextContentBlock(type="text", text="Hi")]
            )
            assert resp.stop_reason == "end_turn"

            assert await _wait(lambda: bool(client.ext_notifications))
            method, params = client.ext_notifications[-1]
            assert method == "aar/panel_changed"
            assert params == {"sessionId": sid, "panel": "demo"}

            # Fetching a snapshot clears the flag, so the next prompt is quiet.
            rpc = client_side._conn  # type: ignore[attr-defined]
            await rpc.send_request("_aar/panel_snapshot", {"sessionId": sid, "panel": "demo"})
            assert not panel.changed.is_set()
