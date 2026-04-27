"""TUI transport — rich terminal UI for interactive agent sessions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from agent.core.config import AgentConfig

from rich.console import Console
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
    StreamChunk,
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


class TUIRenderer:
    """Renders agent events into a rich terminal display."""

    def __init__(
        self,
        console: Console | None = None,
        verbose: bool = False,
        theme: Theme | None = None,
        layout: LayoutConfig | None = None,
        config: "AgentConfig | None" = None,
    ) -> None:
        self.console = console or Console()
        self._verbose = verbose
        self._tool_calls: list[ToolCall] = []
        self._tool_results: list[ToolResult] = []
        self._usage_total: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self._total_cost: float = 0.0
        self._step_count = 0
        self.theme = theme or DEFAULT_THEME
        self.layout = layout or LayoutConfig()
        self._extension_panels: dict[str, Callable[[Console], None]] = {}
        self._streaming_active = False
        self._config: AgentConfig | None = config

    # ------------------------------------------------------------------
    # Theme switching
    # ------------------------------------------------------------------

    def set_theme(self, theme: Theme) -> None:
        """Switch to a new theme. Only affects future output."""
        self.theme = theme
        self.console.print(f"[{self.theme.dim_text}]Switched to theme: {theme.name}[/]")

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
    # Extension panels
    # ------------------------------------------------------------------

    def register_panel(self, name: str, renderer: Callable[[Console], None]) -> None:
        """Register an extension panel renderer."""
        self._extension_panels[name] = renderer

    def render_extension_panels(self) -> None:
        """Render all visible extension panels."""
        for name, render_fn in self._extension_panels.items():
            section = self.layout.extensions.get(name)
            if section and not section.visible:
                continue
            render_fn(self.console)

    # ------------------------------------------------------------------
    # Event rendering
    # ------------------------------------------------------------------

    def render_event(self, event: Event) -> None:
        """Render a single event to the terminal."""
        t = self.theme

        if isinstance(event, StreamChunk):
            if event.reasoning_text:
                if not getattr(self, "_thinking_active", False):
                    self._thinking_active = True
                    self.console.print("\n[dim]▸ thinking[/]")
                self.console.file.write(event.reasoning_text)
                self.console.file.flush()
            if event.text:
                if getattr(self, "_thinking_active", False):
                    self.console.file.write("\n")
                    self.console.file.flush()
                    self._thinking_active = False
                if not self._streaming_active:
                    self._streaming_active = True
                    self.console.print()  # blank line before streamed output
                self.console.file.write(event.text)
                self.console.file.flush()
            if event.finished and self._streaming_active:
                self.console.file.write("\n")
                self.console.file.flush()
            return

        if isinstance(event, AssistantMessage) and event.content:
            if self._streaming_active:
                # Content was already streamed token-by-token
                self._streaming_active = False
                return
            if not self.layout.assistant.visible:
                return
            self.console.print()
            self.console.print(
                Panel(
                    Markdown(event.content),
                    title=f"[{t.assistant.title_style}]Assistant[/]",
                    border_style=t.assistant.border_style,
                    padding=t.assistant.padding,
                )
            )

        elif isinstance(event, ToolCall):
            self._streaming_active = False  # reset between turns
            self._tool_calls.append(event)
            self._step_count += 1
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
            self.console.print(
                Panel(
                    args_display,
                    title=title,
                    border_style=t.tool_call.border_style,
                    padding=t.tool_call.padding,
                )
            )

        elif isinstance(event, ToolResult):
            self._tool_results.append(event)
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
            self.console.print(
                Panel(output, title=title, border_style=ps.border_style, padding=ps.padding)
            )

        elif isinstance(event, ReasoningBlock) and event.content:
            if not self.layout.reasoning.visible:
                return
            self.console.print(
                Panel(
                    Text(event.content, style=f"italic {t.reasoning.border_style}"),
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
            self.console.print(
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

            # Calculate step cost
            from agent.core.tokens import TokenUsage, calculate_cost, get_pricing

            usage_obj = TokenUsage.from_dict(u)
            pricing = get_pricing(event.model)
            step_cost = calculate_cost(usage_obj, pricing) if pricing else 0.0
            self._total_cost += step_cost

            if not self.layout.token_usage.visible:
                return

            from agent.transports.tui_utils.formatting import (
                format_token_display,
                is_over_warning_threshold,
            )

            warning = False
            if self._config:
                cfg = self._config
                token_total = self._usage_total["input_tokens"] + self._usage_total["output_tokens"]
                warning = is_over_warning_threshold(
                    token_total, cfg.token_budget, cfg.token_warning_threshold
                ) or is_over_warning_threshold(
                    self._total_cost, cfg.cost_limit, cfg.cost_warning_threshold
                )

            style = t.usage_warning_style if warning else t.usage_style
            self.console.print(
                Text(
                    f"  {u.get('input_tokens', 0)}in / {u.get('output_tokens', 0)}out "
                    f"(total: {format_token_display(self._usage_total['input_tokens'], self._usage_total['output_tokens'], self._total_cost)})",
                    style=style,
                ),
                justify="right",
            )

    def render_status_bar(self, session: Session) -> None:
        """Print a status bar with session info."""
        if not self.layout.status_bar.visible:
            return
        t = self.theme
        status = Table.grid(padding=(0, 2))
        status.add_column(justify="left")
        status.add_column(justify="center")
        status.add_column(justify="right")
        total_tokens = self._usage_total["input_tokens"] + self._usage_total["output_tokens"]
        token_info = f"Tokens: {total_tokens}"
        if self._total_cost > 0:
            if self._total_cost < 0.01:
                token_info += f" (${self._total_cost:.4f})"
            else:
                token_info += f" (${self._total_cost:.2f})"
        status.add_row(
            f"[{t.dim_text}]Session: {session.session_id[:8]}...[/]",
            f"[{t.dim_text}]Steps: {session.step_count}[/]",
            f"[{t.dim_text}]{token_info}[/]",
        )
        self.console.print(status)

    def render_welcome(self, extra_commands: list[str] | None = None) -> None:
        if not self.layout.welcome.visible:
            return
        t = self.theme
        builtin = ["help", "quit", "model", "status", "tools", "policy", "theme", "clear"]
        cmds = builtin + list(extra_commands or [])
        cmds_markup = " ".join(f"[bold]/{c}[/]" for c in cmds)
        self.console.print(
            Panel(
                "[bold]Aar Agent TUI[/]\n\n"
                "Type your message and press Enter.\n"
                "Attach files with @path (e.g. @photo.jpg @audio.wav)\n"
                f"Commands: {cmds_markup}",
                border_style=t.welcome.border_style,
                padding=t.welcome.padding,
            )
        )

    def render_policy(self, config: AgentConfig) -> None:
        """Display the current safety policy as a table."""
        sc = config.safety
        t = Table(title="Safety Policy", show_header=True, header_style="bold")
        t.add_column("Setting", style="bold")
        t.add_column("Value")
        t.add_row("read_only", "[red]yes[/]" if sc.read_only else "[green]no[/]")
        t.add_row(
            "require_approval_for_writes",
            "[yellow]yes[/]" if sc.require_approval_for_writes else "[green]no[/]",
        )
        t.add_row(
            "require_approval_for_execute",
            "[yellow]yes[/]" if sc.require_approval_for_execute else "[green]no[/]",
        )
        t.add_row("sandbox", sc.sandbox.mode)
        t.add_row("log_all_commands", "yes" if sc.log_all_commands else "no")
        allowed = ", ".join(sc.allowed_paths) if sc.allowed_paths else "[dim]all (no whitelist)[/]"
        t.add_row("allowed_paths", allowed)
        t.add_row("denied_paths", f"[dim]{len(sc.denied_paths)} patterns[/]")
        self.console.print(t)


# Re-export formatting utilities (extracted to tui_utils for sharing with tui_fixed)
from agent.transports.tui_utils.formatting import (  # noqa: E402
    _format_args,
    _side_effect_badge,
)


async def run_tui(
    config: AgentConfig | None = None,
    agent: Agent | None = None,
    verbose: bool = False,
    session_id: str | None = None,
    theme_name: str | None = None,
) -> None:
    """Launch the TUI interactive loop.

    If *agent* is provided it is used as-is (e.g. with MCP tools already
    registered). Otherwise a new :class:`Agent` is created from *config*.
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
    layout = LayoutConfig.model_validate(config.tui.layout) if config.tui.layout else LayoutConfig()

    agent = agent or Agent(config=config)
    renderer = TUIRenderer(verbose=verbose, theme=theme, layout=layout, config=config)
    store = SessionStore(config.session_dir)
    session: Session | None = None

    if session_id:
        try:
            session = store.load(session_id)
            renderer.console.print(f"[{theme.dim_text}]Resumed session {session_id}[/]")
        except FileNotFoundError:
            renderer.console.print(f"[{theme.error.border_style}]Session {session_id} not found[/]")
            return

    agent.on_event(renderer.render_event)

    # Eagerly initialise extensions so their commands are known before the
    # welcome screen is rendered.  We need a temporary Session to satisfy
    # _init_extensions; the real session is created (or loaded) on first run.
    _bootstrap_session = session if session is not None else Session()
    try:
        await agent._init_extensions(_bootstrap_session)
    except Exception:
        pass  # extension load failures are already logged inside _init_extensions

    _ext_cmds = list(agent._extension_manager.commands.keys()) if agent._extension_manager else []
    renderer.render_welcome(extra_commands=_ext_cmds or None)

    try:
        while True:
            try:
                user_input = renderer.console.input(f"\n[{renderer.theme.prompt_style}]> [/]")
            except EOFError:
                break

            stripped = user_input.strip()
            if not stripped:
                continue

            # Handle TUI commands
            if stripped.lower() in {"/quit", "/exit", "/q"}:
                break
            elif stripped.lower() in {"/help", "/h"}:
                _ext_cmds_now = (
                    list(agent._extension_manager.commands.keys())
                    if agent._extension_manager
                    else []
                )
                renderer.render_welcome(extra_commands=_ext_cmds_now or None)
                continue
            elif stripped.lower() == "/status":
                if session:
                    renderer.render_status_bar(session)
                else:
                    renderer.console.print("[dim]No active session.[/dim]")
                continue
            elif stripped.lower() == "/tools":
                for spec in agent.registry.list_tools():
                    effects = ", ".join(e.value for e in spec.side_effects)
                    renderer.console.print(
                        f"  [bold]{spec.name}[/]  [{renderer.theme.dim_text}]({effects})[/]"
                        f"  {spec.description}"
                    )
                continue
            elif stripped.lower() == "/policy":
                renderer.render_policy(config)
                continue
            elif stripped.lower() == "/clear":
                renderer.console.clear()
                session = None
                _ext_cmds_now = (
                    list(agent._extension_manager.commands.keys())
                    if agent._extension_manager
                    else []
                )
                renderer.render_welcome(extra_commands=_ext_cmds_now or None)
                continue
            elif stripped.lower().startswith("/model"):
                parts = stripped.split(maxsplit=1)
                if len(parts) == 1:
                    p = agent.provider
                    renderer.console.print(f"[bold]Active:[/] {p.name}/{p.config.model}")
                    if agent.config.providers:
                        renderer.console.print(
                            f"[{renderer.theme.dim_text}]Available providers:[/]"
                        )
                        for k, v in agent.config.providers.items():
                            marker = (
                                " *"
                                if (v.name == p.config.name and v.model == p.config.model)
                                else ""
                            )
                            renderer.console.print(
                                f"  [{renderer.theme.dim_text}]{k}[/] → {v.name}/{v.model}{marker}"
                            )
                    else:
                        renderer.console.print(
                            f"[{renderer.theme.dim_text}]No named providers"
                            f" configured. Use /model <provider/model>"
                            f" for ad-hoc switch.[/]"
                        )
                else:
                    try:
                        desc = agent.switch_provider(parts[1].strip())
                        renderer.console.print(f"[green]Switched to {desc}[/]")
                    except (ValueError, Exception) as exc:
                        renderer.console.print(f"[{renderer.theme.error.border_style}]{exc}[/]")
                continue
            elif stripped.lower().startswith("/theme"):
                parts = stripped.split(maxsplit=1)
                if len(parts) == 1:
                    # List themes
                    renderer.console.print(
                        f"[{renderer.theme.dim_text}]Current theme:[/] "
                        f"[bold]{renderer.theme.name}[/]"
                    )
                    for tname in registry.list_names():
                        marker = " *" if tname == renderer.theme.name else ""
                        renderer.console.print(f"  [{renderer.theme.dim_text}]{tname}{marker}[/]")
                else:
                    arg = parts[1].strip()
                    if arg == "next":
                        renderer.cycle_theme(registry)
                    else:
                        try:
                            renderer.set_theme(registry.get(arg))
                        except KeyError:
                            renderer.console.print(
                                f"[{renderer.theme.error.border_style}]Unknown theme: {arg}[/]"
                            )
                continue

            # --- Extension slash-commands --------------------------------
            elif stripped.startswith("/"):
                cmd_name = stripped[1:].split()[0].lower()
                args_str = stripped[len(cmd_name) + 1 :].strip()
                ext_mgr = getattr(agent, "_extension_manager", None)
                if ext_mgr is not None:
                    cmds = ext_mgr.commands
                    if cmd_name in cmds:
                        # Sync so commands see the current session (loaded or live),
                        # not the empty bootstrap snapshot from _init_extensions.
                        if session is not None:
                            ext_mgr.update_session(session)
                        _, handler = cmds[cmd_name]
                        ctx = ext_mgr._context
                        try:
                            result = handler(args_str, ctx)
                            if result is not None:
                                renderer.console.print(str(result))
                        except Exception as exc:
                            renderer.console.print(
                                f"[{renderer.theme.error.border_style}]Extension command error: {exc}[/]"
                            )
                        continue
                renderer.console.print(f"[{renderer.theme.dim_text}]Unknown command: {stripped}[/]")
                continue

            # Parse multimodal attachments (@file syntax)
            content = parse_multimodal_input(stripped)
            if isinstance(content, list):
                has_audio = False
                for block in content:
                    if isinstance(block, ImageURLBlock):
                        renderer.console.print(f"[{renderer.theme.dim_text}]  Attached: image[/]")
                    elif isinstance(block, AudioBlock):
                        renderer.console.print(f"[{renderer.theme.dim_text}]  Attached: audio[/]")
                        has_audio = True
                if has_audio and not agent.provider.supports_audio:
                    renderer.console.print(
                        f"[{renderer.theme.badges.write}]Warning:[/] audio input is not "
                        f"supported by {agent.provider.name} (as of Ollama v0.20). "
                        "Audio will be dropped."
                    )

            # Run the agent
            renderer.console.print(Text("  Working...", style=renderer.theme.working_style))
            session = await agent.run(content, session)
            # If a recoverable error occurred (e.g. provider timeout), reset the
            # session state so the next turn works without starting a new session.
            if session.state == AgentState.ERROR:
                last_error = next(
                    (e for e in reversed(session.events) if isinstance(e, ErrorEvent)),
                    None,
                )
                if last_error and last_error.recoverable:
                    session.state = AgentState.COMPLETED
            store.save(session)

            # Render extension panels after each turn
            renderer.render_extension_panels()

    except KeyboardInterrupt:
        renderer.console.print(f"\n[{renderer.theme.dim_text}]Goodbye.[/]")

    if session:
        store.save(session)
        renderer.console.print(
            f"\n[{renderer.theme.dim_text}]Session saved: {session.session_id}[/]"
        )
