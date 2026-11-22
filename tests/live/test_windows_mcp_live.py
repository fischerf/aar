"""Live tests for tools/windows_mcp against real programs (Windows only, opt-in).

    AAR_SANDBOX=1 pytest tests/live/test_windows_mcp_live.py --live

They drive a real desktop (start programs, type, click), so besides ``--live``
they need ``AAR_SANDBOX=1`` (set by the Windows Sandbox harness) or
``AAR_DESKTOP_TESTS=1``. Everything goes through the server over real MCP
stdio. A test skips if its program is already running so none of the user's
windows are touched, and afterwards kills only the processes it started.

* Notepad: launch, find the editor, set/read text, close. Skipped when this
  Windows has no Notepad (e.g. a fresh Windows 11 Sandbox).
* System Information (msinfo32, classic Win32, on every Windows): every tool.
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows desktop only"),
    pytest.mark.skipif(
        not (os.environ.get("AAR_SANDBOX") or os.environ.get("AAR_DESKTOP_TESTS")),
        reason="drives the real desktop; set AAR_DESKTOP_TESTS=1 (or run in the sandbox)",
    ),
]

REPO = Path(__file__).resolve().parents[2]
SERVER = (
    "import sys; sys.path.insert(0, 'tools'); "
    "from windows_mcp.server import mcp; mcp.run(transport='stdio')"
)
NOTEPAD_STATE = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Packages"
    / "Microsoft.WindowsNotepad_8wekyb3d8bbwe"
    / "LocalState"
)
TEXT = "aar windows-mcp {ok} (1) äöü"
LAUNCH_LINE = re.compile(r"^(\d+)\s+(\S+)\s+pid=(\d+)", re.MULTILINE)


def _pids(image: str) -> set[int]:
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
        capture_output=True,
        encoding="oem",
        errors="replace",
        stdin=subprocess.DEVNULL,
        check=False,
    ).stdout
    return {int(r[1]) for r in csv.reader(io.StringIO(out)) if len(r) > 1 and r[1].isdigit()}


def _kill(pids: set[int]) -> None:
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)


def _state_files() -> set[Path]:
    dirs = [NOTEPAD_STATE / "TabState", NOTEPAD_STATE / "WindowState"]
    return {p for d in dirs if d.is_dir() for p in d.iterdir() if p.is_file()}


class Session:
    def __init__(self, session):
        self.session = session

    async def raw(self, tool, **args):
        res = await self.session.call_tool(tool, args)
        text = "\n".join(c.text for c in res.content if c.type == "text")
        return res, text

    async def __call__(self, tool, **args):
        res, text = await self.raw(tool, **args)
        assert not res.isError, f"{tool}({args}) failed: {text}"
        return text


def run_session(steps):
    async def main():
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", SERVER],
            cwd=str(REPO),
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            return await steps(Session(session))

    return asyncio.run(main())


def _launched(text: str) -> tuple[str, int]:
    m = LAUNCH_LINE.search(text)
    assert m, text
    return m.group(1), int(m.group(3))


def test_notepad_set_and_read_text():
    pytest.importorskip("mcp")
    pytest.importorskip("uiautomation")
    if _pids("notepad.exe"):
        pytest.skip("Notepad is already running; not touching the user's windows")
    files_before = _state_files()
    started: set[int] = set()

    async def steps(call):
        res, text = await call.raw("launch_app", name_or_path="notepad.exe")
        if res.isError:
            # pytest.skip() inside the MCP task group would surface as an ExceptionGroup.
            assert "Could not find a program" in text or "could not start" in text, text
            return f"no Notepad on this Windows: {text}"
        handle, pid = _launched(text)
        started.add(pid)

        tree = await call("get_ui_tree", window=handle, max_depth=6)
        editor = re.search(r"^\s*(e\d+) (Document|Edit)\b", tree, re.MULTILINE)
        assert editor, tree
        eid, ctype = editor.groups()

        await call("set_text", window=handle, element=eid, text=TEXT)
        assert TEXT in await call("read_text", window=handle, element=eid)
        # The selector fallback reaches the same control.
        assert TEXT in await call("read_text", window=handle, element=f"type={ctype}")
        await call("close_window", window=handle)  # may stay open on a save prompt
        return None

    try:
        skipped = run_session(steps)
    finally:
        # Only processes we started: the window's pid plus any launcher stub
        # that appeared after the pre-check showed no Notepad running.
        _kill(started | _pids("notepad.exe"))
        time.sleep(1.0)
        for path in _state_files() - files_before:
            path.unlink(missing_ok=True)
    assert not _pids("notepad.exe")
    if skipped:
        pytest.skip(skipped)


def _checkbox(tree: str, name: str | None = None):
    """First enabled checkbox (or the one called *name*): ``(id, name, states)``."""
    for m in re.finditer(r'^\s*(e\d+) CheckBox "([^"]*)".*\[([^\]]*)\]', tree, re.MULTILINE):
        states = m.group(3).split(",")
        if (name is None and "disabled" not in states) or m.group(2) == name:
            return m.group(1), m.group(2), states
    return None


def test_msinfo32_all_tools():
    # System Information is a classic Win32 program on every Windows edition
    # (Character Map would be smaller, but it exits at once in Windows Sandbox).
    pytest.importorskip("mcp")
    pytest.importorskip("uiautomation")
    if _pids("msinfo32.exe"):
        pytest.skip("System Information is already running; not touching the user's windows")
    started: set[int] = set()

    async def steps(call):
        handle, pid = _launched(await call("launch_app", name_or_path="msinfo32"))
        started.add(pid)
        assert handle in await call("list_windows")
        assert "foreground" in await call("focus_window", window=handle)

        # The UI may be localized: find controls by type, not by name.
        tree = await call("get_ui_tree", window=handle, max_depth=12, max_nodes=600)
        edit = re.search(r"^\s*(e\d+) Edit\b", tree, re.MULTILINE)
        assert edit, tree
        eid = edit.group(1)

        await call("set_text", window=handle, element=eid, text="abc")
        assert await call("read_text", window=handle, element=eid) == "abc"
        await call("set_text", window=handle, element="type=Edit", text="xyz")  # selector
        assert await call("read_text", window=handle, element=eid) == "xyz"

        await call("send_keys", window=handle, keys="{End}12", element=eid)
        assert await call("read_text", window=handle, element=eid) == "xyz12"
        assert "xyz12" in await call("read_text", window=handle)  # whole window

        box = _checkbox(tree)
        assert box, tree
        bid, name, states = box
        await call("click_element", window=handle, element=bid)
        tree = await call("get_ui_tree", window=handle, max_depth=12, max_nodes=600)
        after = _checkbox(tree, name)
        assert after and ("on" in after[2]) != ("on" in states), (states, after)

        res = await call.session.call_tool("screenshot", {"window": handle, "max_width": 400})
        assert not res.isError
        image = [c for c in res.content if c.type == "image"]
        assert image and image[0].mimeType == "image/png"

        # Clear errors for the model.
        res, text = await call.raw("click_element", window=handle, element="e9999")
        assert res.isError and "get_ui_tree again" in text, text
        res, text = await call.raw("launch_app", name_or_path="no-such-program-aar")
        assert res.isError and "Could not find a program" in text, text

        closed = await call("close_window", window=handle)
        assert "Closed" in closed or "still open" in closed, closed

    try:
        run_session(steps)
    finally:
        _kill(started | _pids("msinfo32.exe"))
    assert not _pids("msinfo32.exe")
