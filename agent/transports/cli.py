"""CLI transport — primary entry point for the agent."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from agent.core.agent import Agent
from agent.core.config import AgentConfig, load_config
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
from agent.core.logging import configure_logging as _configure_logging
from agent.core.multimodal import parse_multimodal_input
from agent.core.session import Session
from agent.core.state import AgentState
from agent.memory.session_store import SessionStore
from agent.safety.permissions import ApprovalResult
from agent.transports.tui_utils.formatting import _format_approval_args

app = typer.Typer(name="aar", help="Lean Python Agent CLI", no_args_is_help=True)
console = Console()

_USER_DIR = Path.home() / ".aar"
_USER_CONFIG = _USER_DIR / "config.json"
_USER_MCP_CONFIG = _USER_DIR / "mcp_servers.json"

# Map provider name → env var that holds the API key
_PROVIDER_ENV_KEY: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "ollama": "",  # Ollama needs no key
    "generic": "",
}


def _build_config(
    model: Optional[str] = None,
    provider: Optional[str] = None,
    api_key: str = "",
    base_url: str = "",
    max_steps: Optional[int] = None,
    config_file: Optional[str] = None,
    read_only: Optional[bool] = None,
    require_approval: Optional[bool] = None,
    restrict_to_cwd: Optional[bool] = None,
    denied_paths: str = "",
    allowed_paths: str = "",
    log_level: Optional[str] = None,
    log_file: Optional[str] = None,
) -> AgentConfig:
    """Build an AgentConfig with layered precedence.

    Priority: explicit CLI flag > config file (--config or ~/.aar/config.json) > built-in defaults.
    ``None`` means "not explicitly set" — the loaded config (or built-in default) wins.
    """
    # Load config file or fall back to built-in defaults
    if config_file:
        cfg = load_config(Path(config_file))
    elif _USER_CONFIG.is_file():
        cfg = load_config(_USER_CONFIG)
    else:
        cfg = AgentConfig()

    # Provider settings — only override when explicitly passed
    if provider is not None:
        cfg.provider.name = provider
    if model is not None:
        cfg.provider.model = model
    if base_url:
        cfg.provider.base_url = base_url
    if max_steps is not None:
        cfg.max_steps = max_steps

    # api_key: CLI flag > env var matching the provider > loaded config
    env_var = _PROVIDER_ENV_KEY.get(cfg.provider.name, "ANTHROPIC_API_KEY")
    cfg.provider.api_key = (
        api_key or (os.environ.get(env_var, "") if env_var else "") or cfg.provider.api_key
    )

    # Safety — only override when the user explicitly passed the flag
    if read_only is not None:
        cfg.safety.read_only = read_only
    if require_approval is not None:
        cfg.safety.require_approval_for_writes = require_approval
        cfg.safety.require_approval_for_execute = require_approval
    if denied_paths:
        extra = [p.strip() for p in denied_paths.split(",") if p.strip()]
        cfg.safety.denied_paths = cfg.safety.denied_paths + extra
    if allowed_paths:
        cfg.safety.allowed_paths = [p.strip() for p in allowed_paths.split(",") if p.strip()]
    elif restrict_to_cwd is not None and restrict_to_cwd:
        cfg.safety.allowed_paths = [str(Path.cwd()) + "/**"]

    # Log level — CLI flag overrides config file value
    if log_level is not None:
        cfg.log_level = log_level.upper()

    if log_file is not None:
        cfg.log_file = Path(log_file)

    return cfg


def _apply_logging(config: AgentConfig) -> None:
    """Configure logging from the resolved AgentConfig."""
    _configure_logging(config.log_level, config.log_file)


async def _terminal_approval_callback(spec: Any, tc: Any) -> ApprovalResult:
    """Prompt the user in the terminal when a tool call requires approval."""
    args_text = _format_approval_args(tc.arguments)
    console.print()
    console.print(
        Panel(
            f"[bold]{tc.tool_name}[/]\n{args_text}",
            title="[bold red]Approval Required[/]",
            border_style="red",
        )
    )
    response = await asyncio.to_thread(
        console.input,
        "[bold]Allow? \\[y]es / \\[n]o / \\[a]lways:[/] ",
    )
    r = response.strip().lower()
    if r in {"a", "always"}:
        return ApprovalResult.APPROVED_ALWAYS
    if r in {"y", "yes"}:
        return ApprovalResult.APPROVED
    return ApprovalResult.DENIED


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


def _make_event_handler(verbose: bool = False):
    """Return an event handler callback, optionally with richer feedback."""
    _streaming_state = {"active": False}

    def _handler(event: Event) -> None:
        if isinstance(event, StreamChunk):
            if event.text:
                if not _streaming_state["active"]:
                    _streaming_state["active"] = True
                    console.print()  # blank line before streamed output
                console.file.write(event.text)
                console.file.flush()
            if event.reasoning_text and verbose:
                console.file.write(event.reasoning_text)
                console.file.flush()
            if event.finished and _streaming_state["active"]:
                console.file.write("\n")
                console.file.flush()
            return

        if isinstance(event, AssistantMessage) and event.content:
            if _streaming_state["active"]:
                # Content was already streamed token-by-token; skip duplicate render
                _streaming_state["active"] = False
                return
            console.print()
            console.print(Markdown(event.content))
        elif isinstance(event, ToolCall):
            _streaming_state["active"] = False  # reset between turns
            if verbose:
                badge = _side_effect_badge(event.data.get("side_effects", []))
                prefix = f"{badge} " if badge else ""
                console.print(f"\n{prefix}[bold yellow]{event.tool_name}[/]", highlight=False)
            else:
                console.print(f"\n[bold yellow]Tool:[/] {event.tool_name}", highlight=False)
            if event.arguments:
                for k, v in event.arguments.items():
                    val = str(v)
                    if len(val) > 200:
                        val = val[:200] + "..."
                    if verbose and _looks_like_path(val):
                        console.print(f"  [dim]{k}:[/] [bold blue]{val}[/]", highlight=False)
                    else:
                        console.print(f"  [dim]{k}:[/] {val}", highlight=False)
        elif isinstance(event, ToolResult):
            style = "red" if event.is_error else "green"
            output = event.output
            if len(output) > 500:
                output = output[:500] + "\n... (truncated)"
            if verbose and event.duration_ms > 0:
                duration = f" [dim]{event.duration_ms:.0f}ms[/]"
            else:
                duration = ""
            console.print(
                Panel(output, title=f"Result: {event.tool_name}{duration}", border_style=style)
            )
        elif isinstance(event, ReasoningBlock) and event.content:
            console.print(f"\n[dim italic]{event.content[:300]}[/]")
        elif isinstance(event, ErrorEvent):
            hint = (
                "\n[dim]You can type your message again to retry.[/]" if event.recoverable else ""
            )
            console.print(
                Panel(
                    event.message + hint,
                    title="[bold red]Error[/]",
                    border_style="red",
                    padding=(0, 2),
                )
            )
        elif isinstance(event, ProviderMeta):
            tokens = event.usage
            if tokens:
                console.print(
                    f"[dim]({tokens.get('input_tokens', 0)}in / {tokens.get('output_tokens', 0)}out)[/]",
                    justify="right",
                )

    return _handler


# ---------------------------------------------------------------------------
# MCP-aware agent creation
# ---------------------------------------------------------------------------


async def _run_with_mcp(
    coro_factory: Any,
    config: AgentConfig,
    mcp_config_path: str | None = None,
    approval_callback: Any = None,
) -> Any:
    """Run an async operation with optional MCP bridge lifecycle management.

    If *mcp_config_path* is provided (or ``~/.aar/mcp_servers.json`` exists),
    opens an :class:`MCPBridge`, registers all discovered tools into a shared
    :class:`ToolRegistry`, creates an :class:`Agent` with that registry, and
    calls ``coro_factory(agent)``. The bridge stays alive until the coroutine
    completes.

    Without any MCP config this simply creates a plain ``Agent`` and delegates
    to the coroutine factory.
    """
    # Auto-discover user-level MCP config when no explicit path is given
    if not mcp_config_path and _USER_MCP_CONFIG.is_file():
        mcp_config_path = str(_USER_MCP_CONFIG)

    if mcp_config_path:
        from agent.extensions.mcp import MCPBridge, load_mcp_config
        from agent.tools.registry import ToolRegistry

        servers = load_mcp_config(mcp_config_path)
        registry = ToolRegistry()
        async with MCPBridge(servers) as bridge:
            count = await bridge.register_all(registry)
            console.print(f"[dim]Registered {count} MCP tool(s)[/]")
            agent = Agent(config=config, registry=registry, approval_callback=approval_callback)
            return await coro_factory(agent)
    else:
        agent = Agent(config=config, approval_callback=approval_callback)
        return await coro_factory(agent)


# ---------------------------------------------------------------------------
# Async chat loop (keeps MCP bridge alive across turns)
# ---------------------------------------------------------------------------


async def _async_chat_loop(
    agent: Agent,
    config: AgentConfig,
    session_id: str | None = None,
    verbose: bool = False,
) -> None:
    """Interactive chat loop — fully async so the MCP bridge stays open."""
    agent.on_event(_make_event_handler(verbose))
    store = SessionStore(config.session_dir)
    session: Session | None = None

    if session_id:
        try:
            session = store.load(session_id)
            console.print(f"[dim]Resumed session {session_id}[/]")
        except FileNotFoundError:
            console.print(f"[red]Session {session_id} not found[/]")
            raise typer.Exit(1)

    console.print(
        Panel(
            "Agent ready. Type your message. Ctrl+C to exit.\n"
            "[dim]Attach files with @path (e.g. @photo.jpg @audio.wav)[/]",
            border_style="blue",
        )
    )

    try:
        while True:
            try:
                user_input = await asyncio.to_thread(console.input, "[bold blue]> [/]")
            except EOFError:
                break

            if not user_input.strip():
                continue
            if user_input.strip().lower() in {"/quit", "/exit", "/q"}:
                break

            content = parse_multimodal_input(user_input)
            if isinstance(content, list):
                has_audio = False
                for block in content:
                    if isinstance(block, ImageURLBlock):
                        console.print("[dim]  Attached: image[/]")
                    elif isinstance(block, AudioBlock):
                        console.print("[dim]  Attached: audio[/]")
                        has_audio = True
                if has_audio and not agent.provider.supports_audio:
                    console.print(
                        "[yellow]Warning:[/] audio input is not supported by "
                        f"{agent.provider.name} (as of Ollama v0.20). "
                        "Audio will be dropped."
                    )
            session = await agent.run(content, session)
            # Reset session state after a recoverable error (e.g. provider timeout)
            # so the user can retry without losing conversation history.
            if session.state == AgentState.ERROR:
                last_error = next(
                    (e for e in reversed(session.events) if isinstance(e, ErrorEvent)),
                    None,
                )
                if last_error and last_error.recoverable:
                    session.state = AgentState.COMPLETED
            store.save(session)

    except KeyboardInterrupt:
        console.print("\n[dim]Goodbye.[/]")

    if session:
        store.save(session)
        console.print(f"[dim]Session saved: {session.session_id}[/]")


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


@app.command()
def chat(
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Model to use"),
    provider: Optional[str] = typer.Option(
        None, "--provider", "-p", help="Provider name (anthropic, openai, ollama, generic)"
    ),
    base_url: str = typer.Option(
        "", "--base-url", help="Provider base URL (e.g. http://localhost:11434 for Ollama)"
    ),
    api_key: str = typer.Option(
        "", "--api-key", help="API key (overrides env var for the chosen provider)"
    ),
    max_steps: Optional[int] = typer.Option(None, "--max-steps", help="Maximum agent loop steps"),
    session_id: Optional[str] = typer.Option(None, "--session", "-s", help="Resume a session"),
    mcp_config: Optional[str] = typer.Option(
        None,
        "--mcp-config",
        help="Path to MCP servers JSON config (default: ~/.aar/mcp_servers.json)",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show side-effect badges, path highlights, and timing"
    ),
    config_file: Optional[str] = typer.Option(
        None, "--config", help="Path to AgentConfig JSON file (default: ~/.aar/config.json)"
    ),
    read_only: Optional[bool] = typer.Option(
        None, "--read-only/--no-read-only", help="Block all write and execute tools"
    ),
    require_approval: Optional[bool] = typer.Option(
        None,
        "--require-approval/--no-require-approval",
        help="Prompt before write/execute tools",
    ),
    restrict_to_cwd: Optional[bool] = typer.Option(
        None,
        "--restrict-to-cwd/--no-restrict-to-cwd",
        help="Restrict file tools to current directory",
    ),
    denied_paths: str = typer.Option(
        "", "--denied-paths", help="Comma-separated glob patterns to block (appended to defaults)"
    ),
    allowed_paths: str = typer.Option(
        "",
        "--allowed-paths",
        help="Comma-separated glob patterns to allow; overrides --restrict-to-cwd",
    ),
    log_level: Optional[str] = typer.Option(
        None,
        "--log-level",
        help="Log verbosity: DEBUG | INFO | WARNING | ERROR (overrides config file)",
    ),
    log_file: Optional[str] = typer.Option(
        None,
        "--log-file",
        help="Path to log file (append mode). Default: stderr only.",
    ),
) -> None:
    """Start an interactive chat session."""
    config = _build_config(
        model=model,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        max_steps=max_steps,
        config_file=config_file,
        read_only=read_only,
        require_approval=require_approval,
        restrict_to_cwd=restrict_to_cwd,
        denied_paths=denied_paths,
        allowed_paths=allowed_paths,
        log_level=log_level,
        log_file=log_file,
    )
    _apply_logging(config)
    asyncio.run(
        _run_with_mcp(
            lambda agent: _async_chat_loop(agent, config, session_id, verbose),
            config,
            mcp_config,
            approval_callback=_terminal_approval_callback,
        )
    )


@app.command()
def run(
    task: str = typer.Argument(..., help="Task to execute"),
    model: Optional[str] = typer.Option(None, "--model", "-m"),
    provider: Optional[str] = typer.Option(
        None, "--provider", "-p", help="Provider name (anthropic, openai, ollama, generic)"
    ),
    base_url: str = typer.Option(
        "", "--base-url", help="Provider base URL (e.g. http://localhost:11434 for Ollama)"
    ),
    api_key: str = typer.Option(
        "", "--api-key", help="API key (overrides env var for the chosen provider)"
    ),
    max_steps: Optional[int] = typer.Option(None, "--max-steps"),
    mcp_config: Optional[str] = typer.Option(
        None,
        "--mcp-config",
        help="Path to MCP servers JSON config (default: ~/.aar/mcp_servers.json)",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show side-effect badges, path highlights, and timing"
    ),
    session_id: Optional[str] = typer.Option(
        None, "--session", "-s", help="Resume a saved session"
    ),
    config_file: Optional[str] = typer.Option(
        None, "--config", help="Path to AgentConfig JSON file (default: ~/.aar/config.json)"
    ),
    read_only: Optional[bool] = typer.Option(
        None, "--read-only/--no-read-only", help="Block all write and execute tools"
    ),
    require_approval: Optional[bool] = typer.Option(
        None,
        "--require-approval/--no-require-approval",
        help="Prompt before write/execute tools",
    ),
    restrict_to_cwd: Optional[bool] = typer.Option(
        None,
        "--restrict-to-cwd/--no-restrict-to-cwd",
        help="Restrict file tools to current directory",
    ),
    denied_paths: str = typer.Option(
        "", "--denied-paths", help="Comma-separated glob patterns to block (appended to defaults)"
    ),
    allowed_paths: str = typer.Option(
        "",
        "--allowed-paths",
        help="Comma-separated glob patterns to allow; overrides --restrict-to-cwd",
    ),
    log_level: Optional[str] = typer.Option(
        None,
        "--log-level",
        help="Log verbosity: DEBUG | INFO | WARNING | ERROR (overrides config file)",
    ),
    log_file: Optional[str] = typer.Option(
        None,
        "--log-file",
        help="Path to log file (append mode). Default: stderr only.",
    ),
) -> None:
    """Run a single task and exit."""
    config = _build_config(
        model=model,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        max_steps=max_steps,
        config_file=config_file,
        read_only=read_only,
        require_approval=require_approval,
        restrict_to_cwd=restrict_to_cwd,
        denied_paths=denied_paths,
        allowed_paths=allowed_paths,
        log_level=log_level,
        log_file=log_file,
    )
    _apply_logging(config)

    async def _do(agent: Agent) -> None:
        agent.on_event(_make_event_handler(verbose))
        store = SessionStore(config.session_dir)
        session: Session | None = None
        if session_id:
            try:
                session = store.load(session_id)
                console.print(f"[dim]Resumed session {session_id}[/]")
            except FileNotFoundError:
                console.print(f"[red]Session {session_id} not found[/]")
                raise typer.Exit(1)
        content = parse_multimodal_input(task)
        if isinstance(content, list):
            has_audio = False
            for block in content:
                if isinstance(block, ImageURLBlock):
                    console.print("[dim]  Attached: image[/]")
                elif isinstance(block, AudioBlock):
                    console.print("[dim]  Attached: audio[/]")
                    has_audio = True
            if has_audio and not agent.provider.supports_audio:
                console.print(
                    "[yellow]Warning:[/] audio input is not supported by "
                    f"{agent.provider.name} (as of Ollama v0.20). "
                    "Audio will be dropped."
                )
        session = await agent.run(content, session)
        store.save(session)
        console.print(f"\n[dim]Session: {session.session_id}[/]")

    asyncio.run(
        _run_with_mcp(_do, config, mcp_config, approval_callback=_terminal_approval_callback)
    )


@app.command()
def sessions() -> None:
    """List saved sessions."""
    store = SessionStore()
    ids = store.list_sessions()
    if not ids:
        console.print("[dim]No saved sessions.[/]")
        return
    for sid in ids:
        console.print(f"  {sid}")


@app.command()
def prompt(
    model: Optional[str] = typer.Option(None, "--model", "-m"),
    provider: Optional[str] = typer.Option(
        None, "--provider", "-p", help="Provider name (anthropic, openai, ollama, generic)"
    ),
    config_file: Optional[str] = typer.Option(
        None, "--config", help="Path to AgentConfig JSON file (default: ~/.aar/config.json)"
    ),
    raw: bool = typer.Option(
        False,
        "--raw",
        help="Print as plain text without Rich formatting (useful for piping / diffing)",
    ),
) -> None:
    """Print the fully assembled system prompt so you can see exactly what the agent receives."""
    config = _build_config(
        model=model,
        provider=provider,
        config_file=config_file,
    )
    system_prompt = config.system_prompt

    if raw:
        typer.echo(system_prompt)
        return

    n_chars = len(system_prompt)
    n_lines = system_prompt.count("\n") + 1
    console.print(
        Panel(
            system_prompt,
            title="[bold cyan]System Prompt[/]",
            subtitle=f"[dim]{n_chars} chars · {n_lines} lines[/]",
            border_style="cyan",
            padding=(1, 2),
        )
    )


@app.command()
def tools(
    mcp_config: Optional[str] = typer.Option(
        None, "--mcp-config", help="Path to MCP servers JSON config"
    ),
) -> None:
    """List available tools."""
    config = _build_config()

    async def _do(agent: Agent) -> None:
        for spec in agent.registry.list_tools():
            effects = ", ".join(e.value for e in spec.side_effects)
            console.print(f"  [bold]{spec.name}[/]  [dim]({effects})[/]  {spec.description}")

    asyncio.run(_run_with_mcp(_do, config, mcp_config))


@app.command()
def tui(
    model: Optional[str] = typer.Option(None, "--model", "-m"),
    provider: Optional[str] = typer.Option(
        None, "--provider", "-p", help="Provider name (anthropic, openai, ollama, generic)"
    ),
    base_url: str = typer.Option(
        "", "--base-url", help="Provider base URL (e.g. http://localhost:11434 for Ollama)"
    ),
    api_key: str = typer.Option(
        "", "--api-key", help="API key (overrides env var for the chosen provider)"
    ),
    max_steps: Optional[int] = typer.Option(None, "--max-steps"),
    mcp_config: Optional[str] = typer.Option(
        None,
        "--mcp-config",
        help="Path to MCP servers JSON config (default: ~/.aar/mcp_servers.json)",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show side-effect badges, path highlights, and timing"
    ),
    session_id: Optional[str] = typer.Option(
        None, "--session", "-s", help="Resume a saved session"
    ),
    config_file: Optional[str] = typer.Option(
        None, "--config", help="Path to AgentConfig JSON file (default: ~/.aar/config.json)"
    ),
    read_only: Optional[bool] = typer.Option(
        None, "--read-only/--no-read-only", help="Block all write and execute tools"
    ),
    require_approval: Optional[bool] = typer.Option(
        None,
        "--require-approval/--no-require-approval",
        help="Prompt before write/execute tools",
    ),
    restrict_to_cwd: Optional[bool] = typer.Option(
        None,
        "--restrict-to-cwd/--no-restrict-to-cwd",
        help="Restrict file tools to current directory",
    ),
    denied_paths: str = typer.Option(
        "", "--denied-paths", help="Comma-separated glob patterns to block (appended to defaults)"
    ),
    allowed_paths: str = typer.Option(
        "",
        "--allowed-paths",
        help="Comma-separated glob patterns to allow; overrides --restrict-to-cwd",
    ),
    log_level: Optional[str] = typer.Option(
        None,
        "--log-level",
        help="Log verbosity: DEBUG | INFO | WARNING | ERROR (overrides config file)",
    ),
    log_file: Optional[str] = typer.Option(
        None,
        "--log-file",
        help="Path to log file (append mode). Default: stderr only.",
    ),
    theme: Optional[str] = typer.Option(
        None,
        "--theme",
        "-t",
        help="TUI theme name (default, contrast, decker, sleek) or path to theme JSON",
    ),
    fixed: bool = typer.Option(
        False,
        "--fixed",
        help="Full-screen mode with fixed bars, scrollable body, and mouse support (requires textual)",
    ),
) -> None:
    """Launch the rich TUI interface."""
    if fixed:
        from agent.transports.tui_fixed import run_tui_fixed as _run_tui
    else:
        from agent.transports.tui import run_tui as _run_tui

    config = _build_config(
        model=model,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        max_steps=max_steps,
        config_file=config_file,
        read_only=read_only,
        require_approval=require_approval,
        restrict_to_cwd=restrict_to_cwd,
        denied_paths=denied_paths,
        allowed_paths=allowed_paths,
        log_level=log_level,
        log_file=log_file,
    )
    _apply_logging(config)
    asyncio.run(
        _run_with_mcp(
            lambda agent: _run_tui(
                config, agent=agent, verbose=verbose, session_id=session_id, theme_name=theme
            ),
            config,
            mcp_config,
            approval_callback=_terminal_approval_callback,
        )
    )


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Bind address"),
    port: int = typer.Option(8080, "--port", help="Port to listen on"),
    model: Optional[str] = typer.Option(None, "--model", "-m"),
    provider: Optional[str] = typer.Option(
        None, "--provider", "-p", help="Provider name (anthropic, openai, ollama, generic)"
    ),
    base_url: str = typer.Option(
        "", "--base-url", help="Provider base URL (e.g. http://localhost:11434 for Ollama)"
    ),
    api_key: str = typer.Option(
        "", "--api-key", help="API key (overrides env var for the chosen provider)"
    ),
    mcp_config: Optional[str] = typer.Option(
        None,
        "--mcp-config",
        help="Path to MCP servers JSON config (default: ~/.aar/mcp_servers.json)",
    ),
    config_file: Optional[str] = typer.Option(
        None, "--config", help="Path to AgentConfig JSON file (default: ~/.aar/config.json)"
    ),
    read_only: Optional[bool] = typer.Option(
        None, "--read-only/--no-read-only", help="Block all write and execute tools"
    ),
    log_level: Optional[str] = typer.Option(
        None,
        "--log-level",
        help="Log verbosity: DEBUG | INFO | WARNING | ERROR (overrides config file)",
    ),
    log_file: Optional[str] = typer.Option(
        None,
        "--log-file",
        help="Path to log file (append mode). Default: stderr only.",
    ),
) -> None:
    """Start the web API server (requires uvicorn)."""
    config = _build_config(
        model=model,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        config_file=config_file,
        read_only=read_only,
        log_level=log_level,
        log_file=log_file,
    )
    _apply_logging(config)
    from agent.transports.web import create_asgi_app

    try:
        import uvicorn
    except ImportError:
        console.print(
            "[red]uvicorn is required for the web server. Install with: pip install uvicorn[/]"
        )
        raise typer.Exit(1)

    if mcp_config:
        console.print(
            "[yellow]Warning: --mcp-config is not yet supported for the serve command.[/]"
        )

    asgi_app = create_asgi_app(config)
    console.print(f"[bold green]Starting web server on {host}:{port}[/]")
    console.print("[dim]POST /chat, POST /chat/stream, GET /sessions, GET /health[/]")
    uvicorn.run(asgi_app, host=host, port=port, log_level=config.log_level.lower())


@app.command()
def acp(
    http: bool = typer.Option(
        False,
        "--http",
        help="Use HTTP/SSE transport instead of stdio (for REST clients, not Zed).",
    ),
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="HTTP bind address (--http only)"),
    port: int = typer.Option(8000, "--port", help="HTTP port (--http only)"),
    agent_name: str = typer.Option("aar", "--name", "-n", help="ACP agent name"),
    agent_description: str = typer.Option(
        "Aar adaptive action & reasoning agent",
        "--description",
        "-d",
        help="ACP agent description",
    ),
    model: Optional[str] = typer.Option(None, "--model", "-m"),
    provider: Optional[str] = typer.Option(
        None, "--provider", "-p", help="Provider name (anthropic, openai, ollama, generic)"
    ),
    base_url: str = typer.Option(
        "", "--base-url", help="Provider base URL (e.g. http://localhost:11434 for Ollama)"
    ),
    api_key: str = typer.Option(
        "", "--api-key", help="API key (overrides env var for the chosen provider)"
    ),
    config_file: Optional[str] = typer.Option(
        None, "--config", help="Path to AgentConfig JSON file (default: ~/.aar/config.json)"
    ),
    read_only: Optional[bool] = typer.Option(
        None, "--read-only/--no-read-only", help="Block all write and execute tools"
    ),
    log_level: Optional[str] = typer.Option(
        None,
        "--log-level",
        help="Log verbosity: DEBUG | INFO | WARNING | ERROR (overrides config file)",
    ),
    log_file: Optional[str] = typer.Option(
        None,
        "--log-file",
        help="Path to log file (append mode). Default: stderr only.",
    ),
) -> None:
    """Start an ACP agent (Agent Communication Protocol).

    Default (stdio): communicate over stdin/stdout using the official
    agent-client-protocol SDK.  This is the mode Zed and other editors
    use — add Aar via settings.json:

      "agent_servers": {
        "Aar": { "type": "custom", "command": "aar", "args": ["acp"] }
      }

    HTTP mode (--http): start a REST/SSE server for remote or programmatic
    access.  Requires uvicorn (pip install uvicorn).
    """
    config = _build_config(
        model=model,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        config_file=config_file,
        read_only=read_only,
        log_level=log_level,
        log_file=log_file,
    )
    _apply_logging(config)

    if http:
        from agent.transports.acp import create_acp_asgi_app

        try:
            import uvicorn
        except ImportError:
            console.print(
                "[red]uvicorn is required for --http mode. "
                "Install with: pip install uvicorn[/]",
                err=True,
            )
            raise typer.Exit(1)

        asgi_app = create_acp_asgi_app(
            config=config,
            agent_name=agent_name,
            agent_description=agent_description,
        )
        console.print(f"[bold green]ACP HTTP server on {host}:{port}[/]", err=True)
        uvicorn.run(asgi_app, host=host, port=port, log_level=config.log_level.lower())
    else:
        from agent.transports.acp import run_acp_stdio

        asyncio.run(
            run_acp_stdio(
                config=config,
                agent_name=agent_name,
            )
        )


@app.command()
def init(
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing config files"),
) -> None:
    """Create ~/.aar/config.json, ~/.aar/mcp_servers.json, and pricing/theme templates."""
    import json as _json

    _USER_DIR.mkdir(parents=True, exist_ok=True)

    # Derive the config dict from AgentConfig() so defaults live in one place
    defaults = AgentConfig()
    default_config = defaults.model_dump(mode="json", exclude={"system_prompt"})
    # Normalize path separators for cross-platform portability
    default_config["session_dir"] = str(defaults.session_dir.as_posix())

    # Empty by default — works out of the box with no MCP errors.
    # Users add entries by copying from mcp_servers.example.json.
    default_mcp: dict = {"servers": []}

    _USER_MCP_EXAMPLE = _USER_DIR / "mcp_servers.example.json"
    example_mcp: dict = {
        "servers": [
            {
                "name": "my-stdio-server",
                "transport": "stdio",
                "command": "python",
                "args": ["path/to/server.py"],
                "env": {},
                "prefix_tools": True,
            },
            {
                "name": "my-http-server",
                "transport": "http",
                "url": "http://localhost:8000/mcp",
                "headers": {"Authorization": "Bearer YOUR_TOKEN"},
                "prefix_tools": True,
            },
        ]
    }

    # Pricing template — copy of the built-in pricing.json so users can see the
    # format and add custom model prices (e.g. local Ollama models).
    _USER_PRICING_TEMPLATE = _USER_DIR / "pricing.template.json"
    from agent.core.tokens import get_builtin_pricing_path as _get_builtin_pricing_path

    try:
        _pricing_raw = _json.loads(_get_builtin_pricing_path().read_text(encoding="utf-8"))
        # Inject a top-level hint so users know what to do with this file.
        _pricing_raw["_usage"] = (
            "Copy this file to pricing.json in the same directory (~/.aar/) "
            "to activate custom prices. Entries here override the built-in table. "
            "Keys starting with '_' are ignored. Values are USD per 1 million tokens."
        )
    except Exception:
        _pricing_raw = {
            "_comment": "USD per 1 million tokens. Keys are model-name prefixes.",
            "_usage": "Copy this file to pricing.json to activate custom prices.",
        }

    # Theme directory and files
    _USER_THEMES_DIR = _USER_DIR / "themes"
    _USER_THEMES_DIR.mkdir(parents=True, exist_ok=True)

    _USER_THEME_EXAMPLE = _USER_THEMES_DIR / "example.json"

    from agent.transports.themes.builtin import DECKER_THEME
    from agent.transports.themes.models import Theme

    example_theme = _json.loads(DECKER_THEME.model_dump_json())
    example_theme["name"] = "example"
    example_theme["description"] = (
        "Example custom theme (copy of decker). Rename and edit to create your own."
    )

    theme_schema = Theme.model_json_schema()

    _USER_THEME_SCHEMA = _USER_THEMES_DIR / "theme.schema.template"

    created: list[str] = []
    skipped: list[str] = []

    for path, data in [
        (_USER_CONFIG, default_config),
        (_USER_MCP_CONFIG, default_mcp),
        (_USER_MCP_EXAMPLE, example_mcp),
        (_USER_PRICING_TEMPLATE, _pricing_raw),
        (_USER_THEME_EXAMPLE, example_theme),
        (_USER_THEME_SCHEMA, theme_schema),
    ]:
        if path.is_file() and not force:
            console.print(
                f"[yellow]Warning:[/] {path} already exists — skipping (use --force to overwrite)"
            )
            skipped.append(str(path))
        else:
            path.write_text(_json.dumps(data, indent=2), encoding="utf-8")
            created.append(str(path))
            console.print(f"[green]Created:[/] {path}")

    if created:
        console.print("\n[bold]Next steps:[/]")
        console.print(
            f"  1. Edit [bold]{_USER_CONFIG}[/] — set provider, model, api_key, base_url, etc."
        )
        console.print(
            f"  2. Copy entries from [bold]{_USER_MCP_EXAMPLE}[/] into"
            f" [bold]{_USER_MCP_CONFIG}[/] to enable MCP servers."
        )
        console.print("  3. Optionally add global rules to [bold]~/.aar/rules.md[/].")
        console.print(
            f"  4. To add custom model prices (e.g. local Ollama models), copy"
            f" [bold]{_USER_PRICING_TEMPLATE}[/] to [bold]{_USER_DIR / 'pricing.json'}[/]"
            f" and edit the entries."
        )
        console.print(
            f"  5. Create custom themes in [bold]{_USER_THEMES_DIR}[/]"
            f" — see [bold]{_USER_THEME_EXAMPLE}[/] for a template."
        )
        console.print("  6. Run [bold]aar chat[/] — no flags needed.")
    if skipped:
        console.print("\n[dim]Re-run with --force to overwrite skipped files.[/]")


if __name__ == "__main__":
    app()
