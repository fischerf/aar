"""CLI transport — primary entry point for the agent."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from agent.core.agent import Agent
from agent.core.config import AgentConfig, ProviderConfig
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

app = typer.Typer(name="agent", help="Lean Python Agent CLI", no_args_is_help=True)
console = Console()

_DEFAULT_MODEL = "claude-sonnet-4-20250514"
_DEFAULT_PROVIDER = "anthropic"
_DEFAULT_MAX_STEPS = 50


def _build_config(
    model: str = _DEFAULT_MODEL,
    provider: str = _DEFAULT_PROVIDER,
    api_key: str = "",
    max_steps: int = _DEFAULT_MAX_STEPS,
) -> AgentConfig:
    return AgentConfig(
        provider=ProviderConfig(
            name=provider,
            model=model,
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY", ""),
        ),
        max_steps=max_steps,
    )


def _event_handler(event: Event) -> None:
    """Pretty-print events to the console."""
    if isinstance(event, AssistantMessage) and event.content:
        console.print()
        console.print(Markdown(event.content))
    elif isinstance(event, ToolCall):
        console.print(
            f"\n[bold yellow]Tool:[/] {event.tool_name}",
            highlight=False,
        )
        if event.arguments:
            for k, v in event.arguments.items():
                val = str(v)
                if len(val) > 200:
                    val = val[:200] + "..."
                console.print(f"  [dim]{k}:[/] {val}", highlight=False)
    elif isinstance(event, ToolResult):
        style = "red" if event.is_error else "green"
        output = event.output
        if len(output) > 500:
            output = output[:500] + "\n... (truncated)"
        console.print(Panel(output, title=f"Result: {event.tool_name}", border_style=style))
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


def _run_chat_loop(
    session_id: Optional[str] = None,
    model: str = _DEFAULT_MODEL,
    provider: str = _DEFAULT_PROVIDER,
    max_steps: int = _DEFAULT_MAX_STEPS,
) -> None:
    """Shared implementation for chat and resume — accepts plain Python types."""
    config = _build_config(model=model, provider=provider, max_steps=max_steps)
    agent = Agent(config=config)
    agent.on_event(_event_handler)

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
                user_input = console.input("[bold blue]> [/]")
            except EOFError:
                break

            if not user_input.strip():
                continue
            if user_input.strip().lower() in {"/quit", "/exit", "/q"}:
                break

            session = asyncio.run(agent.run(user_input, session))
            store.save(session)

    except KeyboardInterrupt:
        console.print("\n[dim]Goodbye.[/]")

    if session:
        store.save(session)
        console.print(f"[dim]Session saved: {session.session_id}[/]")


@app.command()
def chat(
    model: str = typer.Option(_DEFAULT_MODEL, "--model", "-m", help="Model to use"),
    provider: str = typer.Option(_DEFAULT_PROVIDER, "--provider", "-p", help="Provider name"),
    max_steps: int = typer.Option(_DEFAULT_MAX_STEPS, "--max-steps", help="Maximum agent loop steps"),
    session_id: Optional[str] = typer.Option(None, "--session", "-s", help="Resume a session"),
) -> None:
    """Start an interactive chat session."""
    _run_chat_loop(session_id=session_id, model=model, provider=provider, max_steps=max_steps)


@app.command()
def run(
    task: str = typer.Argument(..., help="Task to execute"),
    model: str = typer.Option("claude-sonnet-4-20250514", "--model", "-m"),
    provider: str = typer.Option("anthropic", "--provider", "-p"),
    max_steps: int = typer.Option(50, "--max-steps"),
) -> None:
    """Run a single task and exit."""
    config = _build_config(model=model, provider=provider, max_steps=max_steps)
    agent = Agent(config=config)
    agent.on_event(_event_handler)

    session = asyncio.run(agent.run(task))
    store = SessionStore(config.session_dir)
    store.save(session)

    console.print(f"\n[dim]Session: {session.session_id}[/]")


@app.command()
def resume(
    session_id: str = typer.Argument(..., help="Session ID to resume"),
) -> None:
    """Resume a saved session."""
    _run_chat_loop(session_id=session_id)


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
def tools() -> None:
    """List available tools."""
    config = _build_config()
    agent = Agent(config=config)
    for spec in agent.registry.list_tools():
        effects = ", ".join(e.value for e in spec.side_effects)
        console.print(f"  [bold]{spec.name}[/]  [dim]({effects})[/]  {spec.description}")


@app.command()
def tui(
    model: str = typer.Option("claude-sonnet-4-20250514", "--model", "-m"),
    provider: str = typer.Option("anthropic", "--provider", "-p"),
    max_steps: int = typer.Option(50, "--max-steps"),
) -> None:
    """Launch the rich TUI interface."""
    from agent.transports.tui import run_tui

    config = _build_config(model=model, provider=provider, max_steps=max_steps)
    asyncio.run(run_tui(config))


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Bind address"),
    port: int = typer.Option(8080, "--port", help="Port to listen on"),
    model: str = typer.Option("claude-sonnet-4-20250514", "--model", "-m"),
    provider: str = typer.Option("anthropic", "--provider", "-p"),
) -> None:
    """Start the web API server (requires uvicorn)."""
    config = _build_config(model=model, provider=provider)
    from agent.transports.web import create_asgi_app

    try:
        import uvicorn
    except ImportError:
        console.print("[red]uvicorn is required for the web server. Install with: pip install uvicorn[/]")
        raise typer.Exit(1)

    asgi_app = create_asgi_app(config)
    console.print(f"[bold green]Starting web server on {host}:{port}[/]")
    console.print(f"[dim]POST /chat, POST /chat/stream, GET /sessions, GET /health[/]")
    uvicorn.run(asgi_app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    app()
