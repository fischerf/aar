"""Platform-independent logic for the Windows desktop MCP server.

Nothing here touches Win32 or UI Automation, so it imports (and is unit
tested) on every platform: window matching, element addressing, the compact
UI-tree renderer, typing escapes and a tiny PNG encoder.
"""

from __future__ import annotations

import itertools
import re
import struct
import unicodedata
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any


class WinMcpError(Exception):
    """An error whose message is meant for the model (what went wrong + what to do)."""


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowInfo:
    handle: int
    title: str
    process: str = ""
    pid: int = 0
    minimized: bool = False

    def line(self) -> str:
        flag = " [minimized]" if self.minimized else ""
        title = _INVISIBLE.sub("", self.title)  # what a model can copy back
        return f'{self.handle}  {self.process or "?"}  pid={self.pid}  "{title}"{flag}'


def parse_handle(text: str) -> int | None:
    """Return a window handle for ``"132456"`` / ``"0x2058C"``, else ``None``."""
    s = str(text).strip()
    try:
        if s.lower().startswith("0x"):
            return int(s, 16)
        if s.isdigit():
            return int(s)
    except ValueError:
        pass
    return None


def _process_stem(name: str) -> str:
    name = name.lower()
    return name.removesuffix(".exe")


# Invisible format characters some apps put into titles (Edge: "Microsoft​ Edge");
# a model copying the title drops them, so they must not matter for matching.
_INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
_DASHES = re.compile("[\u2010-\u2015\u2212]")


def title_key(title: str) -> str:
    """Comparable form of a window title: no invisible characters, one kind of dash,
    single spaces, case-folded."""
    s = unicodedata.normalize("NFKC", _INVISIBLE.sub("", title))
    return " ".join(_DASHES.sub("-", s).split()).casefold()


def find_window(windows: list[WindowInfo], query: str) -> WindowInfo:
    """Resolve *query* (handle, title substring or process name) to one window.

    Order: handle, exact title, title substring, process name. Raises
    :class:`WinMcpError` with the candidates when nothing or several match.
    """
    handle = parse_handle(query)
    if handle is not None:
        for w in windows:
            if w.handle == handle:
                return w
        raise WinMcpError(f"No visible window with handle {handle}. Call list_windows.")

    q = title_key(str(query))
    if not q:
        raise WinMcpError("Window must be a handle or part of a window title.")
    stages = (
        [w for w in windows if title_key(w.title) == q],
        [w for w in windows if q in title_key(w.title)],
        [w for w in windows if w.process and _process_stem(w.process) == _process_stem(q)],
    )
    for matches in stages:
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            listing = "\n".join(w.line() for w in matches[:10])
            raise WinMcpError(
                f"{len(matches)} windows match {query!r}; pass the handle instead:\n{listing}"
            )
    raise WinMcpError(f"No visible window matches {query!r}. Call list_windows.")


# ---------------------------------------------------------------------------
# UI tree
# ---------------------------------------------------------------------------

# Containers that add nesting but no meaning: hidden from the tree (their
# children move up) unless they carry a name, automation id or value.
STRUCTURAL_TYPES = frozenset({"Pane", "Group", "Custom", "Window"})

_PRIVATE_USE = re.compile("[-]")  # icon-font glyphs (Segoe Fluent Icons)
_WS = re.compile(r"\s+")


def clean_text(text: Any, limit: int = 80) -> str:
    """Single-line, glyph-free, truncated version of a UI string."""
    if text is None:
        return ""
    s = _WS.sub(" ", _PRIVATE_USE.sub("", str(text))).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


@dataclass
class NodeInfo:
    """What the renderer needs to know about one UI element."""

    control_type: str  # short form: "Button", "Edit", ...
    name: str = ""
    automation_id: str = ""
    value: str | None = None
    states: list[str] = field(default_factory=list)

    def fingerprint(self) -> tuple[str, str, str]:
        return (self.control_type, self.name, self.automation_id)


def normalize_type(name: str) -> str:
    s = str(name).strip()
    return s[:-7] if s.endswith("Control") and len(s) > 7 else s


def _is_noise(info: NodeInfo, parent: NodeInfo | None) -> bool:
    if info.control_type in ("Text", "Image") and not info.name and not info.value:
        # Empty labels and icons tell a model nothing (Paint: one per toolbar button).
        return info.control_type == "Text" or not info.automation_id
    # A label inside a button/menu item that just repeats the parent's name.
    return info.control_type == "Text" and parent is not None and info.name == parent.name


