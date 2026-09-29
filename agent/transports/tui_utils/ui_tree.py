"""Rich rendering of extension :class:`~agent.extensions.api.UINode` trees.

Shared by the inline TUI (``/panel`` prints a boxed tree) and the fixed TUI
(``ExtensionPanel`` builds its tree labels with :func:`node_text`).  No
Textual import here.
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text
from rich.tree import Tree

from agent.extensions.api import UINode

# ``UINode.style`` role hints → Rich styles.
DEFAULT_STYLE_MAP: dict[str, str] = {
    "active": "bold cyan",
    "dim": "dim",
    "warn": "bold yellow",
    "ok": "green",
    "": "",
}


def node_text(
    node: UINode,
    style_map: dict[str, str] | None = None,
    *,
    with_detail: bool = True,
    detail_style: str = "dim",
) -> Text:
    """``label`` in its role style, followed by ``detail`` (dimmed) if wanted."""
    styles = style_map or DEFAULT_STYLE_MAP
    # Append rather than Text(label, style=…): a base style would leak into
    # the detail span.
    text = Text()
    text.append(node.label, style=styles.get(node.style, ""))
    if with_detail and node.detail:
        text.append("  ")
        text.append(node.detail, style=detail_style)
    return text


def _add(
    parent: Tree,
    node: UINode,
    styles: dict[str, str],
    max_children: int,
    with_detail: bool,
) -> None:
    branch = parent.add(node_text(node, styles, with_detail=with_detail))
    if not node.children:
        return
    if not node.expanded:
        branch.label.append(f"  ▸ {len(node.children)} hidden", style="dim")  # type: ignore[union-attr]
        return
    shown = node.children[:max_children] if max_children > 0 else node.children
    for child in shown:
        _add(branch, child, styles, max_children, with_detail)
    rest = len(node.children) - len(shown)
    if rest > 0:
        branch.add(Text(f"… {rest} more", style="dim"))


def render_ui_tree(
    root: UINode,
    *,
    title: str = "",
    status: str = "",
    style_map: dict[str, str] | None = None,
    max_children: int = 12,
    with_detail: bool = True,
    border_style: str = "dim",
    footer: str = "",
) -> RenderableType:
    """Render *root* as a boxed Rich tree.

    The root's own label heads the tree; collapsed nodes (``expanded=False``)
    are summarised as ``▸ N hidden``; more than *max_children* children are
    cut with ``… N more`` (``0`` = no limit).  *status* goes into the panel
    subtitle, *footer* (e.g. a key legend) under the tree.
    """
    styles = {**DEFAULT_STYLE_MAP, **(style_map or {})}
    tree = Tree(node_text(root, styles, with_detail=with_detail), guide_style="dim")
    for child in root.children:
        _add(tree, child, styles, max_children, with_detail)
    body: RenderableType = tree
    if footer:
        body = Group(tree, Text(""), Text(footer, style="dim"))
    return Panel(
        body,
        # Text, not str: extension-supplied strings must not be parsed as markup.
        title=Text(title, style="bold") if title else None,
        title_align="left",
        subtitle=Text(status) if status else None,
        subtitle_align="right",
        border_style=border_style,
        expand=False,
        padding=(0, 1),
    )
