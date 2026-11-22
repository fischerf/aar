"""Windows backend: Win32 (ctypes) for windows/screenshots, UI Automation for controls.

Windows only. All functions run on the thread that imported this module
(FastMCP calls sync tools on its event-loop thread), which is where
``uiautomation``/comtypes initialised COM.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import shutil
import subprocess
import time
from ctypes import wintypes

import uiautomation as auto
from uiautomation import uiautomation as _uia_impl

try:
    from .core import (
        NodeInfo,
        Snapshot,
        WindowInfo,
        WinMcpError,
        clean_text,
        encode_png,
        ensure_fresh,
        escape_for_typing,
        find_window,
        looks_like_path,
        lookup_id,
        match_selector,
        normalize_type,
        parse_element,
        pick_start_app,
        render_tree,
        resolve_program,
    )
except ImportError:  # executed as a plain script
    from core import (  # type: ignore[no-redef]
        NodeInfo,
        Snapshot,
        WindowInfo,
        WinMcpError,
        clean_text,
        encode_png,
        ensure_fresh,
        escape_for_typing,
        find_window,
        looks_like_path,
        lookup_id,
        match_selector,
        normalize_type,
        parse_element,
        pick_start_app,
        render_tree,
        resolve_program,
    )

auto.Logger.SetLogFile("")  # no @AutomationLog.txt next to the server
auto.SetGlobalSearchTimeout(1)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [_ENUM_PROC, wintypes.LPARAM]
for _fn in (
    "IsWindow",
    "IsWindowVisible",
    "IsIconic",
    "SetForegroundWindow",
    "GetWindowTextLengthW",
):
    getattr(user32, _fn).argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.StretchBlt.argtypes = (
    [wintypes.HDC] + [ctypes.c_int] * 4 + [wintypes.HDC] + [ctypes.c_int] * 4 + [wintypes.DWORD]
)
gdi32.BitBlt.argtypes = (
    [wintypes.HDC] + [ctypes.c_int] * 4 + [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
)
gdi32.GetDIBits.argtypes = [
    wintypes.HDC,
    wintypes.HBITMAP,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.UINT,
]

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x80
DWMWA_CLOAKED = 14
WM_CLOSE = 0x0010
SW_RESTORE = 9
GA_ROOT = 2
GA_ROOTOWNER = 3
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SKIP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

# Latest get_ui_tree result per window handle.
_snapshots: dict[int, Snapshot] = {}
_walker = None


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def _text(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _process_name(pid: int) -> str:
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h)


def _cloaked(hwnd: int) -> bool:
    val = ctypes.c_int(0)
    dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val)
    )
    return bool(val.value)


def list_windows() -> list[WindowInfo]:
    handles: list[int] = []

    def cb(hwnd, _lparam):
        handles.append(hwnd)
        return True

    user32.EnumWindows(_ENUM_PROC(cb), 0)
    names: dict[int, str] = {}
    result = []
    for hwnd in handles:
        if not user32.IsWindowVisible(hwnd) or not user32.GetWindowTextLengthW(hwnd):
            continue
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            continue
        if _class(hwnd) in _SKIP_CLASSES or _cloaked(hwnd):
            continue
        pid = _pid(hwnd)
        if pid == os.getpid():
            continue
        if pid not in names:
            names[pid] = _process_name(pid)
        result.append(WindowInfo(hwnd, _text(hwnd), names[pid], pid, bool(user32.IsIconic(hwnd))))
    return result


def window(query: str) -> WindowInfo:
    return find_window(list_windows(), query)


def _is_foreground(hwnd: int) -> bool:
    fg = user32.GetForegroundWindow()
    return bool(fg) and (fg == hwnd or user32.GetAncestor(fg, GA_ROOT) == hwnd)


def _attach_and_activate(hwnd: int) -> None:
    """Share input state with the foreground thread so Windows allows the switch."""
    fg = user32.GetForegroundWindow()
    ours = kernel32.GetCurrentThreadId()
    theirs = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = bool(theirs and theirs != ours and user32.AttachThreadInput(ours, theirs, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(ours, theirs, False)


def focus(hwnd: int) -> bool:
    """Restore and activate *hwnd*; return whether it really is in the foreground."""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    for attempt in range(4):
        if attempt == 1:
            # Windows only lets the process with the last input steal focus;
            # a synthetic Alt tap usually satisfies that rule.
            auto.PressKey(auto.Keys.VK_MENU, 0)
            auto.ReleaseKey(auto.Keys.VK_MENU, 0)
        elif attempt == 2:
            _attach_and_activate(hwnd)
        elif attempt == 3:
            auto.SwitchToThisWindow(hwnd)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.15)
        if _is_foreground(hwnd):
            return True
    return False


def _describe_window(hwnd: int) -> str:
    pid = _pid(hwnd)
    return f'"{_text(hwnd)}" ({_process_name(pid) or "?"}, handle {hwnd})'


def _require_foreground(hwnd: int, what: str) -> None:
    if focus(hwnd):
        return
    fg = user32.GetForegroundWindow()
    if fg and user32.GetAncestor(fg, GA_ROOTOWNER) == hwnd:
        raise WinMcpError(
            f"The window has a dialog open in front of it: {_describe_window(fg)}. "
            "Nothing was sent; look at the dialog with get_ui_tree first."
        )
    front = f" ({_describe_window(fg)} is in front)" if fg else ""
    raise WinMcpError(
        f"Could not bring the window to the foreground{front}. Nothing was sent ({what} "
        "would have reached the wrong window). Ask the user to click the window, then retry."
    )


def close(hwnd: int, wait: float = 2.0) -> str:
    title = _text(hwnd)
    _snapshots.pop(hwnd, None)
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
            return f'Closed "{title}".'
        time.sleep(0.1)
    dialogs = [w for w in list_windows() if w.pid == _pid(hwnd) and w.handle != hwnd]
    hint = "".join(f"\n{w.line()}" for w in dialogs)
    return (
        f'"{title}" is still open; it is probably asking something (e.g. to save). '
        f"Inspect it with get_ui_tree.{hint}"
    )


# ---------------------------------------------------------------------------
# Launching
# ---------------------------------------------------------------------------


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
SEE_MASK_NOCLOSEPROCESS = 0x40
SEE_MASK_FLAG_NO_UI = 0x400


def _shell_execute(target: str, args: str) -> tuple[int | None, int]:
    """ShellExecute like Win+R does (documents, URLs, ``shell:`` links).

    Returns ``(pid, 0)`` on success (pid 0 when Windows does not hand out a
    process, e.g. for Store apps) and ``(None, win32_error)`` on failure.
    """
    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_FLAG_NO_UI
    info.lpVerb = "open"
    info.lpFile = target
    info.lpParameters = args or None
    info.nShow = 1
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        return None, ctypes.get_last_error()
    pid = 0
    if info.hProcess:
        pid = kernel32.GetProcessId(info.hProcess)
        kernel32.CloseHandle(info.hProcess)
    return pid, 0


def _app_path(exe: str) -> str | None:
    """Executable registered under ``App Paths`` (how Win+R finds excel.exe)."""
    import winreg

    key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, key) as k:
                value, _ = winreg.QueryValueEx(k, None)
        except OSError:
            continue
        path = os.path.expandvars(str(value).strip().strip('"'))
        if path and os.path.isfile(path):
            return path
    return None


def _powershell() -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    path = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return path if os.path.isfile(path) else "powershell"


# PowerShell writes to a pipe in the console's OEM code page by default;
# force UTF-8 and decode the bytes ourselves so app names like "Übersetzer"
# survive (and a stray byte can never crash a reader thread).
_START_APPS_PS = (
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"
)
_start_apps_cache: tuple[float, list[tuple[str, str]]] | None = None


def _start_apps() -> list[tuple[str, str]]:
    """Start-menu apps as ``(name, app_id)``; ``[]`` if they cannot be listed."""
    global _start_apps_cache
    if _start_apps_cache and time.monotonic() - _start_apps_cache[0] < 300:
        return _start_apps_cache[1]
    try:
        out = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive", "-Command", _START_APPS_PS],
            stdin=subprocess.DEVNULL,  # never let a child read the MCP channel
            capture_output=True,
            timeout=30,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        text = out.stdout.decode("utf-8", errors="replace").strip().lstrip("\ufeff")
        data = json.loads(text) if text else []
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    data = [data] if isinstance(data, dict) else data
    apps = [
        (str(d.get("Name") or ""), str(d.get("AppID") or ""))
        for d in data
        if isinstance(d, dict) and d.get("AppID")
    ]
    _start_apps_cache = (time.monotonic(), apps)
    return apps


def _win_error(code: int) -> str:
    text = ctypes.FormatError(code).strip() if code else ""
    return text or f"error {code}"


def _not_found(name: str) -> WinMcpError:
    return WinMcpError(
        f"Could not find a program called {name!r} (searched PATH, App Paths and the "
        "Start menu). Use the name shown in the Start menu (e.g. 'Excel', 'Calculator'), "
        "the .exe name (e.g. 'winword') or a full path."
    )


def _start(name_or_path: str, args: str) -> tuple[str, int]:
    """Start *name_or_path*; return ``(label, pid)`` or raise a readable error.

    Tries, in order: the executable found on PATH / App Paths, the text as
    given (documents, folders, URLs, ``ms-settings:``/``shell:`` links), and
    the Start menu (Store apps such as the Windows 11 Notepad or Calculator).
    """
    failed: str | None = None  # first real launch failure, reported if nothing works
    target = resolve_program(
        name_or_path, which=shutil.which, app_path=_app_path, isfile=os.path.isfile
    )
    if target:
        pid, err = _shell_execute(target, args)
        if pid is not None:
            return os.path.basename(target), pid
        # e.g. a WindowsApps alias whose Store app is not installed
        failed = f"Windows could not start {target!r}: {_win_error(err)}"

    plain = name_or_path.strip().strip('"')
    pid, err = _shell_execute(plain, args)
    if pid is not None:
        return plain, pid
    if looks_like_path(plain):
        raise WinMcpError(failed or f"Cannot open {plain!r}: {_win_error(err)}")

    app = pick_start_app(_start_apps(), plain)
    if app is not None:
        pid, err = _shell_execute(f"shell:AppsFolder\\{app[1]}", args)
        if pid is not None:
            return app[0], pid
        failed = failed or f"Windows could not start {app[0]!r}: {_win_error(err)}"
    if failed:
        raise WinMcpError(failed)
    raise _not_found(name_or_path)


def launch(name_or_path: str, args: str = "", wait_seconds: float = 10.0) -> str:
    if not name_or_path.strip():
        raise WinMcpError("Say which program to start, e.g. 'notepad' or 'Excel'.")
    before = {w.handle for w in list_windows()}
    label, pid = _start(name_or_path, args)

    wait_seconds = min(max(0.0, wait_seconds), 60.0)
    deadline = time.monotonic() + wait_seconds
    new: list[WindowInfo] = []
    while True:
        new = [w for w in list_windows() if w.handle not in before]
        if new or time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    if new:
        time.sleep(0.5)  # let the title settle and late siblings (splash -> main) appear
        new = [w for w in list_windows() if w.handle not in before] or new
    else:
        return (
            f"Started {label!r} but no new window appeared within {wait_seconds:g}s. "
            "It may have reused an existing window; call list_windows."
        )
    new.sort(key=lambda w: w.pid != pid)  # the launched process first
    lines = "\n".join(w.line() for w in new)
    return f"Started {label!r}. New window(s):\n{lines}"


# ---------------------------------------------------------------------------
# UI tree and elements
# ---------------------------------------------------------------------------

# UIA calls fail with COMError (element gone, access denied) or, depending on
# the provider, ctypes/OS errors. Below, a failing read just means "this
# property/control is not available", hence the broad ``except Exception``.


def _children(control: auto.Control):
    global _walker
    if _walker is None:
        _walker = _uia_impl._AutomationClient.instance().IUIAutomation.ControlViewWalker
    try:
        elem = _walker.GetFirstChildElement(control.Element)
    except Exception:  # noqa: BLE001
        return
    while elem:
        child = auto.Control.CreateControlFromElement(elem)
        if child is not None:
            yield child
        try:
            elem = _walker.GetNextSiblingElement(elem)
        except Exception:  # noqa: BLE001
            return


def _pattern(control: auto.Control, pid: int):
    try:
        return control.GetPattern(pid)
    except Exception:  # noqa: BLE001
        return None


def describe(control: auto.Control) -> NodeInfo:
    try:
        info = NodeInfo(
            control_type=normalize_type(control.ControlTypeName),
            name=clean_text(control.Name),
            automation_id=clean_text(control.AutomationId, 60),
        )
    except Exception:  # noqa: BLE001
        return NodeInfo(control_type="Unavailable")
    states = info.states
    with contextlib.suppress(Exception):
        if not control.IsEnabled:
            states.append("disabled")
        if control.HasKeyboardFocus:
            states.append("focused")
        value = _pattern(control, auto.PatternId.ValuePattern)
        if value is not None and not control.IsPassword:
            info.value = clean_text(value.Value)
        toggle = _pattern(control, auto.PatternId.TogglePattern)
        if toggle is not None:
            states.append({0: "off", 1: "on"}.get(toggle.ToggleState, "mixed"))
        expand = _pattern(control, auto.PatternId.ExpandCollapsePattern)
        if expand is not None and expand.ExpandCollapseState in (0, 1):
            states.append("collapsed" if expand.ExpandCollapseState == 0 else "expanded")
        sel = _pattern(control, auto.PatternId.SelectionItemPattern)
        if sel is not None and sel.IsSelected:
            states.append("selected")
    return info


def _root(hwnd: int) -> auto.Control:
    if not user32.IsWindow(hwnd):
        _snapshots.pop(hwnd, None)
        raise WinMcpError("That window no longer exists. Call list_windows.")
    control = auto.ControlFromHandle(hwnd)
    if control is None:
        raise WinMcpError("UI Automation cannot access this window.")
    return control


def ui_tree(hwnd: int, max_depth: int, max_nodes: int) -> str:
    text, snap = render_tree(
        _root(hwnd), describe, _children, max_depth=max_depth, max_nodes=max_nodes
    )
    _snapshots[hwnd] = snap
    return text


def element(hwnd: int, ref: str) -> tuple[str, NodeInfo, auto.Control]:
    """Resolve an element id or selector to a live control of window *hwnd*."""
    kind, value = parse_element(ref)
    if kind == "id":
        info, control = lookup_id(_snapshots.get(hwnd), value)
        ensure_fresh(value, info, describe(control))
        return value, info, control
    _, snap = render_tree(_root(hwnd), describe, _children, max_depth=40, max_nodes=3000)
    info, control = match_selector(snap.elements.values(), value)
    return ref, info, control


def _label(info: NodeInfo) -> str:
    return f'{info.control_type} "{info.name}"' if info.name else info.control_type


def _real_click(hwnd: int, control: auto.Control, action: str) -> None:
    rect = control.BoundingRectangle
    if rect.width() <= 0 or rect.height() <= 0:
        raise WinMcpError("The element is not visible on screen, so it cannot be clicked.")
    _require_foreground(hwnd, "the click")
    x, y = rect.xcenter(), rect.ycenter()
    hit = user32.WindowFromPoint(wintypes.POINT(x, y))
    if not hit or user32.GetAncestor(hit, GA_ROOT) != hwnd:
        raise WinMcpError("Another window covers the element; the click was NOT sent.")
    if action == "double_click":
        auto.Click(x, y, 0.05)
        auto.Click(x, y)
    elif action == "right_click":
        auto.RightClick(x, y)
    else:
        auto.Click(x, y)


def click(hwnd: int, ref: str, action: str = "auto") -> str:
    _, info, control = element(hwnd, ref)
    label = _label(info)
    if action == "auto":
        if (p := _pattern(control, auto.PatternId.InvokePattern)) is not None:
            p.Invoke()
            return f"Invoked {label}."
        if (p := _pattern(control, auto.PatternId.TogglePattern)) is not None:
            p.Toggle()
            return f"Toggled {label}."
        if (p := _pattern(control, auto.PatternId.SelectionItemPattern)) is not None:
            p.Select()
            return f"Selected {label}."
        if (p := _pattern(control, auto.PatternId.ExpandCollapsePattern)) is not None:
            if p.ExpandCollapseState == 0:
                p.Expand()
                return f"Expanded {label}."
            p.Collapse()
            return f"Collapsed {label}."
        action = "click"
    if action not in ("click", "double_click", "right_click"):
        raise WinMcpError("action must be auto, click, double_click or right_click.")
    _real_click(hwnd, control, action)
    return f"{action.replace('_', ' ').capitalize()} on {label}."


def _same_text(a: str, b: str) -> bool:
    def norm(s: str) -> str:
        return s.replace("\r\n", "\n").replace("\r", "\n")

    return norm(a) == norm(b)


def _set_value(control: auto.Control, text: str) -> bool:
    """Set *text* through ValuePattern; ``False`` if unsupported or it did not stick."""
    value = _pattern(control, auto.PatternId.ValuePattern)
    if value is None:
        return False
    try:
        if value.IsReadOnly:
            return False
        value.SetValue(text)
        return _same_text(value.Value or "", text)
    except Exception:  # noqa: BLE001 - some controls expose ValuePattern but reject SetValue
        return False


def set_text(hwnd: int, ref: str, text: str) -> str:
    _, info, control = element(hwnd, ref)
    if _set_value(control, text):
        return f"Set text of {_label(info)} ({len(text)} characters)."
    # No (writable) ValuePattern, or the control ignored it: type instead.
    _require_foreground(hwnd, "the text")
    control.SetFocus()
    auto.SendKeys("{Ctrl}a{Delete}" + escape_for_typing(text), interval=0.005)
    return f"Typed {len(text)} characters into {_label(info)}."


def send_keys(hwnd: int, keys: str, ref: str | None = None) -> str:
    target = "the window"
    if ref:
        _, info, control = element(hwnd, ref)
        target = _label(info)
    _require_foreground(hwnd, "the keys")
    if ref:
        control.SetFocus()
    auto.SendKeys(keys, interval=0.02)
    return f"Sent keys to {target}."


def read_text(hwnd: int, ref: str | None, max_chars: int) -> str:
    if ref:
        _, _, control = element(hwnd, ref)
        text_pat = _pattern(control, auto.PatternId.TextPattern)
        if text_pat is not None:
            text = text_pat.DocumentRange.GetText(max_chars + 1)
        elif (value := _pattern(control, auto.PatternId.ValuePattern)) is not None:
            text = "" if control.IsPassword else value.Value
        else:
            text = control.Name
    else:
        _, snap = render_tree(_root(hwnd), describe, _children, max_depth=40, max_nodes=1000)
        seen: list[str] = []
        for node, _ in snap.elements.values():
            for part in (node.name, node.value):
                if part and part not in seen:
                    seen.append(part)
        text = "\n".join(seen)
    text = text or ""
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... (truncated at {max_chars} characters)"
    return text or "(empty)"


# ---------------------------------------------------------------------------
# Screenshots
# ---------------------------------------------------------------------------


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def screenshot(hwnd: int | None, max_width: int) -> tuple[bytes, int, int]:
    """PNG of a window (PrintWindow, works when covered) or of the primary screen."""
    if hwnd is not None:
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
    else:
        w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    if w <= 0 or h <= 0:
        raise WinMcpError("The window has no visible area (is it minimized?).")
    scale = min(1.0, max_width / w) if max_width > 0 else 1.0
    ow, oh = max(1, int(w * scale)), max(1, int(h * scale))

    screen = user32.GetDC(None)
    src, dst = gdi32.CreateCompatibleDC(screen), gdi32.CreateCompatibleDC(screen)
    src_bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
    dst_bmp = gdi32.CreateCompatibleBitmap(screen, ow, oh)
    old_src, old_dst = gdi32.SelectObject(src, src_bmp), gdi32.SelectObject(dst, dst_bmp)
    try:
        if hwnd is not None:
            user32.PrintWindow(hwnd, src, 2)  # PW_RENDERFULLCONTENT
        else:
            gdi32.BitBlt(src, 0, 0, w, h, screen, 0, 0, 0x00CC0020)  # SRCCOPY
        gdi32.SetStretchBltMode(dst, 4)  # HALFTONE
        gdi32.StretchBlt(dst, 0, 0, ow, oh, src, 0, 0, w, h, 0x00CC0020)
        header = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), ow, -oh, 1, 32, 0)
        buf = ctypes.create_string_buffer(ow * oh * 4)
        gdi32.SelectObject(dst, old_dst)
        if not gdi32.GetDIBits(dst, dst_bmp, 0, oh, buf, ctypes.byref(header), 0):
            raise WinMcpError("Capturing the screenshot failed.")
        return encode_png(ow, oh, buf.raw), ow, oh
    finally:
        gdi32.SelectObject(src, old_src)
        for obj in (src_bmp, dst_bmp):
            gdi32.DeleteObject(obj)
        gdi32.DeleteDC(src)
        gdi32.DeleteDC(dst)
        user32.ReleaseDC(None, screen)
