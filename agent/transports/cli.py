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
    elif restrict_to_cwd is not None and not restrict_to_cwd:
        cfg.safety.allowed_paths = []
    # Expand <cwd> sentinel written by `aar init` (and in sample configs)
    _cwd = str(Path.cwd()).replace("\\", "/")
    cfg.safety.allowed_paths = [p.replace("<cwd>", _cwd) for p in cfg.safety.allowed_paths]

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
    _streaming_state = {"active": False, "thinking": False}

    def _handler(event: Event) -> None:
        if isinstance(event, StreamChunk):
            if event.reasoning_text:
                if not _streaming_state["thinking"]:
                    _streaming_state["thinking"] = True
                    console.print("\n[dim]▸ thinking[/]")
                console.file.write(event.reasoning_text)
                console.file.flush()
            if event.text:
                if _streaming_state["thinking"]:
                    # Separator between thinking and answer
                    console.file.write("\n")
                    console.file.flush()
                    _streaming_state["thinking"] = False
                if not _streaming_state["active"]:
                    _streaming_state["active"] = True
                    console.print()  # blank line before streamed output
                console.file.write(event.text)
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
            console.print("\n[dim]▸ thinking[/]")
            console.print(f"[dim italic]{event.content}[/]")
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
            # Keep extension context in sync with the resumed session so that
            # slash-commands (e.g. /inspect) see the loaded history, not the
            # empty bootstrap snapshot created during Agent.__init__.
            ext_mgr = getattr(agent, "_extension_manager", None)
            if ext_mgr is not None:
                ext_mgr.update_session(session)
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

            stripped = user_input.strip()

            if stripped.lower() in {"/quit", "/exit", "/q"}:
                break

            # --- Extension slash-commands --------------------------------
            if stripped.startswith("/"):
                cmd_name = stripped[1:].split()[0].lower()
                if cmd_name not in {"quit", "exit", "q"}:
                    args_str = stripped[len(cmd_name) + 1 :].strip()
                    ext_mgr = getattr(agent, "_extension_manager", None)
                    if ext_mgr is not None:
                        cmds = ext_mgr.commands
                        if cmd_name in cmds:
                            # Sync session so commands see the latest state
                            if session is not None:
                                ext_mgr.update_session(session)
                            _, handler = cmds[cmd_name]
                            ctx = ext_mgr._context
                            try:
                                result = handler(args_str, ctx)
                                if result is not None:
                                    console.print(str(result))
                            except Exception as exc:
                                console.print(f"[red]Extension command error: {exc}[/]")
                            continue
                    console.print(f"[dim]Unknown command: {stripped}[/]")
                    continue

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
    layers: bool = typer.Option(
        False,
        "--layers",
        help="Show the ordered list of sources that build the prompt instead of the full text.",
    ),
) -> None:
    """Print the fully assembled system prompt so you can see exactly what the agent receives."""
    from agent.core.config import _collect_layers

    config = _build_config(
        model=model,
        provider=provider,
        config_file=config_file,
    )

    if layers:
        sb = config.safety.sandbox
        layer_list = _collect_layers(
            project_rules_dir=config.project_rules_dir,
            sandbox_mode=sb.mode,
            wsl_distro=sb.wsl.distro,
            system_prompt_hint=sb.wsl.system_prompt_hint,
        )
        console.print("\n[bold]System prompt layers[/] (assembled in order):\n")
        for i, layer in enumerate(layer_list, 1):
            tag = f"[cyan]{layer.label}[/]"
            if layer.loaded:
                n = len(layer.text)
                src = f"[green]{layer.source}[/]" if layer.path else f"[dim]{layer.source}[/]"
                console.print(f"  {i:2}. {tag:30}  {src}  [dim]({n} chars)[/]")
            else:
                console.print(f"  {i:2}. {tag:30}  [dim]{layer.source}  (not found — skipped)[/]")
        loaded = sum(1 for lyr in layer_list if lyr.loaded)
        total_chars = sum(len(lyr.text) for lyr in layer_list if lyr.loaded)
        console.print(f"\n  [dim]{loaded} layer(s) loaded · {total_chars} total chars[/]")
        return

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
                "[red]uvicorn is required for --http mode. Install with: pip install uvicorn[/]",
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
def install(
    package: str = typer.Argument(
        ..., help="Package to install (e.g. aar-ext-permission-gate or ./local-ext/)"
    ),
) -> None:
    """Install an Aar extension from PyPI or a local path."""
    import subprocess
    import sys

    console.print(f"[dim]Installing {package}...[/]")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", package],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        console.print(f"[red]pip install failed:[/]\n{result.stderr}")
        raise typer.Exit(1)
    console.print(result.stdout.strip())

    # Validate it declares aar_extensions entry points
    import importlib.metadata

    try:
        eps = importlib.metadata.entry_points(group="aar_extensions")
        # Check if any entry point comes from a dist matching pkg_name
        found = False
        for ep in eps:
            # ep.dist is available in newer Python
            found = True
            break
        if found:
            console.print("[green]✓[/] Extension installed with aar_extensions entry point(s)")
        else:
            console.print(
                "[yellow]Warning:[/] Package installed but no 'aar_extensions' entry points found. "
                "It may not be an Aar extension, or you may need to add entry points to its pyproject.toml."
            )
    except Exception:
        console.print("[yellow]Warning:[/] Could not verify entry points.")


