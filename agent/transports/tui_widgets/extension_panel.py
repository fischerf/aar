"""ExtensionPanel — generic tree + action strip for an extension :class:`UIPanel`.

The widget knows nothing about any particular extension.  It renders the
``UINode`` tree the panel's ``snapshot()`` returns, shows the actions that
apply to the highlighted node in a hint strip, and dispatches key presses to
the matching :class:`~agent.extensions.api.UIAction` — confirming first when
the action is destructive.

Key handling is deliberately local: action keys (``u``, ``b``, ``D`` …) only
fire while the panel has focus, so they can never collide with the input
widget.  ``escape`` asks the app to hide the panel (``ExtensionPanel.Close``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from rich.text import Text

try:
    from textual import events, work
    from textual.app import ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical
    from textual.message import Message
    from textual.screen import ModalScreen
    from textual.widgets import Input, Static, Tree
    from textual.widgets.tree import TreeNode
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "The fixed TUI requires the 'textual' package. "
        'Install it with: pip install "aar-agent[tui-fixed]"'
    ) from exc

from agent.extensions.api import (
    UIAction,
    UIInvocation,
    UINode,
    UIPanel,
    run_ui_action,
    run_ui_snapshot,
)

logger = logging.getLogger(__name__)

# ``UINode.style`` hints → Rich styles.  Kept tiny on purpose; a theme can
# override via ``ExtensionPanel(style_map=...)``.
_DEFAULT_STYLE_MAP: dict[str, str] = {
    "active": "bold",
    "dim": "dim",
    "warn": "bold yellow",
    "": "",
}

BUSY_HINT = "⏳ agent running — cancel first (ctrl+x)"


class ConfirmModal(ModalScreen[dict[str, Any] | None]):
    """Confirmation dialog for destructive panel actions.

    Dismisses with an ``args`` dict (``{"force": bool, "message": str}``) on
    confirm, or ``None`` on cancel.  ``[y]`` confirms, ``[n]``/``esc`` cancel,
    ``[f]`` toggles the optional force flag.  When a message input is shown,
    ``Enter`` inside it confirms.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False, priority=True)]

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }
    ConfirmModal > Vertical {
        width: 64;
        height: auto;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    ConfirmModal .title {
        text-style: bold;
        margin-bottom: 1;
    }
    ConfirmModal .message {
        margin-bottom: 1;
    }
    ConfirmModal .force {
        margin-bottom: 1;
    }
    ConfirmModal Input {
        margin-bottom: 1;
    }
    ConfirmModal .buttons {
        text-align: center;
    }
    """

    def __init__(
        self,
        message: str,
        *,
        title: str = "Confirm",
        force_option: bool = False,
        force_hint: str = "also discard uncommitted changes",
        message_input: bool = False,
        message_placeholder: str = "commit message (optional)",
        confirm_label: str = "Confirm",
    ) -> None:
        super().__init__()
        self._message = message
        self._title = title
        self._force_option = force_option
        self._force_hint = force_hint
        self._force = False
        self._message_input = message_input
        self._placeholder = message_placeholder
        self._confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._title, classes="title")
            yield Static(self._message, classes="message")
            if self._force_option:
                yield Static(self._force_line(), classes="force", id="confirm-force")
            if self._message_input:
                yield Input(placeholder=self._placeholder, id="confirm-message")
            yield Static(
                f"[y] {self._confirm_label}        [n] Cancel",
                classes="buttons",
            )

    def on_mount(self) -> None:
        if self._message_input:
            self.query_one("#confirm-message", Input).focus()

    def _force_line(self) -> str:
        box = "[x]" if self._force else "[ ]"
        return f"{box} f  {self._force_hint}"

    def _result(self) -> dict[str, Any]:
        args: dict[str, Any] = {}
        if self._force_option:
            args["force"] = self._force
        if self._message_input:
            args["message"] = self.query_one("#confirm-message", Input).value.strip()
        return args

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self.dismiss(self._result())

    def on_key(self, event: events.Key) -> None:
        # Let the message input own printable keys while it has focus.
        if isinstance(self.focused, Input):
            return
        if event.key == "y":
            event.stop()
            self.dismiss(self._result())
        elif event.key == "n":
            event.stop()
            self.dismiss(None)
        elif event.key == "f" and self._force_option:
            event.stop()
            self._force = not self._force
            self.query_one("#confirm-force", Static).update(self._force_line())


class ExtensionPanel(Vertical):
    """Tree + hint strip for one extension :class:`UIPanel`.

    Collaborators are injected as callables so the widget stays independent of
    :class:`AarFixedApp`:

    * ``ctx_getter()`` — the current ``ExtensionContext`` (the app calls
      ``ExtensionManager.update_session`` first, exactly like slash commands).
    * ``write_system(text)`` — async; prints a line into the chat body.
    * ``is_busy()`` — ``True`` while the agent worker is running.
    """

    DEFAULT_CSS = """
    ExtensionPanel {
        width: 100%;
        height: 1fr;
        border-top: solid #2a2a2a;
    }
    ExtensionPanel > Static.title {
        height: 1;
        padding: 0 1;
        text-style: bold;
    }
    ExtensionPanel > Tree {
        height: 1fr;
    }
    ExtensionPanel > Static.hints {
        height: 2;
        padding: 0 1;
        color: #777777;
    }
    """

    class Close(Message):
        """Posted when the user presses ``escape`` inside the panel."""

        def __init__(self, panel_widget: ExtensionPanel) -> None:
            super().__init__()
            self.panel_widget = panel_widget

    class Mutated(Message):
        """Posted after an action with ``mutates=True`` ran (successfully or not).

        The app uses it to re-render the transcript — the extension may have
        rewritten ``Session.events`` (e.g. shadow-branching's ``/undo``).
        """

        def __init__(self, panel_name: str, action_id: str) -> None:
            super().__init__()
            self.panel_name = panel_name
            self.action_id = action_id

    def __init__(
        self,
        panel: UIPanel,
        *,
        ctx_getter: Callable[[], Any],
        write_system: Callable[[str], Any],
        is_busy: Callable[[], bool] | None = None,
        style_map: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._panel = panel
        self._ctx_getter = ctx_getter
        self._write_system = write_system
        self._is_busy = is_busy or (lambda: False)
        self._style_map = {**_DEFAULT_STYLE_MAP, **(style_map or {})}
        self._tree: Tree[UINode] = Tree(panel.title, id=f"panel-tree-{panel.name}")
        self._tree.show_root = False
        self._tree.guide_depth = 3
        self._hints = Static("", classes="hints")
        self._root: UINode | None = None
        self._last_error: str = ""

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    @property
    def panel(self) -> UIPanel:
        return self._panel

    @property
    def root(self) -> UINode | None:
        """The most recent snapshot root (``None`` before the first refresh)."""
        return self._root

    def compose(self) -> ComposeResult:
        yield Static(self._panel.title, classes="title")
        yield self._tree
        yield self._hints

    def focus_tree(self) -> None:
        self._tree.focus()

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    async def refresh_tree(self) -> None:
        """Re-run the panel's snapshot and rebuild the tree in place.

        Expansion state and the cursor are keyed on ``UINode.id`` so a refresh
        while the user is navigating doesn't jump.
        """
        try:
            root = await run_ui_snapshot(self._panel, self._ctx_getter())
            self._last_error = ""
        except Exception as exc:
            logger.warning("panel %r snapshot failed: %s", self._panel.name, exc)
            self._last_error = str(exc)
            root = UINode("root", f"✗ {exc}", "info", style="warn")
        self._rebuild(root)
        self._panel.changed.clear()
        self._update_hints()

    def _rebuild(self, root: UINode) -> None:
        expanded, cursor_id = self._collect_state()
        self._root = root
        self._tree.clear()
        self._tree.root.data = root
        # The root itself is hidden (show_root=False); its children are the
        # first visible level.
        for child in root.children:
            self._add_node(self._tree.root, child, expanded)
        self._tree.root.expand()
        target = self._find_tree_node(self._tree.root, cursor_id) if cursor_id else None
        if target is None and self._tree.root.children:
            target = self._tree.root.children[0]
        if target is not None:
            # The tree computes node lines lazily on its next refresh; moving
            # the cursor before that would land on line -1 (clamped to the
            # first row), so defer the move until the lines exist.
            self._tree.call_after_refresh(self._tree.move_cursor, target)

    def _add_node(self, parent: TreeNode[UINode], node: UINode, expanded: dict[str, bool]) -> None:
        label = Text(node.label, style=self._style_map.get(node.style, ""))
        if node.children:
            tn = parent.add(label, data=node, expand=expanded.get(node.id, node.expanded))
            for child in node.children:
                self._add_node(tn, child, expanded)
        else:
            parent.add_leaf(label, data=node)

    def _collect_state(self) -> tuple[dict[str, bool], str | None]:
        expanded: dict[str, bool] = {}

        def walk(tn: TreeNode[UINode]) -> None:
            for child in tn.children:
                if child.data is not None and child.children:
                    expanded[child.data.id] = child.is_expanded
                walk(child)

        walk(self._tree.root)
        cursor = self._tree.cursor_node
        cursor_id = cursor.data.id if cursor is not None and cursor.data is not None else None
        return expanded, cursor_id

    def _find_tree_node(self, tn: TreeNode[UINode], node_id: str) -> TreeNode[UINode] | None:
        for child in tn.children:
            if child.data is not None and child.data.id == node_id:
                return child
            hit = self._find_tree_node(child, node_id)
            if hit is not None:
                return hit
        return None

    def selected_node(self) -> UINode | None:
        cursor = self._tree.cursor_node
        return cursor.data if cursor is not None else None

    # ------------------------------------------------------------------
    # Hints
    # ------------------------------------------------------------------

    def _update_hints(self) -> None:
        node = self.selected_node()
        if self._is_busy():
            movable = [a for a in self._panel.actions_for(node) if not a.mutates]
            parts = [BUSY_HINT]
            if movable:
                parts.append("  ".join(f"[{a.key}] {a.label}" for a in movable))
            self._hints.update("\n".join(parts))
            return
        actions = self._panel.actions_for(node)
        if not actions:
            self._hints.update("↑↓ move · space fold · esc close")
            return
        self._hints.update("  ".join(f"[{a.key}] {a.label}" for a in actions))

    def on_tree_node_highlighted(self, _event: Tree.NodeHighlighted) -> None:
        self._update_hints()

    def on_tree_node_selected(self, _event: Tree.NodeSelected) -> None:
        self._update_hints()

    def on_focus(self, _event: events.Focus) -> None:
        self._update_hints()

    # ------------------------------------------------------------------
    # Keys
    # ------------------------------------------------------------------

    async def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            self.post_message(self.Close(self))
            return
        node = self.selected_node()
        for action in self._panel.actions_for(node):
            if event.key == action.key or event.character == action.key:
                event.stop()
                self.invoke(action, node)
                return

    # ------------------------------------------------------------------
    # Invocation
    # ------------------------------------------------------------------

    @work(exclusive=True, group="extension-panel-action")
    async def invoke(
        self, action: UIAction, node: UINode | None, args: dict[str, Any] | None = None
    ) -> None:
        """Run *action* on *node*: confirm if destructive, dispatch, refresh.

        Runs in a Textual worker so ``push_screen_wait`` is available and the
        handler (via ``run_ui_action``) never blocks rendering.
        """
        if node is None:
            return
        if action.mutates and self._is_busy():
            await self._write_system(BUSY_HINT)
            return

        collected: dict[str, Any] = dict(args or {})
        if action.destructive or action.inputs:
            modal = ConfirmModal(
                action.confirm.format(label=node.label) if action.confirm else action.label,
                title=f"{self._panel.title} · {action.label}",
                force_option="force" in action.inputs,
                message_input="message" in action.inputs,
                confirm_label=action.label,
            )
            result = await self.app.push_screen_wait(modal)
            if result is None:
                return
            collected.update(result)

        try:
            msg = await run_ui_action(
                action, UIInvocation(node=node, ctx=self._ctx_getter(), args=collected)
            )
        except Exception as exc:
            logger.warning("panel %r action %r failed: %s", self._panel.name, action.id, exc)
            msg = f"✗ {action.id}: {exc}"
        if msg:
            await self._write_system(msg)
        await self.refresh_tree()
        if action.mutates:
            self.post_message(self.Mutated(self._panel.name, action.id))
