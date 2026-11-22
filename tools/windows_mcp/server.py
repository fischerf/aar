"""
server.py
~~~~~~~~~
Windows desktop automation MCP server (UI Automation first, keys/mouse as fallback).

Run (Windows only):
    python -c "import sys; sys.path.insert(0, 'tools'); from windows_mcp.server import mcp; mcp.run()"

Tools that only look at the desktop are annotated ``readOnlyHint=True``;
everything that changes it is ``readOnlyHint=False`` so an MCP host (aar in
``review`` mode, Zed, Claude Desktop, ...) asks the user before running it.
"""

from __future__ import annotations

import contextlib
import functools
import sys
from collections.abc import Callable
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations
from pydantic import Field

try:
    from . import uia
    from .core import WinMcpError
except ImportError:  # executed as a plain script
    import uia  # type: ignore[no-redef]
    from core import WinMcpError  # type: ignore[no-redef]

mcp = FastMCP(
    name="windows-desktop",
    instructions=(
        "Operate Windows programs through their accessibility tree. Typical flow: "
        "list_windows (or launch_app) -> get_ui_tree(window) -> click_element / set_text "
        "with the element ids (e.g. 'e12') from the latest tree -> read_text to check the "
        "result. Prefer these over send_keys; use screenshot only if the tree is empty. "
        "Never type passwords or confirm payments, deletions or sending on the user's "
        "behalf unless they asked for exactly that."
    ),
)


def _read(title: str) -> ToolAnnotations:
    return ToolAnnotations(
        title=title, readOnlyHint=True, destructiveHint=False, openWorldHint=False
    )


def _change(title: str, *, destructive: bool = True) -> ToolAnnotations:
    return ToolAnnotations(
        title=title, readOnlyHint=False, destructiveHint=destructive, openWorldHint=False
    )


Window = Annotated[str, Field(description="Window handle from list_windows, or part of its title")]
Element = Annotated[
    str,
    Field(
        description="Element id from the latest get_ui_tree (e.g. 'e12'), or a selector like "
        "'name=Save', 'automation_id=SaveButton', 'type=Button;name=OK'"
    ),
]


def _quiet(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Keep stray prints off stdout (the MCP channel) and make errors readable."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with contextlib.redirect_stdout(sys.stderr):
            try:
                return fn(*args, **kwargs)
            except WinMcpError:
                raise
            except Exception as exc:  # COM errors are cryptic; keep them short
                raise WinMcpError(f"{type(exc).__name__}: {exc}") from exc

    return wrapper


@mcp.tool(annotations=_read("List open windows"))
@_quiet
def list_windows() -> str:
    """List visible top-level windows: handle, process, pid and title."""
    windows = uia.list_windows()
    return "\n".join(w.line() for w in windows) or "No visible windows."


@mcp.tool(annotations=_read("Bring a window to the front"))
@_quiet
def focus_window(window: Window) -> str:
    """Bring a window to the front (restores it if minimized). Changes nothing inside it."""
    w = uia.window(window)
    if uia.focus(w.handle):
        return f'"{w.title}" is now in the foreground.'
    return f'Could not bring "{w.title}" to the foreground (Windows refused).'


@mcp.tool(annotations=_change("Start a program", destructive=False))
@_quiet
def launch_app(
    name_or_path: Annotated[
        str,
        Field(
            description="Program to start: Start menu name ('Excel', 'Calculator'), "
            "exe name ('notepad', 'winword'), a full path, or a document to open"
        ),
    ],
    args: Annotated[str, Field(description="Command-line arguments, e.g. a file to open")] = "",
    wait_seconds: Annotated[
        float, Field(description="How long to wait for its window (max 60)")
    ] = 10,
) -> str:
    """Start a program (or open a document) and return its new window(s)."""
    return uia.launch(name_or_path, args, wait_seconds)


@mcp.tool(annotations=_change("Close a window"))
@_quiet
def close_window(window: Window) -> str:
    """Ask a window to close (like clicking X). Unsaved work may trigger a save prompt."""
    return uia.close(uia.window(window).handle)


@mcp.tool(annotations=_read("Look at a window's controls"))
@_quiet
def get_ui_tree(
    window: Window,
    max_depth: Annotated[int, Field(description="Maximum nesting depth to show")] = 8,
    max_nodes: Annotated[int, Field(description="Maximum number of elements to show")] = 150,
) -> str:
    """Show the window's controls as an indented list: id, type, "name", #automation_id, value, [states].

    Ids stay valid until the next get_ui_tree call for the same window.
    """
    w = uia.window(window)
    return f"{w.line()}\n" + uia.ui_tree(w.handle, max(1, max_depth), max(1, max_nodes))


@mcp.tool(annotations=_change("Click a button, menu or item"))
@_quiet
def click_element(
    window: Window,
    element: Element,
    action: Annotated[
        str,
        Field(description="auto (press/toggle/select/expand), click, double_click or right_click"),
    ] = "auto",
) -> str:
    """Activate a control: press a button, toggle a checkbox, select an item, open a menu."""
    return uia.click(uia.window(window).handle, element, action)


@mcp.tool(annotations=_change("Replace the text in a field"))
@_quiet
def set_text(
    window: Window, element: Element, text: Annotated[str, Field(description="New text")]
) -> str:
    """Replace the text of an edit field or document."""
    return uia.set_text(uia.window(window).handle, element, text)


@mcp.tool(annotations=_change("Press keys or shortcuts"))
@_quiet
def send_keys(
    window: Window,
    keys: Annotated[
        str,
        Field(
            description="Keys to press. Special keys in braces, modifiers apply to the next "
            "key: '{Ctrl}s', '{Alt}{F4}', '{Enter}', '{Tab}', '{Ctrl}{Shift}n'. Other "
            "characters are typed; write '{{}', '{}}', '{(}', '{)}' for literal braces/parens."
        ),
    ],
    element: Annotated[
        str | None, Field(description="Optional element to focus first (id or selector)")
    ] = None,
) -> str:
    """Press keys or shortcuts in a window, for things click_element/set_text cannot do."""
    return uia.send_keys(uia.window(window).handle, keys, element)


@mcp.tool(annotations=_read("Read text from a window"))
@_quiet
def read_text(
    window: Window,
    element: Annotated[
        str | None, Field(description="Element id or selector; omit to read the whole window")
    ] = None,
    max_chars: Annotated[int, Field(description="Maximum characters to return")] = 4000,
) -> str:
    """Read the text or value of a control, or all visible text of a window."""
    return uia.read_text(uia.window(window).handle, element, max(1, max_chars))


@mcp.tool(annotations=_read("Take a screenshot"), structured_output=False)
@_quiet
def screenshot(
    window: Annotated[
        str | None, Field(description="Window handle or title part; omit for the whole screen")
    ] = None,
    max_width: Annotated[
        int, Field(description="Downscale to at most this many pixels wide")
    ] = 1280,
) -> list:
    """Take a PNG screenshot of a window or the screen (for vision-capable hosts)."""
    w = uia.window(window) if window else None
    png, width, height = uia.screenshot(w.handle if w else None, max_width)
    what = f'"{w.title}"' if w else "the screen"
    return [Image(data=png, format="png"), f"Screenshot of {what} ({width}x{height})."]


if __name__ == "__main__":
    mcp.run(transport="stdio")
