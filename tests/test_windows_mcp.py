"""Unit tests for the platform-independent part of tools/windows_mcp (runs everywhere)."""

from __future__ import annotations

import itertools
import struct
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from windows_mcp.core import (
    NodeInfo,
    Snapshot,
    WindowInfo,
    WinMcpError,
    clean_document_text,
    clean_text,
    encode_png,
    ensure_fresh,
    escape_for_typing,
    find_window,
    interpolate,
    looks_like_path,
    lookup_id,
    match_selector,
    page,
    parse_element,
    parse_handle,
    parse_points,
    pick_start_app,
    render_tree,
    resolve_program,
    to_pixels,
)

# ---------------------------------------------------------------------------
# Window matching
# ---------------------------------------------------------------------------

WINDOWS = [
    WindowInfo(100, "Untitled - Notepad", "Notepad.exe", 1),
    WindowInfo(200, "Budget.xlsx - Excel", "EXCEL.EXE", 2),
    WindowInfo(300, "Report.docx - Word", "WINWORD.EXE", 3),
    WindowInfo(400, "Report.docx - Word (2)", "WINWORD.EXE", 3),
]


def test_parse_handle():
    assert parse_handle("123") == 123
    assert parse_handle(" 0x1F ") == 31
    assert parse_handle("Notepad") is None


def test_find_window_by_handle_title_and_process():
    assert find_window(WINDOWS, "200").handle == 200
    assert find_window(WINDOWS, "0xC8").handle == 200
    assert find_window(WINDOWS, "notepad").handle == 100
    assert find_window(WINDOWS, "BUDGET").handle == 200
    assert find_window(WINDOWS, "excel.exe").handle == 200


def test_find_window_exact_title_beats_substring():
    assert find_window(WINDOWS, "report.docx - word").handle == 300


def test_find_window_ambiguous_lists_candidates():
    with pytest.raises(WinMcpError) as exc:
        find_window(WINDOWS, "Report")
    assert "2 windows match" in str(exc.value) and "300" in str(exc.value)


def test_find_window_missing():
    with pytest.raises(WinMcpError, match="list_windows"):
        find_window(WINDOWS, "Paint")
    with pytest.raises(WinMcpError, match="handle 999"):
        find_window(WINDOWS, "999")


# ---------------------------------------------------------------------------
# Tree rendering (fake backend: nested dicts)
# ---------------------------------------------------------------------------


def node(ctype, name="", aid="", value=None, states=(), children=()):
    # Like the UIA backend's describe(): names go through clean_text.
    info = NodeInfo(ctype, clean_text(name), aid, value, list(states))
    return {"info": info, "children": list(children)}


def render(root, **kw):
    return render_tree(root, lambda n: n["info"], lambda n: n["children"], **kw)


TREE = node(
    "Window",
    "Untitled - Notepad",
    children=[
        node("Pane", children=[node("Document", "Text editor", value="hello")]),
        node(
            "MenuBar",
            aid="MenuBar",
            children=[
                node(
                    "MenuItem",
                    "File",
                    "File",
                    states=["collapsed"],
                    children=[node("Text", "File")],
                ),
                node("MenuItem", "Edit", "Edit"),
            ],
        ),
        node("Button", "", "CloseButton", children=[node("Image")]),
        node("Text", ""),
        node("CheckBox", "Word wrap", states=["off", "disabled"]),
    ],
)


def test_render_flattens_structure_and_drops_noise():
    text, snap = render(TREE)
    lines = text.splitlines()
    assert lines[0] == 'Window "Untitled - Notepad"'
    assert lines[1] == 'e1 Document "Text editor" value="hello"'  # Pane flattened away
    assert lines[2] == "e2 MenuBar #MenuBar"
    assert lines[3] == '  e3 MenuItem "File" [collapsed]'  # automation id == name omitted
    assert lines[5] == "e5 Button #CloseButton"  # glyph-only name removed
    assert lines[6] == 'e6 CheckBox "Word wrap" [off,disabled]'
    assert len(lines) == 7  # repeated label and empty Text dropped
    assert set(snap.elements) == {f"e{i}" for i in range(1, 7)}
    assert snap.elements["e3"][0].name == "File"


