"""CLI transport — primary entry point for the agent."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agent.core.agent import Agent
from agent.core.config import AgentConfig, ProviderConfig, SafetyConfig, load_config
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
from agent.memory.session_store import SessionStore
from agent.safety.permissions import ApprovalResult

app = typer.Typer(name="aar", help="Lean Python Agent CLI", no_args_is_help=True)
console = Console()

_DEFAULT_MODEL = "claude-sonnet-4-6"
_DEFAULT_PROVIDER = "anthropic"
_DEFAULT_MAX_STEPS = 50


def _build_config(
    model: str = _DEFAULT_MODEL,
    provider: str = _DEFAULT_PROVIDER,
    api_key: str = "",
    max_steps: int = _DEFAULT_MAX_STEPS,
    config_file: Optional[str] = None,
    read_only: bool = False,
    require_approval: bool = False,
    denied_paths: str = "",
    allowed_paths: str = "",
) -> AgentConfig:
    cfg = load_config(Path(config_file)) if config_file else AgentConfig()

    cfg.provider.name = provider
    cfg.provider.model = model
    cfg.provider.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    cfg.max_steps = max_steps

    if read_only:
        cfg.safety.read_only = True
    if require_approval:
        cfg.safety.require_approval_for_writes = True
        cfg.safety.require_approval_for_execute = True
    if denied_paths:
        extra = [p.strip() for p in denied_paths.split(",") if p.strip()]
        cfg.safety.denied_paths = cfg.safety.denied_paths + extra
    if allowed_paths:
        cfg.safety.allowed_paths = [p.strip() for p in allowed_paths.split(",") if p.strip()]

    return cfg


async def _terminal_approval_callback(spec: Any, tc: Any) -> ApprovalResult:
    """Prompt the user in the terminal when a tool call requires approval."""
    args_text = "\n".join(f"  {k}: {v}" for k, v in tc.arguments.items())
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

    def _handler(event: Event) -> None:
        if isinstance(event, AssistantMessage) and event.content:
            console.print()
            console.print(Markdown(event.content))
        elif isinstance(event, ToolCall):
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
            console.print(f"\n[bold red]Error:[/] {event.message}")
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

    If *mcp_config_path* is provided, opens an :class:`MCPBridge`, registers
    all discovered tools into a shared :class:`ToolRegistry`, creates an
    :class:`Agent` with that registry, and calls ``coro_factory(agent)``.
    The bridge stays alive until the coroutine completes.

    Without *mcp_config_path* this simply creates a plain ``Agent`` and
    delegates to the coroutine factory.
    """
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

    console.print(Panel("Agent ready. Type your message. Ctrl+C to exit.", border_style="blue"))

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

            session = await agent.run(user_input, session)
            store.save(session)

    except KeyboardInterrupt:
        console.print("\n[dim]Goodbye.[/]")

    if session:
        store.save(session)
        console.print(f"[dim]Session saved: {session.session_id}[/]")


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

_SAFETY_OPTIONS = {
    "config_file": typer.Option(None, "--config", help="Path to AgentConfig JSON file"),
    "read_only": typer.Option(False, "--read-only", help="Block all write and execute tools"),
    "require_approval": typer.Option(
        False, "--require-approval", help="Prompt before any write or execute tool"
    ),
    "denied_paths": typer.Option(
        "", "--denied-paths", help="Comma-separated glob patterns to block (appended to defaults)"
    ),
    "allowed_paths": typer.Option(
        "", "--allowed-paths", help="Comma-separated glob patterns to allow (restricts to whitelist)"
    ),
}


