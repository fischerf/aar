"""Fake ``windows_mcp.uia`` backend so the MCP server runs on any OS (tests only).

``install()`` puts it in ``sys.modules`` before ``windows_mcp.server`` is
imported. The fake keeps one Notepad-like window with a single text field.
"""

from __future__ import annotations

import sys
import types

from windows_mcp.core import WindowInfo, WinMcpError, encode_png, find_window

WINDOWS = [WindowInfo(100, "Untitled - Notepad", "Notepad.exe", 7)]
state: dict = {"text": "", "calls": []}


def list_windows():
    return list(WINDOWS)


def window(query):
    return find_window(WINDOWS, query)


def focus(handle):
    state["calls"].append(("focus", handle))
    return True


def launch(name_or_path, args, wait_seconds):
    state["calls"].append(("launch", name_or_path))
    if name_or_path.lower() not in ("notepad", "notepad.exe"):
        raise WinMcpError(f"Could not find a program called {name_or_path!r}.")
    return f"Started 'notepad.exe'. New window(s):\n{WINDOWS[0].line()}"


def close(handle):
    state["calls"].append(("close", handle))
    return 'Closed "Untitled - Notepad".'


def ui_tree(handle, max_depth, max_nodes):
    return 'Window "Untitled - Notepad"\ne1 Document "Text editor"\ne2 Button "Save"'


def click(handle, element, action):
    if element == "boom":
        raise RuntimeError("COM went away")  # an unexpected backend failure
    state["calls"].append(("click", element, action))
    return f"Invoked {element}."


def set_text(handle, element, text, press_enter=False):
    state["calls"].append(("set_text", element))
    state["text"] = text
    return f"Set text of {element} ({len(text)} characters)."


def send_keys(handle, keys, element=None):
    state["calls"].append(("send_keys", keys, element))
    return "Sent keys to the window."


def click_at(handle, x, y, element, action):
    state["calls"].append(("click_at", x, y, element, action))
    return f"Click at [{x:g}, {y:g}] of the window."


def drag(handle, points, element, button):
    state["calls"].append(("drag", points, element, button))
    return f"Dragged through {len(points)} point(s)."


def read_text(handle, element, max_chars, offset=0):
    return state["text"][offset : offset + max_chars] or "(empty)"


def screenshot(handle, max_width):
    return encode_png(1, 1, b"\x00\x00\xff\xff"), 1, 1


def install() -> types.ModuleType:
    """Register this module as ``windows_mcp.uia`` and return it."""
    import windows_mcp

    mod = sys.modules[__name__]
    sys.modules["windows_mcp.uia"] = mod
    windows_mcp.uia = mod
    return mod