def test_render_max_depth_reports_hidden_children():
    text, _ = render(TREE, max_depth=1)
    assert "e2 MenuBar #MenuBar (+2 children, raise max_depth)" in text
    assert "MenuItem" not in text


def test_render_max_nodes_truncates():
    text, snap = render(TREE, max_nodes=2)
    assert len(snap.elements) == 2
    assert "truncated after 2 elements" in text


def test_render_empty_window_hint():
    text, snap = render(node("Window", "Game"))
    assert "no accessible controls" in text
    assert not snap.elements


def test_render_max_visits_bounds_walk():
    wide = node("Window", "Grid", children=[node("Pane") for _ in range(1000)])
    text, _ = render(wide, max_nodes=10, max_visits=50)
    assert "truncated" in text


def test_clean_text():
    assert clean_text("  a\n b\tc ") == "a b c"
    assert clean_text(" Bold") == "Bold"
    assert clean_text("x" * 100, limit=10) == "x" * 9 + "…"
    assert clean_text(None) == ""


# ---------------------------------------------------------------------------
# Element addressing
# ---------------------------------------------------------------------------


def test_parse_element_ids():
    assert parse_element("e12") == ("id", "e12")
    assert parse_element("E7") == ("id", "e7")
    assert parse_element("3") == ("id", "e3")


def test_parse_element_selectors():
    assert parse_element("Save") == ("selector", {"name": "Save"})
    assert parse_element("type=ButtonControl; name=OK") == (
        "selector",
        {"control_type": "Button", "name": "OK"},
    )
    assert parse_element("automation_id=SettingsButton") == (
        "selector",
        {"automation_id": "SettingsButton"},
    )
    # An "=" inside a name that is not a selector key stays a name.
    assert parse_element("a=b") == ("selector", {"name": "a=b"})
    with pytest.raises(WinMcpError):
        parse_element("  ")


def test_lookup_id_errors_tell_model_what_to_do():
    with pytest.raises(WinMcpError, match="get_ui_tree"):
        lookup_id(None, "e1")
    snap = Snapshot({"e1": (NodeInfo("Button", "OK"), object())})
    assert lookup_id(snap, "e1")[0].name == "OK"
    with pytest.raises(WinMcpError, match="e1..e1"):
        lookup_id(snap, "e5")


CANDIDATES = [
    (NodeInfo("Button", "Save"), "save"),
    (NodeInfo("Button", "Save as..."), "saveas"),
    (NodeInfo("MenuItem", "Save"), "menusave"),
    (NodeInfo("Edit", "File name", "FileNameBox"), "edit"),
]


def test_match_selector():
    assert match_selector(CANDIDATES, {"name": "save", "control_type": "Button"})[1] == "save"
    assert match_selector(CANDIDATES, {"name": "save as"})[1] == "saveas"  # substring
    assert match_selector(CANDIDATES, {"automation_id": "filenamebox"})[1] == "edit"
    with pytest.raises(WinMcpError, match="2 elements match"):
        match_selector(CANDIDATES, {"name": "Save"})
    with pytest.raises(WinMcpError, match="No element matches"):
        match_selector(CANDIDATES, {"name": "Print"})


def test_ensure_fresh():
    info = NodeInfo("Button", "OK", "ok")
    ensure_fresh("e1", info, NodeInfo("Button", "OK", "ok", states=["disabled"]))
    with pytest.raises(WinMcpError, match="no longer available"):
        ensure_fresh("e1", info, NodeInfo("Button", "Cancel", "cancel"))
    with pytest.raises(WinMcpError, match="get_ui_tree again"):
        ensure_fresh("e1", info, None)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def test_escape_for_typing():
    assert escape_for_typing("a{b}(c)\r\nd\te") == "a{{}b{}}{(}c{)}{Enter}d{Tab}e"


def test_pick_start_app():
    apps = [
        ("Calculator", "calc!App"),
        ("Calendar", "cal!App"),
        ("Paint", "paint!App"),
        ("Notepad", "notepad!App"),
    ]
    assert pick_start_app(apps, "calculator") == ("Calculator", "calc!App")
    assert pick_start_app(apps, "pai") == ("Paint", "paint!App")
    assert pick_start_app(apps, "notepad.exe") == ("Notepad", "notepad!App")  # .exe ignored
    assert pick_start_app(apps, '"Notepad"') == ("Notepad", "notepad!App")
    assert pick_start_app(apps, "Excel") is None
    assert pick_start_app(apps, "  ") is None
    with pytest.raises(WinMcpError, match="Calculator, Calendar"):
        pick_start_app(apps, "cal")  # ambiguous: say which ones


