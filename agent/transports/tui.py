"""TUI transport — rich terminal UI for interactive agent sessions."""

from __future__ import annotations

import asyncio
from typing import Any

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from agent.core.agent import Agent
from agent.core.config import AgentConfig
from agent.core.events import (
    AssistantMessage,
    ErrorEvent,
    Event,
    EventType,
    ProviderMeta,
    ReasoningBlock,
    ToolCall,
    ToolResult,
)
from agent.core.session import Session
from agent.core.state import AgentState
from agent.memory.session_store import SessionStore


class TUIRenderer:
    """Renders agent events into a rich terminal display."""

    def __init__(self, console: Console | None = None, verbose: bool = False) -> None:
        self.console = console or Console()
        self._verbose = verbose
        self._tool_calls: list[ToolCall] = []
        self._tool_results: list[ToolResult] = []
        self._usage_total: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self._step_count = 0

    def render_event(self, event: Event) -> None:
        """Render a single event to the terminal."""
        if isinstance(event, AssistantMessage) and event.content:
            self.console.print()
            self.console.print(
                Panel(
                    Markdown(event.content),
                    title="[bold green]Assistant[/]",
                    border_style="green",
                    padding=(1, 2),
                )
            )

        elif isinstance(event, ToolCall):
            self._tool_calls.append(event)
            self._step_count += 1
            args_display = _format_args(event.arguments, verbose=self._verbose)
            if self._verbose:
                badge = _side_effect_badge(event.data.get("side_effects", []))
                badge_prefix = f"{badge} " if badge else ""
                title = f"{badge_prefix}[bold yellow]{event.tool_name}[/] [dim](step {self._step_count})[/]"
            else:
                title = f"[bold yellow]Tool: {event.tool_name}[/] [dim](step {self._step_count})[/]"
            self.console.print(
                Panel(args_display, title=title, border_style="yellow", padding=(0, 2))
            )

        elif isinstance(event, ToolResult):
            self._tool_results.append(event)
            style = "red" if event.is_error else "cyan"
            output = event.output
            if len(output) > 1000:
                output = output[:1000] + "\n... (truncated)"
            if self._verbose and event.duration_ms > 0:
                duration = f" [dim]{event.duration_ms:.0f}ms[/]"
            else:
                duration = ""
            title = f"[bold {style}]Result: {event.tool_name}[/]{duration}"
            if event.is_error:
                title += " [red]ERROR[/]"
            self.console.print(Panel(output, title=title, border_style=style, padding=(0, 2)))

        elif isinstance(event, ReasoningBlock) and event.content:
            text = event.content
            if len(text) > 500:
                text = text[:500] + "..."
            self.console.print(
                Panel(
                    Text(text, style="italic dim"),
                    title="[dim]Thinking[/]",
                    border_style="dim",
                    padding=(0, 2),
                )
            )

        elif isinstance(event, ErrorEvent):
            self.console.print(
                Panel(
                    event.message,
                    title="[bold red]Error[/]",
                    border_style="red",
                    padding=(0, 2),
                )
            )

        elif isinstance(event, ProviderMeta):
            u = event.usage
            self._usage_total["input_tokens"] += u.get("input_tokens", 0)
            self._usage_total["output_tokens"] += u.get("output_tokens", 0)
            self.console.print(
                Text(
                    f"  {u.get('input_tokens', 0)}in / {u.get('output_tokens', 0)}out "
                    f"(total: {self._usage_total['input_tokens']}in / {self._usage_total['output_tokens']}out)",
                    style="dim",
                ),
                justify="right",
            )

    def render_status_bar(self, session: Session) -> None:
        """Print a status bar with session info."""
        status = Table.grid(padding=(0, 2))
        status.add_column(justify="left")
        status.add_column(justify="center")
        status.add_column(justify="right")
        status.add_row(
            f"[dim]Session: {session.session_id[:8]}...[/]",
            f"[dim]Steps: {session.step_count}[/]",
            f"[dim]State: {session.state.value}[/]",
        )
        self.console.print(status)

    def render_welcome(self) -> None:
        self.console.print(
            Panel(
                "[bold]Aar Agent TUI[/]\n\n"
                "Type your message and press Enter.\n"
                "Commands: [bold]/quit[/] [bold]/status[/] [bold]/tools[/] [bold]/clear[/]",
                border_style="blue",
                padding=(1, 2),
            )
        )


_SIDE_EFFECT_BADGES = {
    "read": "[dim cyan][read][/]",
    "write": "[yellow][write][/]",
    "execute": "[red][exec][/]",
    "network": "[blue][net][/]",
    "external": "[magenta][ext][/]",
}


def _side_effect_badge(side_effects: list[str]) -> str:
    parts = [_SIDE_EFFECT_BADGES[e] for e in side_effects if e in _SIDE_EFFECT_BADGES]
    return " ".join(parts)


def _looks_like_path(s: str) -> bool:
    return len(s) < 120 and ("/" in s or "\\" in s)


def _format_args(arguments: dict[str, Any], verbose: bool = False) -> str:
    lines = []
    for k, v in arguments.items():
        val = str(v)
        if len(val) > 300:
            val = val[:300] + "..."
        if verbose and _looks_like_path(val):
            lines.append(f"[bold]{k}:[/] [bold blue]{val}[/]")
        else:
            lines.append(f"[bold]{k}:[/] {val}")
    return "\n".join(lines) if lines else "(no arguments)"


async def run_tui(
    config: AgentConfig | None = None,
    agent: Agent | None = None,
    verbose: bool = False,
) -> None:
    """Launch the TUI interactive loop.

    If *agent* is provided it is used as-is (e.g. with MCP tools already
    registered). Otherwise a new :class:`Agent` is created from *config*.
    """
    config = config or AgentConfig()
    agent = agent or Agent(config=config)
    renderer = TUIRenderer(verbose=verbose)
    store = SessionStore(config.session_dir)
    session: Session | None = None

    agent.on_event(renderer.render_event)
    renderer.render_welcome()

    try:
        while True:
            try:
                user_input = renderer.console.input("\n[bold blue]> [/]")
            except EOFError:
                break

            stripped = user_input.strip()
            if not stripped:
                continue

            # Handle TUI commands
            if stripped.lower() in {"/quit", "/exit", "/q"}:
                break
            elif stripped.lower() == "/status" and session:
                renderer.render_status_bar(session)
                continue
            elif stripped.lower() == "/tools":
                for spec in agent.registry.list_tools():
                    effects = ", ".join(e.value for e in spec.side_effects)
                    renderer.console.print(
                        f"  [bold]{spec.name}[/]  [dim]({effects})[/]  {spec.description}"
                    )
                continue
            elif stripped.lower() == "/clear":
                renderer.console.clear()
                session = None
                renderer.render_welcome()
                continue

            # Run the agent
            renderer.console.print(Text("  Working...", style="dim italic"))
            session = await agent.run(stripped, session)
            store.save(session)

    except KeyboardInterrupt:
        renderer.console.print("\n[dim]Goodbye.[/]")

    if session:
        store.save(session)
        renderer.console.print(f"\n[dim]Session saved: {session.session_id}[/]")
