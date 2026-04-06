"""Full-screen TUI with fixed header/footer bars and scrollable conversation body.

Uses Rich Layout + Live for a persistent UI with themed status bars.
The scrollable TUI (tui.py) remains available as the default mode.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from typing import Callable

from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.layout import Layout
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

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
    ToolCall,
    ToolResult,
)
from agent.core.multimodal import parse_multimodal_input
from agent.core.session import Session
from agent.core.state import AgentState
from agent.memory.session_store import SessionStore
from agent.transports.themes import Theme, ThemeRegistry
from agent.transports.themes.builtin import DEFAULT_THEME
from agent.transports.themes.models import LayoutConfig
from agent.transports.tui import _format_args, _side_effect_badge


# ---------------------------------------------------------------------------
# ConversationBuffer — accumulates renderables for the scrollable body
# ---------------------------------------------------------------------------


class ConversationBuffer:
    """Accumulates rendered output for the scrollable body region.

    Implements ``__rich_console__`` so it can be used directly as a Rich
    renderable inside a Layout region.
    """

    def __init__(self, max_items: int = 5000) -> None:
        self._items: deque[RenderableType] = deque(maxlen=max_items)

    def append(self, renderable: RenderableType) -> None:
        self._items.append(renderable)

    def clear(self) -> None:
        self._items.clear()

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        for item in self._items:
            yield item


# ---------------------------------------------------------------------------
# HeaderBar — fixed top bar
# ---------------------------------------------------------------------------


class HeaderBar:
    """Renderable header showing provider, tokens, session, and state."""

    def __init__(self, theme: Theme) -> None:
        self.theme = theme
        self.provider_name: str = ""
        self.model_name: str = ""
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.session_id: str = ""
        self.state: str = "idle"

    def update_tokens(self, usage: dict[str, int]) -> None:
        self.input_tokens += usage.get("input_tokens", 0)
        self.output_tokens += usage.get("output_tokens", 0)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        h = self.theme.header
        grid = Table.grid(padding=(0, 2), expand=True)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1, justify="center")
        grid.add_column(ratio=1, justify="right")

        provider = f"[{h.provider_style}]{self.provider_name}"
        if self.model_name:
            provider += f" / {self.model_name}"
        provider += "[/]"

        tokens = f"[{h.tokens_style}]tokens: {self.input_tokens}in / {self.output_tokens}out[/]"

        right_parts: list[str] = []
        if self.session_id:
            right_parts.append(f"[{h.session_style}]{self.session_id[:8]}...[/]")
        right_parts.append(f"[{h.state_style}]{self.state}[/]")

        grid.add_row(provider, tokens, " | ".join(right_parts))
        sep = Text("─" * options.max_width, style=h.separator_style)
        yield grid
        yield sep


# ---------------------------------------------------------------------------
# FooterBar — fixed bottom bar
# ---------------------------------------------------------------------------


class FooterBar:
    """Renderable footer showing step count, theme, and input status."""

    def __init__(self, theme: Theme) -> None:
        self.theme = theme
        self.step_count: int = 0
        self.theme_name: str = theme.name
        self.status: str = ""

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        f = self.theme.footer
        sep = Text("─" * options.max_width, style=f.separator_style)
        yield sep

        grid = Table.grid(padding=(0, 2), expand=True)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1, justify="center")
        grid.add_column(ratio=1, justify="right")

        step = f"[{f.step_style}]step: {self.step_count}[/]"
        theme_info = f"[{f.theme_style}]theme: {self.theme_name}[/]"
        status = f"[{f.input_style}]{self.status}[/]" if self.status else ""

        grid.add_row(step, theme_info, status)
        yield grid


# ---------------------------------------------------------------------------
# FixedTUIRenderer — renders events into the ConversationBuffer
# ---------------------------------------------------------------------------


class FixedTUIRenderer:
    """Renders agent events into a :class:`ConversationBuffer` for the fixed TUI.

    Reuses the same theme and layout system as :class:`TUIRenderer` but writes
    to the buffer instead of directly to the console.
    """

    def __init__(
        self,
        buffer: ConversationBuffer,
        header: HeaderBar,
        footer: FooterBar,
        verbose: bool = False,
        theme: Theme | None = None,
        layout: LayoutConfig | None = None,
    ) -> None:
        self._buffer = buffer
        self._header = header
        self._footer = footer
        self._verbose = verbose
        self._usage_total: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self._step_count = 0
        self.theme = theme or DEFAULT_THEME
        self.layout = layout or LayoutConfig()
        self._extension_panels: dict[str, Callable[[ConversationBuffer], None]] = {}

    # ------------------------------------------------------------------
    # Theme switching
    # ------------------------------------------------------------------

    def set_theme(self, theme: Theme) -> None:
        """Switch to a new theme."""
        self.theme = theme
        self._header.theme = theme
        self._footer.theme = theme
        self._footer.theme_name = theme.name
        self._buffer.append(Text(f"Switched to theme: {theme.name}", style=theme.dim_text))

    def cycle_theme(self, registry: ThemeRegistry) -> None:
        """Cycle to the next available theme."""
        names = registry.list_names()
        if not names:
            return
        try:
            idx = names.index(self.theme.name)
            next_name = names[(idx + 1) % len(names)]
        except ValueError:
            next_name = names[0]
        self.set_theme(registry.get(next_name))

    # ------------------------------------------------------------------
    # Event rendering (into buffer)
    # ------------------------------------------------------------------

    def render_event(self, event: Event) -> None:
        """Render a single event into the conversation buffer."""
        t = self.theme

        if isinstance(event, AssistantMessage) and event.content:
            if not self.layout.assistant.visible:
                return
            self._buffer.append(Text())  # blank line
            self._buffer.append(
                Panel(
                    Markdown(event.content),
                    title=f"[{t.assistant.title_style}]Assistant[/]",
                    border_style=t.assistant.border_style,
                    padding=t.assistant.padding,
                )
            )

        elif isinstance(event, ToolCall):
            self._step_count += 1
            self._footer.step_count = self._step_count
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
            self._buffer.append(
                Panel(
                    args_display,
                    title=title,
                    border_style=t.tool_call.border_style,
                    padding=t.tool_call.padding,
                )
            )

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
            self._buffer.append(
                Panel(output, title=title, border_style=ps.border_style, padding=ps.padding)
            )

        elif isinstance(event, ReasoningBlock) and event.content:
            if not self.layout.reasoning.visible:
                return
            text = event.content
            if len(text) > 500:
                text = text[:500] + "..."
            self._buffer.append(
                Panel(
                    Text(text, style=f"italic {t.reasoning.border_style}"),
                    title=f"[{t.reasoning.title_style}]Thinking[/]",
                    border_style=t.reasoning.border_style,
                    padding=t.reasoning.padding,
                )
            )

        elif isinstance(event, ErrorEvent):
            hint = (
                f"\n[{t.dim_text}]You can type your message again to retry.[/]"
                if event.recoverable
                else ""
            )
            self._buffer.append(
                Panel(
                    event.message + hint,
                    title=f"[{t.error.title_style}]Error[/]",
                    border_style=t.error.border_style,
                    padding=t.error.padding,
                )
            )

        elif isinstance(event, ProviderMeta):
            u = event.usage
            self._usage_total["input_tokens"] += u.get("input_tokens", 0)
            self._usage_total["output_tokens"] += u.get("output_tokens", 0)
            self._header.update_tokens(u)
            self._header.provider_name = event.provider
            self._header.model_name = event.model
            if not self.layout.token_usage.visible:
                return
            self._buffer.append(
                Text(
                    f"  {u.get('input_tokens', 0)}in / {u.get('output_tokens', 0)}out "
                    f"(total: {self._usage_total['input_tokens']}in"
                    f" / {self._usage_total['output_tokens']}out)",
                    style=t.usage_style,
                )
            )

    def render_welcome(self) -> None:
        if not self.layout.welcome.visible:
            return
        t = self.theme
        self._buffer.append(
            Panel(
                "[bold]Aar Agent TUI (Fixed)[/]\n\n"
                "Type your message and press Enter.\n"
                "Attach files with @path (e.g. @photo.jpg @audio.wav)\n"
                "Commands: [bold]/quit[/] [bold]/status[/] [bold]/tools[/] "
                "[bold]/policy[/] [bold]/theme[/] [bold]/clear[/]",
                border_style=t.welcome.border_style,
                padding=t.welcome.padding,
            )
        )


# ---------------------------------------------------------------------------
# Threaded input helper
# ---------------------------------------------------------------------------


def _threaded_input(
    prompt: str, queue: asyncio.Queue[str | None], loop: asyncio.AbstractEventLoop
) -> None:
    """Read a line from stdin in a background thread and put it on *queue*."""
    try:
        line = input(prompt)
        loop.call_soon_threadsafe(queue.put_nowait, line)
    except (EOFError, KeyboardInterrupt):
        loop.call_soon_threadsafe(queue.put_nowait, None)


async def _get_input(prompt: str) -> str | None:
    """Get user input without blocking the event loop."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    thread = threading.Thread(target=_threaded_input, args=(prompt, queue, loop), daemon=True)
    thread.start()
    result = await queue.get()
    return result


