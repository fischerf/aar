"""Full-screen TUI with fixed header/footer, scrollable body, and input widget.

Built on `textual <https://textual.textualize.io>`_ for native scrollbars,
mouse wheel support, Page Up / Page Down, and a proper input line.  The
scrollable TUI (``tui.py``) remains available as the default ``aar tui`` mode;
pass ``--fixed`` to use this one.

Requires the ``tui-fixed`` optional extra::

    pip install "aar-agent[tui-fixed]"

Architecture — multiplexed streaming UI
----------------------------------------
Each phase of an LLM response gets its own widget mounted into a
``ChatBody`` (VerticalScroll):

* ``ThinkingBlock``  — plain-text stream for reasoning tokens (fast)
* ``AnswerBlock``    — batched Rich-Markdown stream for answer tokens
* ``RichBlock``      — static Rich renderable for tool calls / results / errors

This avoids the "split screen" problem of the old RichLog + separate
MarkdownStream approach: all content lives in one unified scroll container.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from agent.core.config import AgentConfig

from rich.markdown import Markdown as RichMarkdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.widgets import RichLog, Static
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "The fixed TUI requires the 'textual' package. "
        'Install it with: pip install "aar-agent[tui-fixed]"'
    ) from exc

from agent.core.agent import Agent
from agent.core.config import AgentConfig
from agent.core.events import (
    AssistantMessage,
    AudioBlock,
    ErrorEvent,
    Event,
    ImageURLBlock,
    ProviderMeta,
    ReasoningBlock,
    StreamChunk,
    ToolCall,
    ToolResult,
)
from agent.core.multimodal import parse_multimodal_input
from agent.core.session import Session
from agent.core.state import AgentState
from agent.memory.session_store import SessionStore
from agent.safety.permissions import ApprovalResult
from agent.transports.companion_state import get_git_health  # noqa: F401
from agent.transports.keybinds import KeyBinds
from agent.transports.prompt_queue import PromptQueue
from agent.transports.themes import Theme, ThemeRegistry
from agent.transports.themes.builtin import DEFAULT_THEME
from agent.transports.themes.models import LayoutConfig
from agent.transports.tui_utils.formatting import (
    _format_approval_args,
    _format_args,
    _side_effect_badge,
)

# ---------------------------------------------------------------------------
# Widget imports — classes extracted to agent.transports.tui_widgets.*
# Re-exported here for backward compatibility.
# ---------------------------------------------------------------------------
from agent.transports.tui_widgets.bars import (  # noqa: F401
    ApprovalBar,
    FooterBar,
    HeaderBar,
    SeparatorBar,
)
from agent.transports.tui_widgets.blocks import (  # noqa: F401
    AnswerBlock,
    RichBlock,
    SelectableRichLog,
    ThinkingBlock,
    _Block,
)
from agent.transports.tui_widgets.chat_body import ChatBody  # noqa: F401
from agent.transports.tui_widgets.companion import CompanionPanel, KaomojiCompanion  # noqa: F401
from agent.transports.tui_widgets.file_picker import FilePickerModal  # noqa: F401
from agent.transports.tui_widgets.input import HistoryInput, HistoryTextArea  # noqa: F401
from agent.transports.tui_widgets.log_viewer import TUI_LOG_HANDLER, LogViewerModal  # noqa: F401
from agent.transports.tui_widgets.thinking_panel import ThinkingPanel  # noqa: F401

# ---------------------------------------------------------------------------
# FixedTUIRenderer — routes agent events to the appropriate widgets
# ---------------------------------------------------------------------------


class FixedTUIRenderer:
    """Renders agent events into the chat UI.

    Supports two modes:
    - *App mode*: ``chat_body`` is set — blocks are mounted asynchronously.
    - *Test mode*: ``log`` is set — content is written synchronously to a
      ``SelectableRichLog`` (or any duck-typed stand-in).
    """

    def __init__(
        self,
        header: HeaderBar,
        footer: FooterBar,
        log: "RichLog | SelectableRichLog | None" = None,
        chat_body: ChatBody | None = None,
        thinking_panel: "ThinkingPanel | None" = None,
        companion: "CompanionPanel | None" = None,
        verbose: bool = False,
        theme: Theme | None = None,
        layout: LayoutConfig | None = None,
        config: "AgentConfig | None" = None,
    ) -> None:
        self._log = log
        self._chat_body = chat_body
        self._thinking_panel: ThinkingPanel | None = thinking_panel
        self._companion: "KaomojiCompanion | None" = None
        self._header = header
        self._footer = footer
        self._verbose = verbose
        self._usage_total: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self._total_cost: float = 0.0
        self._step_count = 0
        self.theme = theme or DEFAULT_THEME
        self.layout = layout or LayoutConfig()
        self._extension_panels: dict[str, Callable] = {}
        self._thinking_visible = True
        self._config: AgentConfig | None = config
        self._streaming_active = False
        self._stream_in_reasoning = False
        self._panel_thinking_active: bool = False  # True while a step's reasoning is streaming
        # Live streaming widget references (app mode only)
        self._current_thinking: ThinkingBlock | None = None
        self._current_answer: AnswerBlock | None = None

    def _write(self, content: object, raw: str = "", kind: str = "") -> None:
        """Write a content block — sync (test mode) or async mount (app mode)."""
        if self._log is not None:
            if hasattr(self._log, "write_block") and raw:
                self._log.write_block(content, raw=raw, kind=kind)
            else:
                self._log.write(content)
        if self._chat_body is not None:
            block = RichBlock(content, raw=raw, kind=kind)
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._chat_body._mount_block(block))
            except RuntimeError:
                pass  # no running loop (unit-test context without Textual)

    def _mount_streaming(self, widget: Static) -> None:
        """Mount a ThinkingBlock or AnswerBlock to the chat body."""
        if self._chat_body is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._chat_body._mount_block(widget))
            except RuntimeError:
                pass

    # ------------------------------------------------------------------
    # Theme switching
    # ------------------------------------------------------------------

    def set_theme(self, theme: Theme, app: "AarFixedApp") -> None:
        """Switch to a new theme and update all widgets."""
        self.theme = theme
        self._header.theme = theme
        self._footer.theme = theme
        self._footer.theme_name = theme.name
        if self._thinking_panel is not None:
            self._thinking_panel.apply_theme(theme, theme.fixed_layout.thinking_panel)
        app.apply_theme(theme)
        self._write(
            Text(f"Switched to theme: {theme.name}", style=theme.dim_text),
            raw=f"Switched to theme: {theme.name}",
            kind="system",
        )

    def cycle_theme(self, registry: ThemeRegistry, app: "AarFixedApp") -> None:
        """Cycle to the next available theme."""
        names = registry.list_names()
        if not names:
            return
        try:
            idx = names.index(self.theme.name)
            next_name = names[(idx + 1) % len(names)]
        except ValueError:
            next_name = names[0]
        self.set_theme(registry.get(next_name), app)

    # ------------------------------------------------------------------
    # Thinking toggle
    # ------------------------------------------------------------------

    def toggle_thinking(self) -> bool:
        """Toggle the thinking side panel. Returns the new visibility state."""
        self._thinking_visible = not self._thinking_visible
        self._header.thinking_enabled = self._thinking_visible
        self._header.refresh_info()
        if self._thinking_panel is not None:
            display = "block" if self._thinking_visible else "none"
            self._thinking_panel.styles.display = display
            # Also hide the parent right-column container so its fixed width
            # (typ. 40 cols) is released back to the ChatBody's ``1fr`` track
            # when the panel is hidden. Hiding only the panel leaves an empty
            # 40-column gutter because the Vertical parent still participates
            # in the Horizontal split's layout.
            parent = getattr(self._thinking_panel, "parent", None)
            if parent is not None and getattr(parent, "id", None) == "right-col":
                parent.styles.display = display
        label = "shown" if self._thinking_visible else "hidden"
        self._write(
            Text(f"Thinking panel {label}", style=self.theme.dim_text),
            raw=f"Thinking panel {label}",
            kind="system",
        )
        return self._thinking_visible

    # ------------------------------------------------------------------
    # Event rendering
    # ------------------------------------------------------------------

    def render_event(self, event: Event) -> None:  # noqa: C901
        """Render a single agent event."""
        t = self.theme

        # --- Streaming tokens -------------------------------------------------
        if isinstance(event, StreamChunk):
            if event.reasoning_text:
                if not self._streaming_active:
                    self._streaming_active = True
                    self._stream_in_reasoning = True
                    self._header.streaming = True
                    self._header.refresh_info()
                if self._thinking_panel is not None:
                    # Route reasoning to the side panel
                    if not self._panel_thinking_active:
                        self._panel_thinking_active = True
                        self._thinking_panel.begin_step(self._step_count + 1)
                    self._thinking_panel.append(event.reasoning_text)
                    if self._companion is not None:
                        self._companion.agent_thinking()
                elif self._thinking_visible:
                    # Fallback: no panel — stream inline to chat body (test mode)
                    if self._current_thinking is None:
                        self._current_thinking = ThinkingBlock(self.theme)
                        self._mount_streaming(self._current_thinking)
                    self._current_thinking.append(event.reasoning_text)

            if event.text:
                if not self._streaming_active:
                    self._streaming_active = True
                    self._header.streaming = True
                    self._header.refresh_info()
                if self._stream_in_reasoning:
                    self._stream_in_reasoning = False
                    # Finalize inline thinking block if panel is not in use
                    if self._thinking_panel is None and self._current_thinking is not None:
                        self._current_thinking.finalize()
                if self._current_answer is None:
                    self._current_answer = AnswerBlock(self.theme)
                    self._mount_streaming(self._current_answer)
                self._current_answer.append(event.text)
                if self._companion is not None:
                    self._companion.agent_streaming()

            if event.finished:
                self._stream_in_reasoning = False
                if self._thinking_panel is not None:
                    if self._panel_thinking_active:
                        self._thinking_panel.finalize_step()
                        self._panel_thinking_active = False
                elif self._current_thinking is not None:
                    self._current_thinking.finalize()
                    self._current_thinking = None
                # _streaming_active stays True until AssistantMessage arrives
                # so we know to finalize (not re-create) the answer block.
                self._header.streaming = False
                self._header.refresh_info()
            return

        # --- Final assistant message ------------------------------------------
        if isinstance(event, AssistantMessage) and event.content:
            if self._streaming_active:
                # Streaming completed: update blocks with authoritative content.
                self._streaming_active = False
                self._stream_in_reasoning = False
                if self._current_answer is not None:
                    self._current_answer.finalize(event.content)
                    self._current_answer = None
                # Fallback: finalize inline thinking block if panel is not in use
                if self._current_thinking is not None:
                    self._current_thinking.finalize()
                    self._current_thinking = None
                # Panel: finalize if still active (e.g. reasoning-only response)
                if self._thinking_panel is not None and self._panel_thinking_active:
                    self._thinking_panel.finalize_step()
                    self._panel_thinking_active = False
            else:
                # No streaming (e.g. non-streaming provider): write static block.
                if not self.layout.assistant.visible:
                    return
                self._write(
                    Panel(
                        RichMarkdown(event.content),
                        title=f"[{t.assistant.title_style}]Assistant[/]",
                        border_style=t.assistant.border_style,
                        padding=t.assistant.padding,
                    ),
                    raw=event.content,
                    kind="assistant",
                )
            if self._companion is not None:
                self._companion.agent_idle()
            return

        # --- Tool call --------------------------------------------------------
        if isinstance(event, ToolCall):
            self._streaming_active = False
            self._stream_in_reasoning = False
            self._step_count += 1
            self._footer.step_count = self._step_count
            self._footer.refresh()
            if not self.layout.tool_call.visible:
                return
            args_display = _format_args(event.arguments, verbose=self._verbose, theme=t)
            if self._verbose:
                badge = _side_effect_badge(event.data.get("side_effects", []), theme=t)
                badge_prefix = f"{badge} " if badge else ""
                title = (
                    f"{badge_prefix}[{t.tool_call.title_style}]{event.tool_name}[/]"
                    f" [{t.dim_text}](step {self._step_count})[/]"
                )
            else:
                title = (
                    f"[{t.tool_call.title_style}]Tool: {event.tool_name}[/]"
                    f" [{t.dim_text}](step {self._step_count})[/]"
                )
            import json

            try:
                raw_args = json.dumps(event.arguments, indent=2)
            except Exception:
                raw_args = str(event.arguments)
            self._write(
                Panel(
                    args_display,
                    title=title,
                    border_style=t.tool_call.border_style,
                    padding=t.tool_call.padding,
                ),
                raw=f"Tool: {event.tool_name}\n{raw_args}",
                kind="tool_call",
            )
            if self._companion is not None:
                self._companion.agent_step()

        # --- Tool result ------------------------------------------------------
        elif isinstance(event, ToolResult):
            if not self.layout.tool_result.visible:
                return
            ps = t.tool_error if event.is_error else t.tool_result
            output = event.output
            if len(output) > 1000:
                output = output[:1000] + "\n... (truncated)"
            if self._verbose and event.duration_ms > 0:
                duration = f" [{t.dim_text}]{event.duration_ms:.0f}ms[/]"
            else:
                duration = ""
            title = f"[{ps.title_style}]Result: {event.tool_name}[/]{duration}"
            if event.is_error:
                title += f" [{t.tool_error.border_style}]ERROR[/]"
            self._write(
                Panel(output, title=title, border_style=ps.border_style, padding=ps.padding),
                raw=output,
                kind="tool_result",
            )
            if event.is_error and self._companion is not None:
                self._companion.agent_error()

        # --- Reasoning block (non-streaming) ----------------------------------
        elif isinstance(event, ReasoningBlock) and event.content:
            if not self.layout.reasoning.visible:
                return
            text = event.content
            if self._thinking_panel is not None:
                # Route to the side panel — no truncation needed there
                self._thinking_panel.add_static_block(text, self._step_count + 1)
            else:
                # Fallback: write inline to chat body (test mode or panel-less mode)
                if not self._thinking_visible:
                    return
                if len(text) > 500:
                    text = text[:500] + "..."
                self._write(
                    Panel(
                        Text(text, style=f"italic {t.reasoning.border_style}"),
                        title=f"[{t.reasoning.title_style}]Thinking[/]",
                        border_style=t.reasoning.border_style,
                        padding=t.reasoning.padding,
                    ),
                    raw=text,
                    kind="reasoning",
                )

        # --- Error ------------------------------------------------------------
        elif isinstance(event, ErrorEvent):
            hint = (
                f"\n[{t.dim_text}]You can type your message again to retry.[/]"
                if event.recoverable
                else ""
            )
            self._write(
                Panel(
                    event.message + hint,
                    title=f"[{t.error.title_style}]Error[/]",
                    border_style=t.error.border_style,
                    padding=t.error.padding,
                ),
                raw=event.message,
                kind="error",
            )
            if self._companion is not None:
                self._companion.agent_error()

        # --- Provider metadata -----------------------------------------------
        elif isinstance(event, ProviderMeta):
            u = event.usage
            self._usage_total["input_tokens"] += u.get("input_tokens", 0)
            self._usage_total["output_tokens"] += u.get("output_tokens", 0)

            # Calculate step cost
            from agent.core.tokens import TokenUsage, calculate_cost, get_pricing

            usage_obj = TokenUsage.from_dict(u)
            pricing = get_pricing(event.model)
            step_cost = calculate_cost(usage_obj, pricing) if pricing else 0.0
            self._total_cost += step_cost

            # Check warning thresholds from config (if available via session)
            from agent.transports.tui_utils.formatting import is_over_warning_threshold

            warning = False
            if self._config:
                cfg = self._config
                token_total = self._usage_total["input_tokens"] + self._usage_total["output_tokens"]
                warning = is_over_warning_threshold(
                    token_total, cfg.token_budget, cfg.token_warning_threshold
                ) or is_over_warning_threshold(
                    self._total_cost, cfg.cost_limit, cfg.cost_warning_threshold
                )

            # Update header bar and force content refresh
            self._header.update_tokens(u, step_cost=step_cost, warning=warning)
            self._header.provider_name = event.provider
            self._header.model_name = event.model
            # Static.update() requires a running Textual app context.  In test
            # mode (no app) we fall back to refresh() which is a no-op outside
            # an active message pump.
            self._header.refresh_info()

            if not self.layout.token_usage.visible:
                return
            from agent.transports.tui_utils.formatting import format_token_display

            usage_text = (
                f"  {u.get('input_tokens', 0)}in / {u.get('output_tokens', 0)}out "
                f"(total: {format_token_display(self._usage_total['input_tokens'], self._usage_total['output_tokens'], self._total_cost)})"
            )
            style = t.usage_warning_style if warning else t.usage_style
            self._write(
                Text(usage_text, style=style),
                raw=usage_text.strip(),
                kind="usage",
            )

    def render_welcome(self, extra_commands: list[str] | None = None) -> None:
        if not self.layout.welcome.visible:
            return
        t = self.theme
        builtin = [
            "help",
            "quit",
            "model",
            "status",
            "tools",
            "policy",
            "theme",
            "think",
            "clear",
            "queue",
        ]
        cmds = builtin + list(extra_commands or [])
        cmds_markup = " ".join(f"[bold]/{c}[/]" for c in cmds)
        welcome_text = (
            "[bold]Aar Agent TUI (Textual)[/]\n\n"
            "Type your message and press Ctrl+S to send.\n"
            "Send while the agent is running to queue prompts.\n"
            "Use Enter for new lines in multi-line messages.\n"
            "Attach files with @path (e.g. @photo.jpg @audio.wav)\n"
            f"Commands: {cmds_markup}\n\n"
        )
        self._write(
            Panel(welcome_text, border_style=t.welcome.border_style, padding=t.welcome.padding),
            raw="Aar Agent TUI (Textual) — welcome",
            kind="welcome",
        )


# ---------------------------------------------------------------------------
# Textual App
# ---------------------------------------------------------------------------

# Single source of truth for key strings.  KeyBinds is defined in
# agent/transports/keybinds.py — change keys/labels there only.
_KB = KeyBinds()


class AarFixedApp(App):
    """Full-screen Textual application for the Textual TUI mode."""

    # ext_cmds is injected by run_tui_fixed after eager extension init.
    _ext_cmds: list[str] = []

    # Keys come from _KB so that keybinds.py remains the single source of
    # truth.  Only Textual-specific attrs (action name, priority, show) live
    # here, because those are framework wiring, not user-facing config.
    # NOTE: toggle_cp is listed here for future use; action_toggle_cp is not
    # yet implemented, so Ctrl+P is currently a no-op.
    BINDINGS = [
        Binding(_KB.scroll_up.key, "scroll_up", "Page Up", show=False),
        Binding(_KB.scroll_down.key, "scroll_down", "Page Down", show=False),
        Binding(_KB.cancel.key, "cancel_agent", "Cancel", show=False, priority=True),
        Binding(_KB.cycle_theme.key, "cycle_theme", "Cycle theme", show=False),
        Binding(
            _KB.toggle_thinking.key, "toggle_thinking", "Toggle thinking", show=False, priority=True
        ),
        Binding(_KB.clear_screen.key, "clear_screen", "Clear screen", show=False),
        Binding(_KB.toggle_log_viewer.key, "toggle_log_viewer", "Logs", show=False),
    ]

    CSS = """
    Screen {
        layout: vertical;
    }
    #body-split {
        height: 1fr;
        width: 100%;
    }
    #chat-body {
        width: 1fr;
        height: 100%;
        min-height: 4;
    }
    #right-col {
        height: 100%;
        width: 40;
    }
    ThinkingPanel {
        height: 1fr;
    }
    #input-sep {
        height: 1;
    }
    #user-input {
        height: 5;
        padding: 0 1;
    }
    #footer-sep {
        height: 1;
    }
    """

    def __init__(
        self,
        agent: Agent,
        config: AgentConfig,
        renderer: FixedTUIRenderer | None = None,
        theme: Theme | None = None,
        layout_config: LayoutConfig | None = None,
        registry: ThemeRegistry | None = None,
        verbose: bool = False,
        session_id: str | None = None,
    ) -> None:
        super().__init__()
        self._agent = agent
        self._config = config
        self._theme = theme or DEFAULT_THEME
        self._layout_config = layout_config or LayoutConfig()
        self._theme_registry = registry or ThemeRegistry()
        self._verbose = verbose
        self._session_id = session_id
        self._session: Session | None = None
        self._store = SessionStore(config.session_dir)
        self._renderer: FixedTUIRenderer | None = renderer
        self._cancel_event: asyncio.Event | None = None
        self._keybinds: KeyBinds = KeyBinds()
        self._prompt_queue: PromptQueue = PromptQueue()
        self._drain_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._agent_running: bool = False

    # ------------------------------------------------------------------
    # Compose the widget tree from theme layout config
    # ------------------------------------------------------------------

    def _make_body_split(self) -> Horizontal:
        """Build the horizontal body container: ChatBody + right column (companion + thinking panel)."""
        tp_cfg = self._theme.fixed_layout.thinking_panel
        panel = ThinkingPanel(self._theme, tp_cfg)
        if tp_cfg.side == "left":
            panel.add_class("_left_side")
        body = ChatBody(id="chat-body")

        # Companion now lives in the header bar as KaomojiCompanion.
        # The right column only holds the ThinkingPanel.
        right_col = Vertical(panel, id="right-col")

        if tp_cfg.side == "left":
            return Horizontal(right_col, body, id="body-split")
        return Horizontal(body, right_col, id="body-split")

    def compose(self) -> ComposeResult:
        fl = self._theme.fixed_layout

        region_sizes: dict[str, int | None] = {}
        for region in fl.regions:
            region_sizes[region.name] = region.size

        widget_map: dict[str, Callable[[], list]] = {
            "header": lambda: [
                HeaderBar(self._theme),
                SeparatorBar(
                    self._theme.header.separator.style,
                    self._theme.header.separator.character,
                ),
            ],
            "body": lambda: [self._make_body_split()],
            "input": lambda: [
                ApprovalBar(),
                SeparatorBar(
                    self._theme.footer.separator.style,
                    self._theme.footer.separator.character,
                ),
                HistoryTextArea(
                    id="user-input",
                    show_line_numbers=True,
                    send_key=self._keybinds.send.key,
                    history_prev_key=self._keybinds.history_prev.key,
                    history_next_key=self._keybinds.history_next.key,
                ),
            ],
            "footer": lambda: [
                SeparatorBar(
                    self._theme.footer.separator.style,
                    self._theme.footer.separator.character,
                ),
                FooterBar(self._theme, self._keybinds),
            ],
        }

        for region in fl.regions:
            if not region.visible:
                continue
            factory = widget_map.get(region.name)
            if factory:
                yield from factory()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _make_approval_callback(self):
        """Create an approval callback that shows the inline ApprovalBar."""
        app = self

        async def _approval(spec, tc) -> ApprovalResult:
            args_text = _format_approval_args(tc.arguments)
            approval_bar = app.query_one(ApprovalBar)
            done = approval_bar.show_prompt(tc.tool_name, args_text)
            await done.wait()
            return approval_bar.result

        return _approval

    def on_mount(self) -> None:
        chat_body = self.query_one("#chat-body", ChatBody)
        thinking_panel = self.query_one(ThinkingPanel)
        header = self.query_one(HeaderBar)
        footer = self.query_one(FooterBar)

        _active = self._config.resolve_provider()
        header.provider_name = _active.name
        header.model_name = _active.model

        # Apply selected block highlight style from theme to ChatBody CSS
        fl = self._theme.fixed_layout
        chat_body.styles.background = fl.body_background

        self._renderer = FixedTUIRenderer(
            chat_body=chat_body,
            thinking_panel=thinking_panel,
            header=header,
            footer=footer,
            verbose=self._verbose,
            theme=self._theme,
            layout=self._layout_config,
            config=self._config,
        )

        # Mount kaomoji companion into the header bar (right side) if enabled.
        cp_cfg = fl.companion
        if cp_cfg.enabled:
            try:
                from agent.transports.tui_widgets.companion import KaomojiCompanion

                header = self.query_one(HeaderBar)
                companion = KaomojiCompanion(self._theme, cp_cfg)
                header.mount(companion)
                self._renderer._companion = companion
            except Exception:
                pass

        self._agent.on_event(self._renderer.render_event)

        # Apply thinking panel styles from theme
        tp_cfg = fl.thinking_panel
        thinking_panel.styles.background = tp_cfg.background
        thinking_panel.styles.width = tp_cfg.width
        try:
            right_col = self.query_one("#right-col")
            right_col.styles.width = tp_cfg.width
        except Exception:
            pass
        sb = tp_cfg.scrollbar
        thinking_panel.styles.scrollbar_color = sb.color
        thinking_panel.styles.scrollbar_color_hover = sb.color_hover
        thinking_panel.styles.scrollbar_color_active = sb.color_active
        thinking_panel.styles.scrollbar_background = sb.background
        thinking_panel.styles.scrollbar_background_hover = sb.background_hover
        thinking_panel.styles.scrollbar_background_active = sb.background_active
        thinking_panel.styles.scrollbar_size_vertical = sb.size
        border_color = tp_cfg.border_style
        if tp_cfg.side == "left":
            thinking_panel.styles.border_right = ("solid", border_color)
            thinking_panel.styles.border_left = None
        else:
            thinking_panel.styles.border_left = ("solid", border_color)
            thinking_panel.styles.border_right = None
        # Start hidden if not enabled in theme config
        if not tp_cfg.enabled:
            thinking_panel.styles.display = "none"
            self._renderer._thinking_visible = False
            self._renderer._header.thinking_enabled = False

        approval_cb = self._make_approval_callback()
        if hasattr(self._agent, "executor") and hasattr(self._agent.executor, "permissions"):
            self._agent.executor.permissions._approval_callback = approval_cb

        self.apply_theme(self._theme)

        if self._session_id:
            try:
                self._session = self._store.load(self._session_id)
                header.session_id = self._session.session_id
                header.refresh_info()
                # Restore companion progress derived from the session's event history.
                # No separate save-file is needed: tool-call and error counts are
                # counted from session.events plus the companion_baseline watermark
                # that SessionStore.compact() writes before pruning old events.
                if cp_cfg.enabled and self._renderer and self._renderer._companion is not None:
                    self._renderer._companion.bootstrap_from_session(self._session)
                loop = asyncio.get_running_loop()
                loop.create_task(
                    chat_body._mount_block(
                        RichBlock(
                            Text(
                                f"Resumed session {self._session_id}",
                                style=self._theme.dim_text,
                            ),
                            raw=f"Resumed session {self._session_id}",
                            kind="system",
                        )
                    )
                )
            except FileNotFoundError:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    chat_body._mount_block(
                        RichBlock(
                            Text(
                                f"Session {self._session_id} not found",
                                style=self._theme.error.border_style,
                            ),
                            raw=f"Session {self._session_id} not found",
                            kind="error",
                        )
                    )
                )

        self._renderer.render_welcome(extra_commands=self._ext_cmds or None)

        # Start periodic git health polling for the companion
        if cp_cfg.enabled:
            self.run_worker(
                self._companion_git_poll(),
                exclusive=False,
                name="companion-git-poll",
            )

        # Start the prompt queue drain loop
        self._drain_task = asyncio.get_running_loop().create_task(
            self._prompt_queue.start_drain(
                run_fn=self._run_queued_prompt,
                is_idle_fn=self._agent_is_idle,
                on_dispatch=self._on_queue_dispatch,
            )
        )

        self.query_one("#user-input", HistoryTextArea).focus()

    def apply_theme(self, theme: Theme) -> None:
        """Apply theme colors to Textual widget styles."""
        self._theme = theme
        fl = theme.fixed_layout
        sb = fl.scrollbar

        region_sizes: dict[str, int | None] = {}
        for region in fl.regions:
            region_sizes[region.name] = region.size

        # Chat body
        try:
            chat_body = self.query_one("#chat-body", ChatBody)
            chat_body.styles.background = fl.body_background
            chat_body.styles.scrollbar_color = sb.color
            chat_body.styles.scrollbar_color_hover = sb.color_hover
            chat_body.styles.scrollbar_color_active = sb.color_active
            chat_body.styles.scrollbar_background = sb.background
            chat_body.styles.scrollbar_background_hover = sb.background_hover
            chat_body.styles.scrollbar_background_active = sb.background_active
            chat_body.styles.scrollbar_size_vertical = sb.size
        except Exception:
            pass

        # Input
        try:
            inp = self.query_one("#user-input", HistoryTextArea)
            inp.styles.background = fl.input_background
            ifield = fl.input_field
            inp.styles.border = (ifield.border_type, ifield.border_color)
            inp.styles.color = ifield.text_color
            inp._border_color = ifield.border_color
            inp._border_color_focus = ifield.border_color_focus
            inp._border_type = ifield.border_type
        except Exception:
            pass

        # Header
        try:
            header = self.query_one(HeaderBar)
            header.styles.background = theme.header.background.replace("on ", "")
            header_size = region_sizes.get("header")
            if header_size is not None:
                header.styles.height = header_size
        except Exception:
            pass

        # Footer
        try:
            footer = self.query_one(FooterBar)
            footer.styles.background = theme.footer.background.replace("on ", "")
            footer_size = region_sizes.get("footer")
            if footer_size is not None:
                footer.styles.height = footer_size
        except Exception:
            pass

        # Separator bars
        try:
            separators = self.query(SeparatorBar)
            for i, sep in enumerate(separators):
                if i == 0:
                    sep._style = theme.header.separator.style
                    sep._character = theme.header.separator.character
                else:
                    sep._style = theme.footer.separator.style
                    sep._character = theme.footer.separator.character
                sep.refresh()
        except Exception:
            pass

        # Thinking panel
        try:
            panel = self.query_one(ThinkingPanel)
            panel.apply_theme(theme, theme.fixed_layout.thinking_panel)
            # Preserve current visibility state
            if self._renderer and not self._renderer._thinking_visible:
                panel.styles.display = "none"
        except Exception:
            pass
        try:
            right_col = self.query_one("#right-col")
            right_col.styles.width = theme.fixed_layout.thinking_panel.width
        except Exception:
            pass

        # Kaomoji companion (lives inside HeaderBar)
        try:
            companion = self.query_one(KaomojiCompanion)
            companion.apply_theme(theme)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Key binding actions
    # ------------------------------------------------------------------

    def _scroll_speed(self) -> int:
        return self._theme.fixed_layout.scrollbar.scroll_speed

    def action_scroll_up(self) -> None:
        self.query_one("#chat-body", ChatBody).scroll_up(
            animate=False, duration=0, speed=self._scroll_speed()
        )

    def action_scroll_down(self) -> None:
        self.query_one("#chat-body", ChatBody).scroll_down(
            animate=False, duration=0, speed=self._scroll_speed()
        )

    def action_cycle_theme(self) -> None:
        """Ctrl+T — cycle to the next theme."""
        if self._renderer:
            self._renderer.cycle_theme(self._theme_registry, self)

    def action_toggle_thinking(self) -> None:
        """Ctrl+K — toggle reasoning/thinking block visibility."""
        if self._renderer:
            self._renderer.toggle_thinking()

    async def action_clear_screen(self) -> None:
        """Ctrl+L — clear the chat body and reset counters."""
        if not self._renderer:
            return
        chat_body = self.query_one("#chat-body", ChatBody)
        header = self.query_one(HeaderBar)
        footer = self.query_one(FooterBar)
        await chat_body.remove_children()
        chat_body.auto_scroll = True
        self._session = None
        header.session_id = ""
        header.input_tokens = 0
        header.output_tokens = 0
        header.state = "idle"
        header.streaming = False
        header.refresh_info()
        footer.step_count = 0
        footer.refresh()
        self._renderer._step_count = 0
        self._renderer._usage_total = {"input_tokens": 0, "output_tokens": 0}
        self._renderer._streaming_active = False
        self._renderer._stream_in_reasoning = False
        self._renderer._current_thinking = None
        self._renderer._current_answer = None
        self._renderer._panel_thinking_active = False
        # Clear the thinking panel
        try:
            thinking_panel = self.query_one(ThinkingPanel)
            await thinking_panel.clear_log()
        except Exception:
            pass
        _ext_mgr_clear = getattr(self._agent, "_extension_manager", None)
        _ext_cmds_now = (
            list(_ext_mgr_clear.commands.keys())
            if _ext_mgr_clear is not None
            else list(self._ext_cmds)
        )
        self._renderer.render_welcome(extra_commands=_ext_cmds_now or None)

    async def action_cancel_agent(self) -> None:
        """Ctrl+X — cancel the running agent."""
        if self._cancel_event is not None:
            self._cancel_event.set()
        cleared = self._prompt_queue.clear()
        for worker in self.workers:
            if getattr(worker, "name", "") == "agent-run" and worker.is_running:
                worker.cancel()
                if self._renderer:
                    # Finalize any live streaming blocks
                    if self._renderer._current_thinking is not None:
                        self._renderer._current_thinking.finalize()
                        self._renderer._current_thinking = None
                    if self._renderer._current_answer is not None:
                        self._renderer._current_answer.finalize()
                        self._renderer._current_answer = None
                    self._renderer._streaming_active = False
                    self._renderer._write(
                        Text("Cancelled", style=self._renderer.theme.error.border_style),
                        raw="Cancelled",
                        kind="system",
                    )
                    if cleared:
                        self._renderer._write(
                            Text(
                                f"Cleared {cleared} queued prompt(s)",
                                style=self._renderer.theme.dim_text,
                            ),
                            raw=f"Cleared {cleared} queued prompts",
                            kind="system",
                        )
                try:
                    header = self.query_one(HeaderBar)
                    header.streaming = False
                    header.state = "cancelled"
                    header.refresh_info()
                except Exception:
                    pass
                self._restore_input()
                break

    async def action_toggle_log_viewer(self) -> None:
        """Open/close the in-app log viewer modal (default: Ctrl+G)."""
        for screen in self.screen_stack:
            if isinstance(screen, LogViewerModal):
                self.pop_screen()
                return
        await self.push_screen(LogViewerModal())

    def on_history_text_area_at_triggered(self, _event: "HistoryTextArea.AtTriggered") -> None:
        """Open the file picker when the user types '@' in the input."""
        inp = self.query_one("#user-input", HistoryTextArea)

        def _on_picked(result: str | None) -> None:
            if result:
                inp.insert(f"@{result} ")
            inp.focus()

        self.push_screen(FilePickerModal(Path.cwd()), _on_picked)

    # ------------------------------------------------------------------
    # Input handling
    # ------------------------------------------------------------------

    async def on_history_text_area_submitted(self, event: "HistoryTextArea.Submitted") -> None:
        """Handle user input from the HistoryTextArea widget."""
        user_input = event.value
        inp = self.query_one("#user-input", HistoryTextArea)
        inp.text = ""
        stripped = user_input.strip()
        if not stripped:
            return

        inp.add_to_history(stripped)

        assert self._renderer is not None
        chat_body = self.query_one("#chat-body", ChatBody)
        header = self.query_one(HeaderBar)
        t = self._renderer.theme

        async def _write(content: object, raw: str = "", kind: str = "") -> None:
            await chat_body._mount_block(RichBlock(content, raw=raw, kind=kind))

        # Echo input
        await _write(Text(f"  > {stripped}", style=self._renderer.theme.prompt_style))

        # --- TUI commands ------------------------------------------------
        if stripped.lower() in {"/quit", "/exit", "/q"}:
            self.exit()
            return
        elif stripped.lower() == "/status":
            if not self._session:
                await _write(f"[{t.dim_text}]No active session.[/]")
            else:
                status = Table.grid(padding=(0, 2))
                status.add_column(justify="left")
                status.add_column(justify="center")
                status.add_column(justify="right")
                status.add_row(
                    f"[{t.dim_text}]Session: {self._session.session_id[:8]}...[/]",
                    f"[{t.dim_text}]Steps: {self._session.step_count}[/]",
                    f"[{t.dim_text}]State: {self._session.state.value}[/]",
                )
                await _write(status)
            return
        elif stripped.lower() == "/tools":
            for spec in self._agent.registry.list_tools():
                effects = ", ".join(e.value for e in spec.side_effects)
                await _write(
                    Text.from_markup(
                        f"  [bold]{spec.name}[/]  [{t.dim_text}]({effects})[/]  {spec.description}"
                    )
                )
            return
        elif stripped.lower() == "/policy":
            sc = self._config.safety
            tbl = Table(title="Safety Policy", show_header=True, header_style="bold")
            tbl.add_column("Setting", style="bold")
            tbl.add_column("Value")
            tbl.add_row("read_only", "[red]yes[/]" if sc.read_only else "[green]no[/]")
            tbl.add_row(
                "require_approval_for_writes",
                "[yellow]yes[/]" if sc.require_approval_for_writes else "[green]no[/]",
            )
            tbl.add_row(
                "require_approval_for_execute",
                "[yellow]yes[/]" if sc.require_approval_for_execute else "[green]no[/]",
            )
            tbl.add_row("sandbox", sc.sandbox.mode)
            tbl.add_row("log_all_commands", "yes" if sc.log_all_commands else "no")
            allowed = (
                ", ".join(sc.allowed_paths) if sc.allowed_paths else "[dim]all (no whitelist)[/]"
            )
            tbl.add_row("allowed_paths", allowed)
            tbl.add_row("denied_paths", f"[dim]{len(sc.denied_paths)} patterns[/]")
            await _write(tbl)
            return
        elif stripped.lower() == "/clear":
            await self.action_clear_screen()
            return
        elif stripped.lower().startswith("/model"):
            parts = stripped.split(maxsplit=1)
            if len(parts) == 1:
                p = self._agent.provider
                await _write(Text.from_markup(f"[bold]Active:[/] {p.name}/{p.config.model}"))
                if self._agent.config.providers:
                    await _write(Text.from_markup(f"[{t.dim_text}]Available providers:[/]"))
                    for k, v in self._agent.config.providers.items():
                        marker = (
                            " *" if (v.name == p.config.name and v.model == p.config.model) else ""
                        )
                        await _write(
                            Text.from_markup(f"  [{t.dim_text}]{k}[/] → {v.name}/{v.model}{marker}")
                        )
                else:
                    await _write(
                        Text.from_markup(
                            f"[{t.dim_text}]No named providers configured. "
                            f"Use /model <provider/model> for ad-hoc switch.[/]"
                        )
                    )
            else:
                try:
                    desc = self._agent.switch_provider(parts[1].strip())
                    await _write(Text.from_markup(f"[green]Switched to {desc}[/]"))
                except (ValueError, Exception) as exc:
                    await _write(Text(str(exc), style=t.error.border_style))
            return
        elif stripped.lower().startswith("/theme"):
            parts = stripped.split(maxsplit=1)
            if len(parts) == 1:
                await _write(Text.from_markup(f"[{t.dim_text}]Current theme:[/] [bold]{t.name}[/]"))
                for tname in self._theme_registry.list_names():
                    marker = " *" if tname == t.name else ""
                    await _write(Text.from_markup(f"  [{t.dim_text}]{tname}{marker}[/]"))
            else:
                arg = parts[1].strip()
                if arg == "next":
                    self._renderer.cycle_theme(self._theme_registry, self)
                else:
                    try:
                        self._renderer.set_theme(self._theme_registry.get(arg), self)
                    except KeyError:
                        await _write(Text(f"Unknown theme: {arg}", style=t.error.border_style))
            return
        elif stripped.lower() == "/think":
            self._renderer.toggle_thinking()
            return
        elif stripped.lower().startswith("/queue"):
            parts = stripped.split(maxsplit=1)
            if len(parts) == 1 or parts[1].strip() == "":
                if self._prompt_queue.is_empty:
                    await _write(Text("No queued prompts.", style=t.dim_text))
                else:
                    for i, qp in enumerate(self._prompt_queue._queue):
                        preview = qp.content if isinstance(qp.content, str) else "[multimodal]"
                        if len(preview) > 60:
                            preview = preview[:57] + "..."
                        await _write(Text.from_markup(f"  [{t.dim_text}]{i + 1}.[/] {preview}"))
            elif parts[1].strip().lower() == "clear":
                count = self._prompt_queue.clear()
                try:
                    header = self.query_one(HeaderBar)
                    header.queue_depth = 0
                    header.refresh_info()
                except Exception:
                    pass
                await _write(Text(f"Cleared {count} queued prompt(s).", style=t.dim_text))
            else:
                await _write(Text("Usage: /queue or /queue clear", style=t.dim_text))
            return
        elif stripped.lower() in {"/help", "/h"}:
            _ext_mgr_help = getattr(self._agent, "_extension_manager", None)
            _ext_cmds_now = (
                list(_ext_mgr_help.commands.keys())
                if _ext_mgr_help is not None
                else list(self._ext_cmds)
            )
            self._renderer.render_welcome(extra_commands=_ext_cmds_now or None)
            return
        # --- Extension slash-commands ------------------------------------
        elif stripped.startswith("/"):
            cmd_name = stripped[1:].split()[0].lower()
            args_str = stripped[len(cmd_name) + 1 :].strip()
            ext_mgr = getattr(self._agent, "_extension_manager", None)
            if ext_mgr is not None:
                cmds = ext_mgr.commands
                if cmd_name in cmds:
                    # Sync so commands see the current session (loaded or live),
                    # not the empty bootstrap snapshot from _init_extensions.
                    if self._session is not None:
                        ext_mgr.update_session(self._session)
                    _, handler = cmds[cmd_name]
                    ctx = ext_mgr._context
                    try:
                        result = handler(args_str, ctx)
                        if result is not None:
                            for line in str(result).splitlines() or [str(result)]:
                                await _write(Text(line), raw=line, kind="system")
                    except Exception as exc:
                        await _write(
                            Text(f"Extension command error: {exc}", style=t.error.border_style)
                        )
                    return
            await _write(Text(f"Unknown command: {stripped}", style=t.dim_text))
            return
        # --- Parse multimodal attachments --------------------------------
        content = parse_multimodal_input(stripped)
        if isinstance(content, list):
            has_audio = False
            for block in content:
                if isinstance(block, ImageURLBlock):
                    await _write(Text("  Attached: image", style=self._renderer.theme.dim_text))
                elif isinstance(block, AudioBlock):
                    await _write(Text("  Attached: audio", style=self._renderer.theme.dim_text))
                    has_audio = True
            if has_audio and not self._agent.provider.supports_audio:
                await _write(
                    Text(
                        f"Warning: audio input is not supported by "
                        f"{self._agent.provider.name}. Audio will be dropped.",
                        style=self._renderer.theme.badges.write,
                    )
                )

        # --- Run agent or enqueue if busy ----------------------------------
        if not self._agent_is_idle():
            # Agent is busy — queue this prompt for later
            depth = self._prompt_queue.enqueue(content)
            header.queue_depth = depth
            header.refresh_info()
            await chat_body._mount_block(
                RichBlock(
                    Text(f"  Queued ({depth} pending)", style=self._renderer.theme.dim_text),
                    raw=f"Queued ({depth} pending)",
                    kind="system",
                )
            )
            return

        header.state = "running"
        header.refresh_info()
        chat_body.auto_scroll = True

        self._agent_running = True
        self._run_agent_worker(content)

    def _run_agent_worker(self, content: object) -> None:
        """Launch the agent in a Textual worker so the event loop stays free."""
        cancel_event = asyncio.Event()
        self._cancel_event = cancel_event

        async def _do_run() -> Session:
            return await self._agent.run(content, self._session, cancel_event=cancel_event)

        self.run_worker(_do_run(), exclusive=True, name="agent-run")

    async def on_worker_state_changed(self, event: object) -> None:
        """Handle agent worker completion."""
        worker = getattr(event, "worker", None)
        if worker is None or getattr(worker, "name", "") != "agent-run":
            return

        from textual.worker import WorkerState

        # Only act on terminal states — ignore PENDING and RUNNING transitions.
        if worker.state in {WorkerState.PENDING, WorkerState.RUNNING}:
            return

        if worker.state != WorkerState.SUCCESS:
            if worker.state == WorkerState.ERROR:
                try:
                    chat_body = self.query_one("#chat-body", ChatBody)
                    err_msg = str(worker.error) if worker.error else "Agent run failed"
                    await chat_body._mount_block(
                        RichBlock(
                            Text(f"Error: {err_msg}", style=self._theme.error.border_style),
                            raw=err_msg,
                            kind="error",
                        )
                    )
                except Exception:
                    pass
            if self._renderer and self._renderer._companion is not None:
                self._renderer._companion.agent_idle()
            self._restore_input()
            return

        session = worker.result
        if session is None:
            self._restore_input()
            return

        self._session = session
        header = self.query_one(HeaderBar)
        header.state = self._session.state.value
        header.session_id = self._session.session_id
        header.refresh_info()

        if self._session.state == AgentState.ERROR:
            last_error = next(
                (e for e in reversed(self._session.events) if isinstance(e, ErrorEvent)),
                None,
            )
            if last_error and last_error.recoverable:
                self._session.state = AgentState.COMPLETED
                header.state = "completed"
                header.refresh_info()

        self._store.save(self._session)
        if self._renderer and self._renderer._companion is not None:
            self._renderer._companion.agent_idle()
        self._restore_input()

    def _restore_input(self) -> None:
        """Mark the agent as idle and refocus the input widget."""
        self._agent_running = False
        try:
            header = self.query_one(HeaderBar)
            header.queue_depth = self._prompt_queue.depth
            header.refresh_info()
            inp = self.query_one("#user-input", HistoryTextArea)
            inp.focus()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Prompt queue helpers
    # ------------------------------------------------------------------

    def _agent_is_idle(self) -> bool:
        """Check whether the agent is ready for the next prompt."""
        if self._agent_running:
            return False
        if self._session is None:
            return True
        return self._session.state in {
            AgentState.IDLE,
            AgentState.COMPLETED,
            AgentState.ERROR,
        }

    async def _run_queued_prompt(self, content: str | list) -> None:
        """Dispatch a queued prompt through the normal submit pipeline."""
        chat_body = self.query_one("#chat-body", ChatBody)
        header = self.query_one(HeaderBar)

        # Echo the queued message in chat
        assert self._renderer is not None
        await chat_body._mount_block(
            RichBlock(
                Text(
                    f"  > {content}" if isinstance(content, str) else "  > [queued message]",
                    style=self._renderer.theme.prompt_style,
                ),
                raw=str(content),
                kind="user",
            )
        )

        header.state = "running"
        header.queue_depth = self._prompt_queue.depth
        header.refresh_info()
        chat_body.auto_scroll = True

        self._agent_running = True
        self._run_agent_worker(content)

    def _on_queue_dispatch(self, prompt: object, remaining: int) -> None:
        """Called by the drain loop right before dispatching a queued prompt."""
        try:
            header = self.query_one(HeaderBar)
            header.queue_depth = remaining
            header.refresh_info()
        except Exception:
            pass

    async def _companion_git_poll(self) -> None:
        """Periodically probe git health and update the companion's mood.

        Sleeps *first* so the initial probe is deferred — this keeps app
        startup fast and ensures Textual test teardown is never blocked by
        subprocess creation before the app has fully mounted.
        """
        import asyncio as _asyncio

        while True:
            await _asyncio.sleep(self._theme.fixed_layout.companion.git_poll_interval)
            try:
                health = await get_git_health()
                companion = self.query_one(KaomojiCompanion)
                companion.apply_git_health(health)
            except Exception:
                pass
            await _asyncio.sleep(self._theme.fixed_layout.companion.git_poll_interval)

    def on_unmount(self) -> None:
        """Clean up the prompt queue drain task on app teardown."""
        self._prompt_queue.stop_drain()
        if self._drain_task is not None:
            self._drain_task.cancel()
        if self._session:
            self._store.save(self._session)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run_tui_fixed(
    config: AgentConfig | None = None,
    agent: Agent | None = None,
    verbose: bool = False,
    session_id: str | None = None,
    theme_name: str | None = None,
) -> None:
    """Launch the full-screen TUI with fixed header/footer bars."""
    import logging as _logging
    import sys as _sys

    # ------------------------------------------------------------------
    # Redirect logging away from stderr while Textual owns the terminal.
    # Writing raw bytes to stderr mid-frame corrupts the layout.
    #
    # Strategy:
    #   1. Remove every StreamHandler that targets sys.stderr (added by
    #      configure_logging before we get here).
    #   2. Install TextualHandler — when the app is running, records go to
    #      the Textual devtools console (``textual console``).  When no
    #      devtools session is connected, records are silently discarded,
    #      which is acceptable: users who want persistent output should
    #      pass --log-file (the FileHandler survives untouched).
    #   3. Install TUI_LOG_HANDLER — buffers records and streams them into
    #      the in-app LogViewerModal (Ctrl+G).
    # ------------------------------------------------------------------
    try:
        from textual.logging import TextualHandler as _TextualHandler

        _root = _logging.getLogger()
        _root.handlers = [
            h
            for h in _root.handlers
            if not (isinstance(h, _logging.StreamHandler) and h.stream is _sys.stderr)
        ]
        if not any(isinstance(h, _TextualHandler) for h in _root.handlers):
            _root.addHandler(_TextualHandler())
        if not any(h is TUI_LOG_HANDLER for h in _root.handlers):
            _root.addHandler(TUI_LOG_HANDLER)
    except Exception:
        pass  # Never block startup over a logging reconfiguration error

    config = config or AgentConfig()

    registry = ThemeRegistry()
    name = theme_name or config.tui.theme
    try:
        theme = registry.get(name)
    except KeyError:
        theme = DEFAULT_THEME

    layout_config = (
        LayoutConfig.model_validate(config.tui.layout) if config.tui.layout else LayoutConfig()
    )

    agent = agent or Agent(config=config)

    # Eagerly initialise extensions before the Textual app launches so the
    # welcome screen can list all slash-commands (including extension ones)
    # from the very first render.
    try:
        from agent.core.session import Session as _Session

        _bootstrap = _Session()
        await agent._init_extensions(_bootstrap)
    except Exception:
        pass  # failures are logged inside _init_extensions

    _ext_cmds = list(agent._extension_manager.commands.keys()) if agent._extension_manager else []

    app = AarFixedApp(
        agent=agent,
        config=config,
        theme=theme,
        layout_config=layout_config,
        registry=registry,
        verbose=verbose,
        session_id=session_id,
    )
    app._ext_cmds = _ext_cmds
    await app.run_async()