def _is_structural(info: NodeInfo) -> bool:
    return (
        info.control_type in STRUCTURAL_TYPES
        and not info.name
        and not info.automation_id
        and info.value is None
    )


def format_node(eid: str, info: NodeInfo) -> str:
    parts = [eid, info.control_type]
    if info.name:
        parts.append(f'"{info.name}"')
    if info.automation_id and info.automation_id != info.name:
        parts.append(f"#{info.automation_id}")
    if info.value is not None and info.value != info.name:
        parts.append(f'value="{info.value}"')
    if info.states:
        parts.append(f"[{','.join(info.states)}]")
    return " ".join(parts)


@dataclass
class Snapshot:
    """Element ids of one ``get_ui_tree`` call: ``"e3" -> (NodeInfo, backend ref)``."""

    elements: dict[str, tuple[NodeInfo, Any]] = field(default_factory=dict)


def render_tree(
    root: Any,
    describe: Callable[[Any], NodeInfo],
    children: Callable[[Any], Iterable[Any]],
    *,
    max_depth: int = 8,
    max_nodes: int = 150,
    max_visits: int | None = None,
) -> tuple[str, Snapshot]:
    """Walk the tree lazily and return (compact text, snapshot).

    *describe* and *children* adapt the backend (UIA controls in production,
    plain dicts in tests). Structural wrappers are flattened and do not count
    toward *max_depth*; redundant text labels are dropped. *max_visits* bounds
    the raw walk so a huge grid cannot stall the server.
    """
    max_visits = max_visits or max_nodes * 6
    snap = Snapshot()
    lines: list[str] = []
    visits = 0
    truncated = False

    def walk(ref: Any, depth: int, parent: NodeInfo | None) -> None:
        nonlocal visits, truncated
        for child in children(ref):
            if len(snap.elements) >= max_nodes or visits >= max_visits:
                truncated = True
                return
            visits += 1
            info = describe(child)
            if _is_noise(info, parent):
                continue
            if _is_structural(info):
                walk(child, depth, parent)
                continue
            eid = f"e{len(snap.elements) + 1}"
            snap.elements[eid] = (info, child)
            line = "  " * depth + format_node(eid, info)
            if depth + 1 >= max_depth:
                hidden = sum(1 for _ in children(child))
                if hidden:
                    line += f" (+{hidden} children, raise max_depth)"
                lines.append(line)
                continue
            lines.append(line)
            walk(child, depth + 1, info)

    root_info = describe(root)
    header = f'{root_info.control_type} "{root_info.name}"'
    walk(root, 0, root_info)
    if truncated:
        lines.append(
            f"... truncated after {len(snap.elements)} elements "
            "(raise max_nodes, or use a selector such as name=...)"
        )
    if not snap.elements:
        lines.append("(no accessible controls found; try screenshot or send_keys)")
    return header + "\n" + "\n".join(lines), snap


# ---------------------------------------------------------------------------
# Element addressing
# ---------------------------------------------------------------------------

_ID_RE = re.compile(r"^\s*e?(\d+)\s*$", re.IGNORECASE)
_SELECTOR_KEYS = {
    "name": "name",
    "automation_id": "automation_id",
    "automationid": "automation_id",
    "id": "automation_id",
    "type": "control_type",
    "control_type": "control_type",
}


def parse_element(element: str) -> tuple[str, Any]:
    """``"e12"``/``"12"`` -> ``("id", "e12")``; otherwise ``("selector", {...})``.

    Selectors are ``key=value`` pairs joined by ``;`` with keys ``name``,
    ``automation_id`` and ``type``. A bare string is a name.
    """
    m = _ID_RE.match(str(element))
    if m:
        return "id", f"e{int(m.group(1))}"
    text = str(element).strip()
    if not text:
        raise WinMcpError("Element is empty. Pass an id from get_ui_tree (e.g. 'e12').")
    selector: dict[str, str] = {}
    parts = [p for p in text.split(";") if p.strip()]
    if all("=" in p and p.split("=", 1)[0].strip().lower() in _SELECTOR_KEYS for p in parts):
        for p in parts:
            key, value = p.split("=", 1)
            selector[_SELECTOR_KEYS[key.strip().lower()]] = value.strip()
    else:
        selector["name"] = text
    if "control_type" in selector:
        selector["control_type"] = normalize_type(selector["control_type"])
    return "selector", selector