@app.command()
def chat(
    model: str = typer.Option(_DEFAULT_MODEL, "--model", "-m", help="Model to use"),
    provider: str = typer.Option(_DEFAULT_PROVIDER, "--provider", "-p", help="Provider name"),
    max_steps: int = typer.Option(
        _DEFAULT_MAX_STEPS, "--max-steps", help="Maximum agent loop steps"
    ),
    session_id: Optional[str] = typer.Option(None, "--session", "-s", help="Resume a session"),
    mcp_config: Optional[str] = typer.Option(
        None, "--mcp-config", help="Path to MCP servers JSON config"
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show side-effect badges, path highlights, and timing"
    ),
    config_file: Optional[str] = typer.Option(None, "--config", help="Path to AgentConfig JSON file"),
    read_only: bool = typer.Option(False, "--read-only", help="Block all write and execute tools"),
    require_approval: bool = typer.Option(
        False, "--require-approval", help="Prompt before any write or execute tool"
    ),
    denied_paths: str = typer.Option(
        "", "--denied-paths", help="Comma-separated glob patterns to block (appended to defaults)"
    ),
    allowed_paths: str = typer.Option(
        "", "--allowed-paths", help="Comma-separated glob patterns to allow (restricts to whitelist)"
    ),
) -> None:
    """Start an interactive chat session."""
    config = _build_config(
        model=model, provider=provider, max_steps=max_steps,
        config_file=config_file, read_only=read_only, require_approval=require_approval,
        denied_paths=denied_paths, allowed_paths=allowed_paths,
    )
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
    model: str = typer.Option(_DEFAULT_MODEL, "--model", "-m"),
    provider: str = typer.Option(_DEFAULT_PROVIDER, "--provider", "-p"),
    max_steps: int = typer.Option(_DEFAULT_MAX_STEPS, "--max-steps"),
    mcp_config: Optional[str] = typer.Option(
        None, "--mcp-config", help="Path to MCP servers JSON config"
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show side-effect badges, path highlights, and timing"
    ),
    config_file: Optional[str] = typer.Option(None, "--config", help="Path to AgentConfig JSON file"),
    read_only: bool = typer.Option(False, "--read-only", help="Block all write and execute tools"),
    require_approval: bool = typer.Option(
        False, "--require-approval", help="Prompt before any write or execute tool"
    ),
    denied_paths: str = typer.Option(
        "", "--denied-paths", help="Comma-separated glob patterns to block (appended to defaults)"
    ),
    allowed_paths: str = typer.Option(
        "", "--allowed-paths", help="Comma-separated glob patterns to allow (restricts to whitelist)"
    ),
) -> None:
    """Run a single task and exit."""
    config = _build_config(
        model=model, provider=provider, max_steps=max_steps,
        config_file=config_file, read_only=read_only, require_approval=require_approval,
        denied_paths=denied_paths, allowed_paths=allowed_paths,
    )

    async def _do(agent: Agent) -> None:
        agent.on_event(_make_event_handler(verbose))
        session = await agent.run(task)
        store = SessionStore(config.session_dir)
        store.save(session)
        console.print(f"\n[dim]Session: {session.session_id}[/]")

    asyncio.run(_run_with_mcp(_do, config, mcp_config, approval_callback=_terminal_approval_callback))


@app.command()
def resume(
    session_id: str = typer.Argument(..., help="Session ID to resume"),
    mcp_config: Optional[str] = typer.Option(
        None, "--mcp-config", help="Path to MCP servers JSON config"
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show side-effect badges, path highlights, and timing"
    ),
) -> None:
    """Resume a saved session."""
    config = _build_config()
    asyncio.run(
        _run_with_mcp(
            lambda agent: _async_chat_loop(agent, config, session_id, verbose),
            config,
            mcp_config,
            approval_callback=_terminal_approval_callback,
        )
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
    model: str = typer.Option(_DEFAULT_MODEL, "--model", "-m"),
    provider: str = typer.Option(_DEFAULT_PROVIDER, "--provider", "-p"),
    max_steps: int = typer.Option(_DEFAULT_MAX_STEPS, "--max-steps"),
    mcp_config: Optional[str] = typer.Option(
        None, "--mcp-config", help="Path to MCP servers JSON config"
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show side-effect badges, path highlights, and timing"
    ),
    config_file: Optional[str] = typer.Option(None, "--config", help="Path to AgentConfig JSON file"),
    read_only: bool = typer.Option(False, "--read-only", help="Block all write and execute tools"),
    require_approval: bool = typer.Option(
        False, "--require-approval", help="Prompt before any write or execute tool"
    ),
    denied_paths: str = typer.Option(
        "", "--denied-paths", help="Comma-separated glob patterns to block (appended to defaults)"
    ),
    allowed_paths: str = typer.Option(
        "", "--allowed-paths", help="Comma-separated glob patterns to allow (restricts to whitelist)"
    ),
) -> None:
    """Launch the rich TUI interface."""
    from agent.transports.tui import run_tui

    config = _build_config(
        model=model, provider=provider, max_steps=max_steps,
        config_file=config_file, read_only=read_only, require_approval=require_approval,
        denied_paths=denied_paths, allowed_paths=allowed_paths,
    )
    asyncio.run(
        _run_with_mcp(
            lambda agent: run_tui(config, agent=agent, verbose=verbose),
            config,
            mcp_config,
            approval_callback=_terminal_approval_callback,
        )
    )


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Bind address"),
    port: int = typer.Option(8080, "--port", help="Port to listen on"),
    model: str = typer.Option(_DEFAULT_MODEL, "--model", "-m"),
    provider: str = typer.Option(_DEFAULT_PROVIDER, "--provider", "-p"),
    mcp_config: Optional[str] = typer.Option(
        None, "--mcp-config", help="Path to MCP servers JSON config"
    ),
    config_file: Optional[str] = typer.Option(None, "--config", help="Path to AgentConfig JSON file"),
    read_only: bool = typer.Option(False, "--read-only", help="Block all write and execute tools"),
) -> None:
    """Start the web API server (requires uvicorn)."""
    config = _build_config(model=model, provider=provider, config_file=config_file, read_only=read_only)
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
    console.print(f"[dim]POST /chat, POST /chat/stream, GET /sessions, GET /health[/]")
    uvicorn.run(asgi_app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    app()
