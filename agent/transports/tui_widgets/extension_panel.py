"""ExtensionPanel — generic tree + action strip for an extension :class:`UIPanel`.

The widget knows nothing about any particular extension.  It renders the
``UINode`` tree the panel's ``snapshot()`` returns, shows the actions that
apply to the highlighted node, and dispatches key presses (or, in the zoomed
window, button clicks) to the matching :class:`~agent.extensions.api.UIAction`
— confirming first when the action is destructive.

Two modes:

* ``"sidebar"`` — the narrow, always-on column left of the chat body.  Labels
  only, a one-line status in the title, ``z`` (or a click on the title) asks
  the app to zoom (``ExtensionPanel.Zoom``).
* ``"window"`` — hosted by :class:`PanelWindow`, a near-full-screen modal:
  labels plus ``UINode.detail``, a detail pane fed by
  :func:`~agent.extensions.api.run_ui_describe` for the highlighted node, and
  a clickable button per applicable action.

Key handling is deliberately local: action keys (``u``, ``b``, ``D`` …) only
fire while the panel has focus, so they can never collide with the input
widget.  ``escape`` posts ``ExtensionPanel.Close`` (the app hides the sidebar,
the window dismisses itself).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from rich.text import Text

try:
    from textual import events, work
    from textual.app import ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.message import Message
    from textual.screen import ModalScreen
    from textual.widgets import Button, Input, Static, Tree
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
    run_ui_describe,
    run_ui_preview,
    run_ui_snapshot,
)
from agent.transports.tui_utils.ui_tree import DEFAULT_STYLE_MAP, node_text

logger = logging.getLogger(__name__)

# ``UINode.style`` hints → Rich styles; override via ``ExtensionPanel(style_map=...)``.
_DEFAULT_STYLE_MAP: dict[str, str] = dict(DEFAULT_STYLE_MAP)

BUSY_HINT = "⏳ agent running — cancel first (ctrl+x)"

# Panel-local key that zooms the sidebar into a PanelWindow (and back).  An
# extension action bound to the same key wins.
ZOOM_KEY = "z"


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
            yield Static(Text(self._message), classes="message")
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


class PanelTitle(Static):
    """Title row; a click asks the owning panel to zoom (sidebar) or close (window)."""

    class Clicked(Message):
        pass

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(self.Clicked())


class ExtensionPanel(Vertical):
    """Tree + actions for one extension :class:`UIPanel`.

    Collaborators are injected as callables so the widget stays independent of
    :class:`AarFixedApp`:

    * ``ctx_getter()`` — the current ``ExtensionContext`` (the app calls
      ``ExtensionManager.update_session`` first, exactly like slash commands).
    * ``write_system(text)`` — async; prints a line into the chat body.
    * ``is_busy()`` — ``True`` while the agent worker is running.

    ``mode`` is ``"sidebar"`` (compact column) or ``"window"`` (zoomed; see
    the module docstring).
    """

    DEFAULT_CSS = """
    ExtensionPanel {
        width: 100%;
        height: 1fr;
    }
    ExtensionPanel > PanelTitle {
        height: 1;
        padding: 0 1;
        text-style: bold;
        background: $boost;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    ExtensionPanel > PanelTitle:hover {
        background: $accent 30%;
    }
    ExtensionPanel Tree {
        height: 1fr;
        scrollbar-size-vertical: 1;
    }
    ExtensionPanel > Static.hints {
        height: auto;
        max-height: 3;
        padding: 0 1;
        color: $text-muted;
    }
    ExtensionPanel #panel-main {
        height: 1fr;
    }
    ExtensionPanel #panel-main > Tree {
        width: 1fr;
        min-width: 30;
    }
    ExtensionPanel #panel-detail-scroll {
        width: 45%;
        border-left: solid $primary-background;
        padding: 0 1;
    }
    ExtensionPanel #panel-actions {
        height: auto;
        padding: 0 1;
    }
    ExtensionPanel #panel-actions > Button {
        margin-right: 1;
    }
    """

    class Close(Message):
        """Posted when the user presses ``escape`` inside the panel."""

        def __init__(self, panel_widget: ExtensionPanel) -> None:
            super().__init__()
            self.panel_widget = panel_widget

    class Zoom(Message):
        """Posted by a sidebar panel when the user asks for the big window."""

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
        mode: str = "sidebar",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._panel = panel
        self._ctx_getter = ctx_getter
        self._write_system = write_system
        self._is_busy = is_busy or (lambda: False)
        self._style_map = {**_DEFAULT_STYLE_MAP, **(style_map or {})}
        self._mode = mode
        self.add_class(f"-{mode}")
        self._tree: Tree[UINode] = Tree(panel.title, id=f"panel-tree-{panel.name}")
        self._tree.show_root = False
        self._tree.guide_depth = 2 if mode == "sidebar" else 3
        self._title = PanelTitle(self._title_text(""), classes="title")
        self._hints = Static("", classes="hints")
        self._detail = Static("", id="panel-detail")
        self._actions_bar = Horizontal(id="panel-actions")
        self._root: UINode | None = None
        self._last_error: str = ""
        self._status: str = ""
        self._button_sig: tuple[Any, ...] = ()
        # Highlight events and an action worker's refresh can both rebuild
        # the buttons; interleaved remove/mount would collide on widget ids.
        self._buttons_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    @property
    def panel(self) -> UIPanel:
        return self._panel

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def root(self) -> UINode | None:
        """The most recent snapshot root (``None`` before the first refresh)."""
        return self._root

    @property
    def status(self) -> str:
        return self._status

    def compose(self) -> ComposeResult:
        yield self._title
        if self._mode == "window":
            with Horizontal(id="panel-main"):
                yield self._tree
                with VerticalScroll(id="panel-detail-scroll"):
                    yield self._detail
            yield self._actions_bar
        else:
            yield self._tree
        yield self._hints

    def focus_tree(self) -> None:
        self._tree.focus()

    def _title_text(self, status: str) -> Text:
        title = self._panel.title
        # Statuses often repeat the title's icon ("⎇ Shadow" / "⎇ shadow · 4 cp").
        icon = title.split(maxsplit=1)[0] if title else ""
        if icon and status.startswith(icon + " "):
            status = status[len(icon) + 1 :]
        text = Text()
        text.append(title, style="bold")
        if status:
            text.append(" · ", style="dim")
            text.append(status)
        hint = "  ⊞" if self._mode == "sidebar" else "  ✕ esc"
        text.append(hint, style="dim")
        return text

    def set_status(self, status: str) -> None:
        self._status = status
        self._title.update(self._title_text(status))

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    async def refresh_tree(self) -> None:
        """Re-run the panel's snapshot and rebuild the tree in place.

        Expansion state and the cursor are keyed on ``UINode.id`` so a refresh
        while the user is navigating doesn't jump.
        """
        ctx = self._ctx_getter()
        try:
            root = await run_ui_snapshot(self._panel, ctx)
            self._last_error = ""
        except Exception as exc:
            logger.warning("panel %r snapshot failed: %s", self._panel.name, exc)
            self._last_error = str(exc)
            root = UINode("root", f"✗ {exc}", "info", style="warn")
        self._rebuild(root)
        self._panel.changed.clear()
        self.set_status(self._panel.status_text(ctx))
        self._update_hints()
        await self._update_actions_bar()
        if self._mode == "window":
            self._load_detail(self.selected_node())

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
        label = node_text(node, self._style_map, with_detail=self._mode == "window")
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

    def select_node(self, node_id: str) -> bool:
        """Move the cursor to the node with *node_id* (used to hand the sidebar
        selection over to the zoomed window).  Returns ``False`` if unknown."""
        target = self._find_tree_node(self._tree.root, node_id)
        if target is None:
            return False
        self._tree.call_after_refresh(self._tree.move_cursor, target)
        return True

    # ------------------------------------------------------------------
    # Hints, action buttons, detail pane
    # ------------------------------------------------------------------

    def _update_hints(self) -> None:
        node = self.selected_node()
        zoom = f"[{ZOOM_KEY}] zoom" if self._mode == "sidebar" else f"[{ZOOM_KEY}]/esc close"
        if self._is_busy():
            movable = [a for a in self._panel.actions_for(node) if not a.mutates]
            parts = [BUSY_HINT]
            if movable:
                parts.append("  ".join(f"[{a.key}] {a.label}" for a in movable))
            self._hints.update(Text("\n".join(parts)))
            return
        actions = self._panel.actions_for(node)
        if self._mode == "window":
            # Actions are buttons in the window; the strip carries navigation.
            self._hints.update(
                Text(f"↑↓ move · space fold · click a button or press its key · {zoom}")
            )
            return
        if not actions:
            self._hints.update(Text(f"↑↓ move · space fold · {zoom} · esc hide"))
            return
        self._hints.update(Text("  ".join(f"[{a.key}] {a.label}" for a in actions) + f"  {zoom}"))

    async def _update_actions_bar(self) -> None:
        if self._mode != "window":
            return
        node = self.selected_node()
        busy = self._is_busy()
        actions = self._panel.actions_for(node)
        sig = (node.id if node else None, busy, tuple(a.id for a in actions))
        async with self._buttons_lock:
            if sig == self._button_sig:
                return
            self._button_sig = sig
            await self._actions_bar.remove_children()
            await self._mount_buttons(actions, busy)

    async def _mount_buttons(self, actions: list[UIAction], busy: bool) -> None:
        buttons = [
            Button(
                Text(f"{a.label} [{a.key}]"),
                id=f"panel-act-{a.id}",
                variant="error" if a.destructive else "default",
                compact=True,
                disabled=busy and a.mutates,
            )
            for a in actions
        ]
        if buttons:
            await self._actions_bar.mount_all(buttons)

    @work(exclusive=True, group="extension-panel-detail")
    async def _load_detail(self, node: UINode | None) -> None:
        if self._mode != "window":
            return
        if node is None:
            self._detail.update(Text(""))
            return
        await asyncio.sleep(0.05)  # debounce fast cursor movement
        try:
            text = await run_ui_describe(self._panel, node, self._ctx_getter())
        except Exception as exc:
            text = f"✗ {exc}"
        self._detail.update(Text(text))

    async def on_tree_node_highlighted(self, _event: Tree.NodeHighlighted) -> None:
        self._update_hints()
        await self._update_actions_bar()
        if self._mode == "window":
            self._load_detail(self.selected_node())

    def on_tree_node_selected(self, _event: Tree.NodeSelected) -> None:
        self._update_hints()

    def on_focus(self, _event: events.Focus) -> None:
        self._update_hints()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if not button_id.startswith("panel-act-"):
            return
        event.stop()
        action = self._panel.action(button_id[len("panel-act-") :])
        if action is not None:
            self.invoke(action, self.selected_node())
            self._tree.focus()

    def on_panel_title_clicked(self, event: PanelTitle.Clicked) -> None:
        event.stop()
        if self._mode == "sidebar":
            self.post_message(self.Zoom(self))
        else:
            self.post_message(self.Close(self))

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
        if event.key == ZOOM_KEY or event.character == ZOOM_KEY:
            event.stop()
            if self._mode == "sidebar":
                self.post_message(self.Zoom(self))
            else:
                self.post_message(self.Close(self))

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
            message = action.confirm.format(label=node.label) if action.confirm else action.label
            preview = await run_ui_preview(action, node, self._ctx_getter())
            if preview:
                message = f"{message}\n\n{preview}"
            modal = ConfirmModal(
                message,
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
            if self._mode == "window" and not action.mutates:
                # Read-only output (e.g. a diff) belongs next to the tree too.
                self._detail.update(Text(msg))
        await self.refresh_tree()
        if action.mutates:
            self.post_message(self.Mutated(self._panel.name, action.id))


class PanelWindow(ModalScreen[None]):
    """The zoomed view of one extension panel — nearly full screen.

    Hosts an :class:`ExtensionPanel` in ``"window"`` mode.  ``escape``, ``z``,
    a click on the title, or the app's panel key close it; ``Mutated``
    messages bubble on to the app like they do from the sidebar.
    """

    DEFAULT_CSS = """
    PanelWindow {
        align: center middle;
        background: $background 60%;
    }
    PanelWindow > #panel-window-frame {
        width: 94%;
        height: 90%;
        border: round $accent;
        background: $surface;
    }
    """

    def __init__(
        self,
        panel: UIPanel,
        *,
        ctx_getter: Callable[[], Any],
        write_system: Callable[[str], Any],
        is_busy: Callable[[], bool] | None = None,
        style_map: dict[str, str] | None = None,
        select_id: str | None = None,
    ) -> None:
        super().__init__()
        self._select_id = select_id
        self.panel_widget = ExtensionPanel(
            panel,
            ctx_getter=ctx_getter,
            write_system=write_system,
            is_busy=is_busy,
            style_map=style_map,
            mode="window",
            id=f"ext-window-{panel.name}",
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="panel-window-frame"):
            yield self.panel_widget

    async def on_mount(self) -> None:
        await self.panel_widget.refresh_tree()
        if self._select_id:
            self.panel_widget.select_node(self._select_id)
        self.panel_widget.focus_tree()

    def on_extension_panel_close(self, event: ExtensionPanel.Close) -> None:
        event.stop()
        self.dismiss(None)