extensions_app = typer.Typer(name="extensions", help="Manage Aar extensions", no_args_is_help=True)
app.add_typer(extensions_app, name="extensions")


@extensions_app.command("list")
def extensions_list(
    user_dir: Optional[str] = typer.Option(
        None, "--user-dir", help="Custom user extensions directory"
    ),
    project_dir: Optional[str] = typer.Option(
        None, "--project-dir", help="Custom project extensions directory"
    ),
) -> None:
    """List all discovered extensions."""
    from agent.extensions.loader import discover_extensions

    infos = discover_extensions(
        user_dir=Path(user_dir) if user_dir else None,
        project_dir=Path(project_dir) if project_dir else None,
    )
    if not infos:
        console.print("[dim]No extensions found.[/]")
        return

    from rich.table import Table

    table = Table(title="Discovered Extensions")
    table.add_column("Name", style="cyan")
    table.add_column("Source", style="green")
    table.add_column("Path")
    for info in infos:
        table.add_row(info.name, info.source, info.path or "—")
    console.print(table)


@extensions_app.command("inspect")
def extensions_inspect(
    name: str = typer.Argument(..., help="Extension name or package to inspect"),
    user_dir: Optional[str] = typer.Option(None, "--user-dir"),
    project_dir: Optional[str] = typer.Option(None, "--project-dir"),
) -> None:
    """Show what events, tools, and commands an extension registers."""
    from agent.extensions.loader import discover_extensions, load_extension

    infos = discover_extensions(
        user_dir=Path(user_dir) if user_dir else None,
        project_dir=Path(project_dir) if project_dir else None,
    )

    info = next((i for i in infos if i.name == name), None)
    if info is None:
        console.print(f"[red]Extension {name!r} not found.[/]")
        raise typer.Exit(1)

    try:
        api = asyncio.run(load_extension(info))
    except Exception as exc:
        console.print(f"[red]Failed to load extension {name!r}: {exc}[/]")
        raise typer.Exit(1)

    console.print(f"\n[bold cyan]{name}[/] ({info.source})")
    if info.path:
        console.print(f"  Path: {info.path}")

    if api._event_handlers:
        console.print("\n[bold]Event hooks:[/]")
        for event_name, handlers in api._event_handlers.items():
            console.print(f"  • {event_name} ({len(handlers)} handler(s))")

    if api._tools:
        console.print("\n[bold]Tools:[/]")
        for spec in api._tools:
            console.print(f"  • {spec.name} — {spec.description}")

    if api._commands:
        console.print("\n[bold]Commands:[/]")
        for cmd_name, (desc, _) in api._commands.items():
            console.print(f"  • /{cmd_name}" + (f" — {desc}" if desc else ""))

    if api._system_prompt_parts:
        console.print(
            f"\n[bold]System prompt additions:[/] {len(api._system_prompt_parts)} part(s)"
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

    # Distro profiles directory and built-in templates
    _USER_DISTROS_DIR = _USER_DIR / "distros"
    _USER_DISTROS_DIR.mkdir(parents=True, exist_ok=True)

    # Theme directory and files
    _USER_THEMES_DIR = _USER_DIR / "themes"
    _USER_THEMES_DIR.mkdir(parents=True, exist_ok=True)

    # Extensions directory and starter file
    _USER_EXTENSIONS_DIR = _USER_DIR / "extensions"
    _USER_EXTENSIONS_DIR.mkdir(parents=True, exist_ok=True)
    _USER_EXTENSION_HELLO = _USER_EXTENSIONS_DIR / "hello.py"
    _HELLO_EXTENSION_CONTENT = '''\
"""hello — starter Aar extension.

Generated by `aar init`. Edit or delete this file freely.
Drop any *.py file into ~/.aar/extensions/ and Aar picks it up automatically.
See ~/.aar/extensions/hello.py and docs/extensions.md for the full API.
"""

from __future__ import annotations

from typing import Any

from agent.extensions.api import ExtensionAPI, ExtensionContext


def register(api: ExtensionAPI) -> None:
    """Wire up event hooks, tools, and slash-commands for this extension."""

    # ── Lifecycle hook ────────────────────────────────────────────────────
    # Called once when a new chat session starts.
    @api.on("session_start")
    def on_start(event: Any, ctx: ExtensionContext) -> None:
        ctx.logger.debug("hello extension: session started")

    # ── Custom tool ───────────────────────────────────────────────────────
    # Uncomment and adapt to expose a new tool to the model.
    #
    # @api.tool(
    #     name="greet",
    #     description="Return a friendly greeting for the given name.",
    #     input_schema={
    #         "type": "object",
    #         "properties": {"name": {"type": "string", "description": "Name to greet"}},
    #         "required": ["name"],
    #     },
    # )
    # def greet(name: str, ctx: ExtensionContext) -> str:
    #     return f"Hello, {name}! 👋"

    # ── Slash-command ─────────────────────────────────────────────────────
    # Type /hello in the TUI or inline chat to trigger this.
    #
    # @api.command("hello", description="Say hello from the extension.")
    # def cmd_hello(args: str, ctx: ExtensionContext) -> None:
    #     ctx.logger.info("Hello from the extension! args=%r", args)

    # ── System prompt append ──────────────────────────────────────────────
    # Uncomment to inject extra context into every session\'s system prompt.
    #
    # api.append_system_prompt(
    #     "You have access to a greeting tool. Use it when the user says hello."
    # )
'''

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

    distro_profile_items = [
        (_USER_DISTROS_DIR / name, data) for name, data in _load_builtin_distro_profiles().items()
    ]

    for path, data in [
        (_USER_CONFIG, default_config),
        (_USER_MCP_CONFIG, default_mcp),
        (_USER_MCP_EXAMPLE, example_mcp),
        (_USER_PRICING_TEMPLATE, _pricing_raw),
        (_USER_THEME_EXAMPLE, example_theme),
        (_USER_THEME_SCHEMA, theme_schema),
        *distro_profile_items,
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

    # Extension starter file (plain Python, not JSON)
    if _USER_EXTENSION_HELLO.is_file() and not force:
        console.print(
            f"[yellow]Warning:[/] {_USER_EXTENSION_HELLO} already exists — skipping (use --force to overwrite)"
        )
        skipped.append(str(_USER_EXTENSION_HELLO))
    else:
        _USER_EXTENSION_HELLO.write_text(_HELLO_EXTENSION_CONTENT, encoding="utf-8")
        created.append(str(_USER_EXTENSION_HELLO))
        console.print(f"[green]Created:[/] {_USER_EXTENSION_HELLO}")

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
        console.print(
            f"  6. (Windows) WSL2 sandbox distro profiles are in [bold]{_USER_DISTROS_DIR}[/]."
            " Set [bold]safety.sandbox.wsl.profile[/] in config.json to point at one, then run"
            " [bold]aar sandbox setup[/]."
        )
        console.print(
            f"  7. Edit or delete the starter extension at [bold]{_USER_EXTENSION_HELLO}[/]."
            "  Any *.py in ~/.aar/extensions/ is auto-loaded. See docs/extensions.md for the full API."
        )
        console.print("  8. Run [bold]aar chat[/] — no flags needed.")
        console.print(
            "  Tip: run [bold]aar prompt --layers[/] to inspect the system prompt sources."
        )
    if skipped:
        console.print("\n[dim]Re-run with --force to overwrite skipped files.[/]")


# ---------------------------------------------------------------------------
# aar sandbox — WSL2 distro sandbox management
# ---------------------------------------------------------------------------

_sandbox_app = typer.Typer(
    name="sandbox",
    help="Manage the Aar WSL2 sandbox distro.",
    no_args_is_help=True,
)
app.add_typer(_sandbox_app, name="sandbox")

_DEFAULT_DISTRO = "aar-sandbox"
_DEFAULT_PACKAGES = "python3,py3-pip"

# ---------------------------------------------------------------------------
# Built-in distro profiles — sourced from config/distros/ in the repo,
# written to ~/.aar/distros/ by `aar init`
# ---------------------------------------------------------------------------
# Installed wheel: config/distros is mapped to agent/data/distros via force-include.
# Dev/editable install: fall back to config/distros/ at the repo root.
_WHEEL_DISTROS_DIR = Path(__file__).parent.parent / "data" / "distros"
_REPO_DISTROS_DIR = Path(__file__).parent.parent.parent / "config" / "distros"
_BUILTIN_DISTROS_DIR = _WHEEL_DISTROS_DIR if _WHEEL_DISTROS_DIR.is_dir() else _REPO_DISTROS_DIR


def _load_builtin_distro_profiles() -> dict[str, dict]:
    """Return {filename: parsed_dict} for all *.json files in config/distros/."""
    import json as _json

    if not _BUILTIN_DISTROS_DIR.is_dir():
        return {}
    return {
        p.name: _json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(_BUILTIN_DISTROS_DIR.glob("*.json"))
    }


def _resolve_install_path(distro: str, install_path: Optional[str]) -> "Path":
    from agent.safety.wsl_manager import default_install_path

    if install_path:
        return Path(install_path)
    return default_install_path(distro)


def _resolve_rootfs_url(rootfs_url: Optional[str]) -> str:
    from agent.safety.wsl_manager import default_rootfs_url

    return rootfs_url or default_rootfs_url()


@_sandbox_app.command("setup")
def sandbox_setup(
    distro: str = typer.Option(_DEFAULT_DISTRO, "--distro", "-d", help="Name for the WSL2 distro"),
    install_path: Optional[str] = typer.Option(
        None,
        "--install-path",
        help=r"Where to store distro data (default: %%LOCALAPPDATA%%\aar\wsl-distros\<distro>)",
    ),
    rootfs_url: Optional[str] = typer.Option(
        None,
        "--rootfs-url",
        help="URL of the rootfs tarball (default: Alpine latest-stable x86_64)",
    ),
    packages: str = typer.Option(
        _DEFAULT_PACKAGES,
        "--packages",
        help="Comma-separated packages to install via apk add (Alpine) or the distro's package manager",
    ),
    force: bool = typer.Option(False, "--force", help="Unregister existing distro and recreate"),
) -> None:
    """Download a rootfs and import it as a dedicated sandbox WSL2 distro.

    All flags are optional — defaults come from ``~/.aar/config.json``
    (``safety.sandbox.wsl.*`` fields).  Use flags only to override for a
    one-off setup.

    After setup, add this to ``~/.aar/config.json``:

    \\b
        "safety": {
          "sandbox": { "mode": "wsl", "wsl": { "distro": "<distro>" } }
        }
    """
    import os
    import tempfile

    from agent.safety import wsl_manager as wm

    if os.name != "nt":
        console.print("[red]Error:[/] WSL2 sandbox setup is only supported on Windows.")
        raise typer.Exit(1)

    if not wm.is_wsl_available():
        console.print(
            "[red]Error:[/] WSL2 is not available or not enabled on this system.\n"
            "Enable it with: [bold]wsl --install[/]"
        )
        raise typer.Exit(1)

    # Load config so flags can fall back to config values
    cfg = _build_config()
    wsl_cfg = cfg.safety.sandbox.wsl

    # Resolve final values (CLI flags override config/profile)
    if distro == _DEFAULT_DISTRO and wsl_cfg.distro != _DEFAULT_DISTRO:
        distro = wsl_cfg.distro
    resolved_url = rootfs_url or wsl_cfg.rootfs_url
    if packages == _DEFAULT_PACKAGES and wsl_cfg.packages:
        packages = ",".join(wsl_cfg.packages)

    resolved_install = _resolve_install_path(distro, install_path or wsl_cfg.install_path)

    # Handle existing distro
    if wm.distro_exists(distro):
        if not force:
            console.print(
                f"[yellow]Distro '[bold]{distro}[/]' already exists.[/]\n"
                "Use [bold]--force[/] to unregister and recreate, or "
                "[bold]aar sandbox status[/] to inspect it."
            )
            raise typer.Exit(1)
        console.print(f"[yellow]Unregistering existing distro '{distro}'…[/]")
        wm.unregister_distro(distro)

    # Download rootfs
    console.print(f"Downloading rootfs from:\n  [dim]{resolved_url}[/]")
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        _last: list[int] = [0]

        def _progress(downloaded: int, total: int) -> None:
            if total > 0:
                pct = int(downloaded * 100 / total)
                if pct != _last[0] and pct % 10 == 0:
                    console.print(f"  [dim]{pct}%[/]", end="\r")
                    _last[0] = pct

        wm.download_rootfs(resolved_url, tmp_path, progress_cb=_progress)
        console.print(f"  [green]Downloaded[/] → {tmp_path.stat().st_size // 1024} KB")

        # Import distro
        console.print(f"Importing distro '[bold]{distro}[/]' to {resolved_install} …")
        wm.import_distro(distro, resolved_install, tmp_path)
        console.print("  [green]Imported[/]")

    finally:
        tmp_path.unlink(missing_ok=True)

    # Run pre-install commands from config (e.g. enabling extra package repos).
    for i, cmd in enumerate(wsl_cfg.pre_install_commands, 1):
        console.print(f"Pre-install [{i}/{len(wsl_cfg.pre_install_commands)}]: [dim]{cmd}[/]")
        stdout, stderr, rc = wm.run_in_distro(distro, cmd, timeout=60)
        if stdout.strip():
            console.print(f"[dim]{stdout.strip()[:600]}[/]")
        if rc != 0:
            console.print(f"[yellow]Warning:[/] pre-install command returned exit code {rc}.")
            if stderr.strip():
                console.print(f"[red]{stderr.strip()[:600]}[/]")
        else:
            console.print("  [green]OK[/]")

    # Install packages
    pkg_list = [p.strip() for p in packages.split(",") if p.strip()]
    if pkg_list:
        console.print(f"Installing packages: [bold]{', '.join(pkg_list)}[/]")
        install_cmd = wsl_cfg.package_install_command.format(packages=" ".join(pkg_list))
        stdout, stderr, rc = wm.run_in_distro(distro, f"{install_cmd} 2>&1", timeout=600)
        if stdout.strip():
            console.print(f"[dim]{stdout.strip()[:1200]}[/]")
        if rc != 0:
            console.print(f"[yellow]Warning:[/] Package install returned exit code {rc}.")
            if stderr.strip():
                console.print(f"[red]{stderr.strip()[:600]}[/]")
        else:
            console.print("  [green]Packages installed[/]")

    # Success — print config snippet
    console.print(
        f"\n[bold green]✓ Sandbox '{distro}' is ready.[/]\n\n"
        "Add this to [bold]~/.aar/config.json[/] (inside the top-level object):\n"
    )
    console.print(
        f'  "safety": {{\n    "sandbox": {{"mode": "wsl", "wsl": {{"distro": "{distro}"}}}}\n  }}'
    )
    console.print(f"\nThen run [bold]aar sandbox status --distro {distro}[/] to verify.")


@_sandbox_app.command("status")
def sandbox_status(
    distro: str = typer.Option(_DEFAULT_DISTRO, "--distro", "-d", help="Distro name to inspect"),
) -> None:
    """Show status of the WSL2 sandbox distro."""
    import os

    from agent.safety import wsl_manager as wm

    if os.name != "nt":
        console.print("[yellow]WSL2 sandbox is Windows-only.[/]")
        raise typer.Exit(0)

    cfg = _build_config()
    wsl_cfg = cfg.safety.sandbox.wsl
    if distro == _DEFAULT_DISTRO:
        distro = wsl_cfg.distro

    resolved_install = _resolve_install_path(distro, wsl_cfg.install_path)
    workspace = wsl_cfg.workspace or "[dim]cwd at runtime[/]"

    # Configuration summary
    console.print("\n[bold]Sandbox configuration[/]")
    console.print(f"  Profile:           {wsl_cfg.profile or '[dim]none[/]'}")
    console.print(f"  Distro:            [cyan]{distro}[/]")
    console.print(f"  Shell:             {wsl_cfg.shell}")
    console.print(f"  Workspace:         {workspace}")
    console.print(f"  Install path:      {resolved_install}")
    console.print(f"  Rootfs URL:        [dim]{wsl_cfg.rootfs_url}[/]")
    pre_cmds = wsl_cfg.pre_install_commands
    if pre_cmds:
        console.print(f"  Pre-install cmds:  {len(pre_cmds)} command(s)")
        for i, cmd in enumerate(pre_cmds, 1):
            console.print(f"    [{i}] [dim]{cmd}[/]")
    else:
        console.print("  Pre-install cmds:  [dim]none[/]")
    pkg_display = ", ".join(wsl_cfg.packages) if wsl_cfg.packages else "[dim]none[/]"
    console.print(f"  Packages:          {pkg_display}")
    console.print(f"  Install command:   [dim]{wsl_cfg.package_install_command}[/]")
    hint = wsl_cfg.system_prompt_hint
    if hint:
        console.print(f"  Prompt hint:       [dim]{hint[:80]}{'…' if len(hint) > 80 else ''}[/]")
    console.print()

    # Live distro status
    console.print(f"[bold]WSL2 distro status[/] — [cyan]{distro}[/]\n")

    wsl_ok = wm.is_wsl_available()
    console.print(f"  WSL2 available : {'[green]yes[/]' if wsl_ok else '[red]no[/]'}")

    if not wsl_ok:
        raise typer.Exit(1)

    exists = wm.distro_exists(distro)
    console.print(
        f"  Distro exists  : {'[green]yes[/]' if exists else '[red]no — run aar sandbox setup[/]'}"
    )

    if exists:
        stdout, _, rc = wm.run_in_distro(
            distro, "uname -r && python3 --version 2>/dev/null || echo 'python3: not installed'"
        )
        for line in stdout.strip().splitlines():
            console.print(f"  [dim]{line}[/]")


@_sandbox_app.command("reset")
def sandbox_reset(
    distro: str = typer.Option(_DEFAULT_DISTRO, "--distro", "-d", help="Distro name to reset"),
    rootfs_url: Optional[str] = typer.Option(None, "--rootfs-url"),
    packages: str = typer.Option(_DEFAULT_PACKAGES, "--packages"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Unregister the sandbox distro and recreate it from scratch.

    All installed packages and state inside the distro are wiped.
    Workspace files (on the Windows filesystem) are not affected.
    """
    import os

    from agent.safety import wsl_manager as wm

    if os.name != "nt":
        console.print("[red]Error:[/] WSL2 sandbox reset is only supported on Windows.")
        raise typer.Exit(1)

    cfg = _build_config()
    if distro == _DEFAULT_DISTRO:
        distro = cfg.safety.sandbox.wsl.distro

    if not wm.distro_exists(distro):
        console.print(f"[yellow]Distro '{distro}' does not exist — nothing to reset.[/]")
        raise typer.Exit(0)

    if not yes:
        confirm = console.input(
            f"[bold red]This will permanently delete distro '{distro}' and all its contents.[/]\n"
            "Type the distro name to confirm: "
        )
        if confirm.strip() != distro:
            console.print("[dim]Aborted.[/]")
            raise typer.Exit(0)

    console.print(f"[yellow]Unregistering '{distro}'…[/]")
    wm.unregister_distro(distro)
    console.print("  [green]Unregistered[/]")

    # Re-run setup with same options
    sandbox_setup(
        distro=distro,
        install_path=None,
        rootfs_url=rootfs_url,
        packages=packages,
        force=False,
    )


if __name__ == "__main__":
    app()