def lookup_id(snapshot: Snapshot | None, eid: str) -> tuple[NodeInfo, Any]:
    if snapshot is None:
        raise WinMcpError(
            "No element ids for this window yet. Call get_ui_tree for it first, "
            "or address the element by name (e.g. 'name=Save')."
        )
    try:
        return snapshot.elements[eid]
    except KeyError:
        raise WinMcpError(
            f"Unknown element id {eid!r} (the latest tree has e1..e{len(snapshot.elements)}). "
            "Call get_ui_tree again."
        ) from None


def match_selector(
    candidates: Iterable[tuple[NodeInfo, Any]], selector: dict[str, str]
) -> tuple[NodeInfo, Any]:
    """Pick the single element matching *selector* (exact name before substring)."""
    pool = list(candidates)
    ctype = selector.get("control_type", "").lower()
    aid = selector.get("automation_id", "").lower()
    if ctype:
        pool = [c for c in pool if c[0].control_type.lower() == ctype]
    if aid:
        pool = [c for c in pool if c[0].automation_id.lower() == aid]
    name = selector.get("name", "").lower()
    if name:
        exact = [c for c in pool if c[0].name.lower() == name]
        pool = exact or [c for c in pool if name in c[0].name.lower()]
    if len(pool) == 1:
        return pool[0]
    desc = "; ".join(f"{k}={v}" for k, v in selector.items())
    if not pool:
        raise WinMcpError(f"No element matches {desc!r}. Call get_ui_tree to see the controls.")
    listing = "\n".join(format_node("-", info) for info, _ in pool[:8])
    raise WinMcpError(
        f"{len(pool)} elements match {desc!r}; add type=... or call get_ui_tree and use an id:\n"
        f"{listing}"
    )


def ensure_fresh(eid: str, expected: NodeInfo, current: NodeInfo | None) -> None:
    """Raise when the element behind *eid* vanished or turned into something else."""
    if current is None or current.fingerprint() != expected.fingerprint():
        raise WinMcpError(
            f"Element {eid} is no longer available (the window changed). Call get_ui_tree again."
        )


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def escape_for_typing(text: str) -> str:
    """Escape literal text for ``uiautomation.SendKeys`` (braces, parens, newlines)."""
    out = []
    for ch in text.replace("\r\n", "\n"):
        if ch in "{}()":
            out.append("{" + ch + "}")
        elif ch == "\n":
            out.append("{Enter}")
        elif ch == "\t":
            out.append("{Tab}")
        else:
            out.append(ch)
    return "".join(out)


def _strip_quotes(text: str) -> str:
    return str(text).strip().strip('"').strip()


def resolve_program(
    query: str,
    *,
    which: Callable[[str], str | None],
    app_path: Callable[[str], str | None],
    isfile: Callable[[str], bool],
) -> str | None:
    """Turn ``'notepad'`` / ``'excel.exe'`` / a full path into an executable path.

    Looks at the path itself, then ``PATH`` (*which*), then the registry's
    App Paths (*app_path*, keyed by ``name.exe``). Returns ``None`` when
    nothing matches; the caller then tries the shell and the Start menu.
    """
    q = _strip_quotes(query)
    if not q:
        return None
    if isfile(q):
        return q
    if looks_like_path(q):
        return None  # a path that does not exist; no point searching
    exe = q if q.lower().endswith(".exe") else q + ".exe"
    return which(q) or which(exe) or app_path(exe)


def looks_like_path(query: str) -> bool:
    q = _strip_quotes(query)
    return "\\" in q or "/" in q


def pick_start_app(apps: list[tuple[str, str]], query: str) -> tuple[str, str] | None:
    """Choose a Start-menu entry ``(name, app_id)`` for *query*.

    Matches the display name, ignoring case and a trailing ``.exe``: exact
    name first, then a unique substring. Returns ``None`` when nothing
    matches and raises :class:`WinMcpError` listing the candidates when
    several do.
    """
    q = _process_stem(_strip_quotes(query))
    if not q:
        return None
    exact = [a for a in apps if a[0].strip().lower() == q]
    if exact:
        return exact[0]
    partial = [a for a in apps if q in a[0].lower()]
    if len(partial) == 1:
        return partial[0]
    if partial:
        names = ", ".join(sorted({a[0] for a in partial})[:10])
        raise WinMcpError(f"Several programs match {query!r}: {names}. Use the full name.")
    return None


# ---------------------------------------------------------------------------
# Reading text (read_text)
# ---------------------------------------------------------------------------