def _resolver(files=(), on_path=None, app_paths=None):
    on_path = {k.lower(): v for k, v in (on_path or {}).items()}
    app_paths = {k.lower(): v for k, v in (app_paths or {}).items()}

    def resolve(query):
        return resolve_program(
            query,
            which=lambda n: on_path.get(n.lower()),
            app_path=lambda n: app_paths.get(n.lower()),
            isfile=lambda p: p in files,
        )

    return resolve


def test_resolve_program_order():
    resolve = _resolver(
        files={r"C:\Tools\x.exe"},
        on_path={"notepad": r"C:\Windows\notepad.exe"},
        app_paths={"excel.exe": r"C:\Office\EXCEL.EXE"},
    )
    assert resolve(r'"C:\Tools\x.exe"') == r"C:\Tools\x.exe"  # existing path, quotes ok
    assert resolve("notepad") == r"C:\Windows\notepad.exe"  # PATH
    assert resolve("excel") == r"C:\Office\EXCEL.EXE"  # App Paths (keyed by .exe name)
    assert resolve("EXCEL.EXE") == r"C:\Office\EXCEL.EXE"
    assert resolve("Calculator") is None  # left to the shell / Start menu
    assert resolve(r"C:\missing\app.exe") is None  # a missing path is not searched for
    assert resolve("") is None


def test_resolve_program_tries_exe_suffix_on_path():
    resolve = _resolver(on_path={"charmap.exe": r"C:\Windows\System32\charmap.exe"})
    assert resolve("charmap") == r"C:\Windows\System32\charmap.exe"
    assert looks_like_path(r"C:\x\y.exe") and looks_like_path("./run.exe")
    assert not looks_like_path("notepad.exe")


def test_encode_png_roundtrip():
    # 2x1 image: red, blue (BGRA input)
    png = encode_png(2, 1, bytes([0, 0, 255, 255, 255, 0, 0, 255]))
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (2, 1)
    idat_len = struct.unpack(">I", png[33:37])[0]
    raw = zlib.decompress(png[41 : 41 + idat_len])
    assert raw == bytes([0, 255, 0, 0, 0, 0, 255])


def test_clean_document_text_drops_object_placeholders_and_blank_lines():
    raw = "Title\r\n￼\r\n  First paragraph.  \r\r\n￼Button\nLast"
    assert clean_document_text(raw) == "Title\nFirst paragraph.\nButton\nLast"


def test_page_tells_the_model_how_to_read_on():
    text = "abcdefghij"
    assert page(text, 0, 20) == text
    first = page(text, 0, 4)
    assert first.startswith("abcd\n") and "offset=4" in first and "6 more" in first
    middle = page(text, 4, 4)
    assert middle.splitlines()[1] == "efgh" and "characters 4-8 of 10" in middle
    assert "offset=8" in middle
    assert page(text, 8, 4).endswith("ij")
    assert "no more text" in page(text, 10, 4)


def test_parse_points_accepts_what_models_send():
    want = [(0.1, 0.2), (0.9, 0.2)]
    assert parse_points([[0.1, 0.2], [0.9, 0.2]]) == want
    assert parse_points([{"x": 0.1, "y": 0.2}, {"x": 0.9, "y": 0.2}]) == want
    assert parse_points("[[0.1, 0.2], [0.9, 0.2]]") == want
    assert parse_points("0.1,0.2 0.9,0.2") == want
    for bad in ([], [[1, 2, 3]], "0.1 0.2 0.3", 5):
        with pytest.raises(WinMcpError, match="0 = left edge"):
            parse_points(bad)


def test_to_pixels_maps_fractions_into_the_rectangle():
    rect = (100, 50, 300, 151)  # 200 x 101
    assert to_pixels(rect, [(0, 0), (1, 1), (0.5, 0.5)]) == [(100, 50), (299, 150), (200, 100)]
    assert to_pixels(rect, [(1.01, -0.01)]) == [(299, 50)]  # rounding noise is clamped
    with pytest.raises(WinMcpError, match="outside 0..1"):
        to_pixels(rect, [(50, 50)])  # percent / pixels are refused, not clamped
    with pytest.raises(WinMcpError, match="no visible area"):
        to_pixels((0, 0, 0, 10), [(0, 0)])


def test_interpolate_fills_gaps_for_freehand_strokes():
    assert interpolate([(0, 0)]) == [(0, 0)]
    path = interpolate([(0, 0), (40, 0), (40, 20)], step=10)
    assert path[0] == (0, 0) and path[-1] == (40, 20)
    assert (40, 0) in path
    gaps = [max(abs(b[0] - a[0]), abs(b[1] - a[1])) for a, b in itertools.pairwise(path)]
    assert max(gaps) <= 10 and len(path) == 7


# ---------------------------------------------------------------------------
# Server (any OS: the UI Automation backend is replaced by tests/windows_mcp_fake.py)
# ---------------------------------------------------------------------------

READ_ONLY = {"list_windows", "focus_window", "get_ui_tree", "read_text", "screenshot"}
CHANGING = {
    "launch_app",
    "close_window",
    "click_element",
    "set_text",
    "send_keys",
    "click_at",
    "drag",
}
REPO = Path(__file__).resolve().parents[1]

# Starts the real server module on top of the fake backend (used over stdio).
FAKE_SERVER = (
    "import sys; "
    f"sys.path[:0] = [{str(REPO / 'tools')!r}, {str(REPO)!r}]; "
    "from tests import windows_mcp_fake; windows_mcp_fake.install(); "
    "from windows_mcp.server import mcp; mcp.run(transport='stdio')"
)


@pytest.fixture()
def fake_server(monkeypatch):
    pytest.importorskip("mcp")
    import importlib

    import windows_mcp

    from tests import windows_mcp_fake

    monkeypatch.setitem(sys.modules, "windows_mcp.uia", windows_mcp_fake)
    monkeypatch.setattr(windows_mcp, "uia", windows_mcp_fake, raising=False)
    monkeypatch.delitem(sys.modules, "windows_mcp.server", raising=False)
    windows_mcp_fake.state.update(text="", calls=[])
    yield importlib.import_module("windows_mcp.server"), windows_mcp_fake.state
    sys.modules.pop("windows_mcp.server", None)


def _check_annotations(tools):
    assert set(tools) == READ_ONLY | CHANGING
    for name in READ_ONLY:
        a = tools[name].annotations
        assert a.readOnlyHint is True and a.destructiveHint is False, name
        assert a.title, name
    for name in CHANGING:
        a = tools[name].annotations
        assert a.readOnlyHint is False, name
        assert a.destructiveHint is (name != "launch_app"), name
        assert a.title, name


async def test_server_tool_annotations(fake_server):
    server, _ = fake_server
    _check_annotations({t.name: t for t in await server.mcp.list_tools()})


async def test_server_parameter_descriptions_reach_the_schema(fake_server):
    server, _ = fake_server
    tools = {t.name: t for t in await server.mcp.list_tools()}
    props = tools["click_element"].inputSchema["properties"]
    assert props["window"]["description"].startswith("Window handle")
    assert "get_ui_tree" in props["element"]["description"]
    assert tools["click_element"].inputSchema["required"] == ["window", "element"]


async def test_server_errors_are_readable(fake_server):
    from mcp.server.fastmcp.exceptions import ToolError

    server, state = fake_server
    with pytest.raises(ToolError, match="No visible window matches 'Paint'"):
        await server.mcp.call_tool("focus_window", {"window": "Paint"})
    # Unexpected backend exceptions become one short line, not a traceback.
    with pytest.raises(ToolError, match="RuntimeError: COM went away"):
        await server.mcp.call_tool("click_element", {"window": "Notepad", "element": "boom"})
    await server.mcp.call_tool("click_element", {"window": "Notepad", "element": "e2"})
    assert state["calls"][-1] == ("click", "e2", "auto")


async def test_server_screenshot_returns_png(fake_server):
    server, _ = fake_server
    content = await server.mcp.call_tool("screenshot", {"window": "Notepad"})
    kinds = [c.type for c in content]
    assert kinds == ["image", "text"]
    assert content[0].mimeType == "image/png"
    assert "Untitled - Notepad" in content[1].text


async def test_server_pointer_tools(fake_server):
    server, state = fake_server
    tools = {t.name: t for t in await server.mcp.list_tools()}
    assert "0 = left edge" in tools["drag"].inputSchema["properties"]["points"]["description"]
    assert tools["drag"].inputSchema["required"] == ["window", "points"]
    await server.mcp.call_tool(
        "drag", {"window": "Notepad", "points": [[0, 0.5], [1, 0.5]], "element": "e1"}
    )
    assert state["calls"][-1] == ("drag", [[0, 0.5], [1, 0.5]], "e1", "left")
    await server.mcp.call_tool("click_at", {"window": "Notepad", "x": 0.25, "y": 0.75})
    assert state["calls"][-1] == ("click_at", 0.25, 0.75, None, "click")


async def test_aar_asks_before_desktop_changes_over_stdio():
    """Real MCP stdio round trip: server annotations -> aar bridge -> policy -> approval."""
    pytest.importorskip("mcp")
    from agent.core.config import SafetyConfig, ToolConfig
    from agent.core.events import ToolCall
    from agent.extensions.mcp import MCPBridge, MCPServerConfig
    from agent.safety.permissions import ApprovalResult
    from agent.tools.execution import ToolExecutor
    from agent.tools.registry import ToolRegistry
    from agent.tools.schema import SideEffect

    asked: list[str] = []
    answers = [ApprovalResult.DENIED, ApprovalResult.APPROVED, ApprovalResult.APPROVED]

    async def approve(spec, tc):
        asked.append(f"{spec.name} {tc.arguments.get('text', '')}".strip())
        return answers.pop(0)

    cfg = MCPServerConfig(
        name="windows", command=sys.executable, args=["-c", FAKE_SERVER], prefix_tools=True
    )
    async with MCPBridge([cfg]) as bridge:
        registry = ToolRegistry()
        assert await bridge.register_all(registry) == 12
        # The server's instructions reach the system prompt as a tool guideline.
        (guideline,) = registry.get_prompt_guidelines()
        assert guideline.startswith("MCP server 'windows': These tools control")
        for name in READ_ONLY:
            assert registry.get(f"windows__{name}").side_effects == [SideEffect.EXTERNAL]
        for name in CHANGING:
            assert SideEffect.EXECUTE in registry.get(f"windows__{name}").side_effects

        # Default SafetyConfig == "review": approval for writes and execute.
        executor = ToolExecutor(registry, ToolConfig(), SafetyConfig(), approval_callback=approve)

        async def call(tool, **args):
            (res,) = await executor.execute(
                [ToolCall(tool_name=f"windows__{tool}", arguments=args)]
            )
            return res

        assert "Untitled - Notepad" in (await call("list_windows")).output
        denied = await call("set_text", window="Notepad", element="e1", text="first")
        assert denied.is_error and "denied by user" in denied.output
        assert (await call("read_text", window="Notepad")).output == "(empty)"  # never ran
        assert not (await call("set_text", window="Notepad", element="e1", text="second")).is_error
        assert (await call("read_text", window="Notepad", element="e1")).output == "second"

        missing = await call("focus_window", window="Paint")  # read-only: no prompt
        assert missing.is_error and "No visible window matches 'Paint'" in missing.output
        failed = await call("launch_app", name_or_path="excel")
        assert failed.is_error and "Could not find a program called 'excel'" in failed.output

    assert asked == [
        "windows__set_text first",
        "windows__set_text second",
        "windows__launch_app",
    ]


@pytest.mark.skipif(sys.platform != "win32", reason="the real backend needs Windows")
async def test_real_server_imports_on_windows(monkeypatch):
    pytest.importorskip("mcp")
    pytest.importorskip("uiautomation")
    monkeypatch.delitem(sys.modules, "windows_mcp.server", raising=False)
    monkeypatch.delitem(sys.modules, "windows_mcp.uia", raising=False)
    from windows_mcp.server import mcp

    _check_annotations({t.name: t for t in await mcp.list_tools()})