# ---------------------------------------------------------------------------
# run_tui_fixed — full-screen entry point
# ---------------------------------------------------------------------------


async def run_tui_fixed(
    config: AgentConfig | None = None,
    agent: Agent | None = None,
    verbose: bool = False,
    session_id: str | None = None,
    theme_name: str | None = None,
) -> None:
    """Launch the full-screen TUI with fixed header/footer bars.

    This mode uses Rich's ``Layout`` + ``Live`` for a persistent UI with
    a scrollable conversation body and themed status bars.
    """
    config = config or AgentConfig()

    # Resolve theme
    registry = ThemeRegistry()
    name = theme_name or config.tui.theme
    try:
        theme = registry.get(name)
    except KeyError:
        theme = DEFAULT_THEME

    # Resolve layout
    layout_config = (
        LayoutConfig.model_validate(config.tui.layout) if config.tui.layout else LayoutConfig()
    )

    agent = agent or Agent(config=config)
    console = Console()

    # Build UI components
    buffer = ConversationBuffer()
    header = HeaderBar(theme)
    footer = FooterBar(theme)

    # Populate header from config
    header.provider_name = config.provider.name
    header.model_name = config.provider.model

    renderer = FixedTUIRenderer(
        buffer=buffer,
        header=header,
        footer=footer,
        verbose=verbose,
        theme=theme,
        layout=layout_config,
    )

    store = SessionStore(config.session_dir)
    session: Session | None = None

    if session_id:
        try:
            session = store.load(session_id)
            header.session_id = session.session_id
            buffer.append(Text(f"Resumed session {session_id}", style=theme.dim_text))
        except FileNotFoundError:
            buffer.append(Text(f"Session {session_id} not found", style=theme.error.border_style))
            return

    agent.on_event(renderer.render_event)
    renderer.render_welcome()

    # Build the three-region layout
    ui_layout = Layout()
    ui_layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=3),
    )

    def _refresh_layout() -> None:
        ui_layout["header"].update(header)
        ui_layout["body"].update(buffer)
        ui_layout["footer"].update(footer)

    _refresh_layout()

    try:
        with Live(
            ui_layout,
            console=console,
            screen=True,
            auto_refresh=True,
            refresh_per_second=8,
        ):
            while True:
                # Update footer prompt status
                footer.status = "> waiting for input..."
                _refresh_layout()

                # Get input via threaded reader (Live owns the screen)
                user_input = await _get_input("> ")
                if user_input is None:
                    break

                stripped = user_input.strip()
                if not stripped:
                    continue

                footer.status = ""

                # Handle TUI commands
                if stripped.lower() in {"/quit", "/exit", "/q"}:
                    break
                elif stripped.lower() == "/status" and session:
                    t = renderer.theme
                    buffer.append(
                        Text(
                            f"Session: {session.session_id[:8]}... | "
                            f"Steps: {session.step_count} | "
                            f"State: {session.state.value}",
                            style=t.dim_text,
                        )
                    )
                    _refresh_layout()
                    continue
                elif stripped.lower() == "/tools":
                    for spec in agent.registry.list_tools():
                        effects = ", ".join(e.value for e in spec.side_effects)
                        buffer.append(Text(f"  {spec.name} ({effects})  {spec.description}"))
                    _refresh_layout()
                    continue
                elif stripped.lower() == "/policy":
                    sc = config.safety
                    lines = [
                        f"  read_only: {sc.read_only}",
                        f"  require_approval_for_writes: {sc.require_approval_for_writes}",
                        f"  require_approval_for_execute: {sc.require_approval_for_execute}",
                        f"  sandbox: {sc.sandbox}",
                    ]
                    buffer.append(Text("Safety Policy", style="bold"))
                    for line in lines:
                        buffer.append(Text(line))
                    _refresh_layout()
                    continue
                elif stripped.lower() == "/clear":
                    buffer.clear()
                    session = None
                    header.session_id = ""
                    header.input_tokens = 0
                    header.output_tokens = 0
                    header.state = "idle"
                    footer.step_count = 0
                    renderer._step_count = 0
                    renderer._usage_total = {"input_tokens": 0, "output_tokens": 0}
                    renderer.render_welcome()
                    _refresh_layout()
                    continue
                elif stripped.lower().startswith("/theme"):
                    parts = stripped.split(maxsplit=1)
                    if len(parts) == 1:
                        buffer.append(
                            Text(
                                f"Current theme: {renderer.theme.name}",
                                style=renderer.theme.dim_text,
                            )
                        )
                        for tname in registry.list_names():
                            marker = " *" if tname == renderer.theme.name else ""
                            buffer.append(Text(f"  {tname}{marker}", style=renderer.theme.dim_text))
                    else:
                        arg = parts[1].strip()
                        if arg == "next":
                            renderer.cycle_theme(registry)
                        else:
                            try:
                                renderer.set_theme(registry.get(arg))
                            except KeyError:
                                buffer.append(
                                    Text(
                                        f"Unknown theme: {arg}",
                                        style=renderer.theme.error.border_style,
                                    )
                                )
                    _refresh_layout()
                    continue

                # Parse multimodal attachments (@file syntax)
                content = parse_multimodal_input(stripped)
                if isinstance(content, list):
                    has_audio = False
                    for block in content:
                        if isinstance(block, ImageURLBlock):
                            buffer.append(Text("  Attached: image", style=renderer.theme.dim_text))
                        elif isinstance(block, AudioBlock):
                            buffer.append(Text("  Attached: audio", style=renderer.theme.dim_text))
                            has_audio = True
                    if has_audio and not agent.provider.supports_audio:
                        buffer.append(
                            Text(
                                f"Warning: audio input is not supported by "
                                f"{agent.provider.name}. Audio will be dropped.",
                                style=renderer.theme.badges.write,
                            )
                        )

                # Show working indicator
                header.state = "running"
                footer.status = "working..."
                _refresh_layout()

                # Run the agent
                session = await agent.run(content, session)
                header.state = session.state.value
                header.session_id = session.session_id

                # Handle recoverable errors
                if session.state == AgentState.ERROR:
                    last_error = next(
                        (e for e in reversed(session.events) if isinstance(e, ErrorEvent)),
                        None,
                    )
                    if last_error and last_error.recoverable:
                        session.state = AgentState.COMPLETED
                        header.state = "completed"

                store.save(session)
                footer.status = ""
                _refresh_layout()

    except KeyboardInterrupt:
        pass

    # Restore terminal and show final message
    if session:
        store.save(session)
        console.print(f"\nSession saved: {session.session_id}")
    console.print("Goodbye.")