def clean_document_text(raw: str) -> str:
    """Tidy a document's TextPattern text: one line per paragraph, no empty lines.

    Browsers put U+FFFC (object replacement) where images, buttons and form
    fields sit; on a typical page these are a third of all lines.
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n").replace("￼", "")
    return "\n".join(line.strip() for line in text.split("\n") if line.strip())


def page(text: str, offset: int, max_chars: int) -> str:
    """Return ``max_chars`` characters from *offset* with a hint how to read on."""
    total = len(text)
    offset = max(0, offset)
    if offset >= total and total:
        return f"(no more text: it has {total} characters)"
    chunk = text[offset : offset + max_chars]
    if offset:
        chunk = f"... (characters {offset}-{offset + len(chunk)} of {total})\n" + chunk
    if offset + max_chars < total:
        chunk += (
            f"\n... (truncated: {total - offset - max_chars} more characters; "
            f"call read_text again with offset={offset + max_chars})"
        )
    return chunk


# ---------------------------------------------------------------------------
# Pointer positions (click_at / drag)
# ---------------------------------------------------------------------------

POINT_HELP = (
    "Positions are fractions of the element (or window): x 0 = left edge, 1 = right edge; "
    "y 0 = top, 1 = bottom; e.g. [0.5, 0.5] is the centre."
)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def parse_points(points: Any) -> list[tuple[float, float]]:
    """Accept ``[[x, y], ...]``, ``[{"x":..,"y":..}, ...]`` or a string of numbers.

    Small models send all of these; anything else raises :class:`WinMcpError`.
    """
    pairs: list[tuple[float, float]] = []
    if isinstance(points, str):
        nums = [float(n) for n in _NUMBER.findall(points)]
        if len(nums) % 2:
            raise WinMcpError(f"Points need x and y for each position. {POINT_HELP}")
        pairs = list(zip(nums[0::2], nums[1::2], strict=True))
    elif isinstance(points, (list, tuple)):
        for p in points:
            if isinstance(p, dict) and "x" in p and "y" in p:
                pairs.append((float(p["x"]), float(p["y"])))
            elif isinstance(p, (list, tuple)) and len(p) == 2:
                pairs.append((float(p[0]), float(p[1])))
            else:
                raise WinMcpError(f"Each point must be [x, y], got {p!r}. {POINT_HELP}")
    else:
        raise WinMcpError(f"Points must be a list like [[0.1, 0.2], [0.8, 0.2]]. {POINT_HELP}")
    if not pairs:
        raise WinMcpError(f"No points given. {POINT_HELP}")
    return pairs


def to_pixels(
    rect: tuple[int, int, int, int], points: list[tuple[float, float]]
) -> list[tuple[int, int]]:
    """Map fractional *points* into the screen rectangle ``(left, top, right, bottom)``.

    0 and 1 land on the first and last pixel inside the rectangle. Values a
    little outside 0..1 (rounding) are clamped; clearly wrong ones are refused,
    including percentages or pixel coordinates.
    """
    left, top, right, bottom = rect
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        raise WinMcpError("The element has no visible area, so it cannot be pointed at.")
    out = []
    for x, y in points:
        if not (-0.02 <= x <= 1.02 and -0.02 <= y <= 1.02):
            raise WinMcpError(f"Position [{x:g}, {y:g}] is outside 0..1. {POINT_HELP}")
        x, y = min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)
        out.append((left + round(x * (w - 1)), top + round(y * (h - 1))))
    return out


def interpolate(path: list[tuple[int, int]], step: int = 8) -> list[tuple[int, int]]:
    """Insert points so consecutive positions are at most *step* pixels apart.

    Drawing programs only see the mouse moves they receive; without these a
    freehand stroke would become straight segments or vanish.
    """
    if len(path) < 2:
        return list(path)
    out = [path[0]]
    for (x0, y0), (x1, y1) in itertools.pairwise(path):
        n = max(1, int(max(abs(x1 - x0), abs(y1 - y0)) // max(1, step)))
        for i in range(1, n + 1):
            out.append((round(x0 + (x1 - x0) * i / n), round(y0 + (y1 - y0) * i / n)))
    return out


def encode_png(width: int, height: int, bgra: bytes) -> bytes:
    """Encode top-down 32-bit BGRA pixels as an RGB PNG (stdlib only)."""
    n = width * height
    rgb = bytearray(n * 3)
    rgb[0::3] = bgra[2::4]
    rgb[1::3] = bgra[1::4]
    rgb[2::3] = bgra[0::4]
    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride : (y + 1) * stride] for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )
