"""
auth.py — Interactive onboarding wizard for Signal MCP
"""
from __future__ import annotations

import asyncio
import re
import sys
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.rule import Rule
from rich.text import Text

from .config import SignalConfig, load_config, save_config
from .logging_config import get_logger
from .signal_client import SignalClient, SignalError

log = get_logger("auth")
console = Console()

E164_RE = re.compile(r"^\+\d{7,15}$")


# ──────────────────────────────────────────────────────────────────────────────
# Public entry-point
# ──────────────────────────────────────────────────────────────────────────────

async def run_onboarding(cfg: Optional[SignalConfig] = None) -> SignalConfig:
    """
    Full interactive onboarding flow.  Returns a ready-to-use config.
    Steps:
        1. Confirm / set API URL
        2. Choose registration mode (new number | link device)
        3. Verify / complete registration
        4. Persist config
    """
    cfg = cfg or load_config()

    console.print(
        Panel.fit(
            "[bold cyan]Signal MCP — Onboarding Wizard[/bold cyan]\n"
            "[dim]Connects to a running [link=https://github.com/bbernhard/signal-cli-rest-api]"
            "signal-cli-rest-api[/link] instance[/dim]",
            border_style="cyan",
        )
    )

    # ── Step 1: API endpoint ──────────────────────────────────────────────────
    console.print(Rule("[bold]Step 1/3 — API Connection[/bold]"))
    api_url = Prompt.ask(
        "  signal-cli REST API URL",
        default=cfg.api_base_url,
    )
    cfg.api_base_url = api_url.rstrip("/")

    # Verify connectivity
    async with SignalClient(cfg.api_base_url, "__probe__") as client:
        try:
            accounts = await asyncio.wait_for(client.list_accounts(), timeout=8.0)
            console.print(f"  [green]✓[/green] Connected — {len(accounts)} account(s) registered")
            if accounts:
                console.print(f"    Existing accounts: {', '.join(accounts)}")
        except (SignalError, asyncio.TimeoutError, Exception) as exc:
            console.print(f"  [yellow]⚠[/yellow]  Could not reach API ({exc}).")
            if not Confirm.ask("  Continue anyway?", default=False):
                console.print("[red]Aborted.[/red]")
                sys.exit(1)

    # ── Step 2: Phone number ──────────────────────────────────────────────────
    console.print(Rule("[bold]Step 2/3 — Phone Number[/bold]"))
    while True:
        number = Prompt.ask(
            "  Phone number [dim](E.164 format, e.g. +15551234567)[/dim]",
            default=cfg.account_number or "",
        )
        if E164_RE.match(number):
            cfg.account_number = number
            break
        console.print("  [red]Invalid format.[/red] Use E.164: +<country_code><number>")

    # ── Step 3: Registration mode ─────────────────────────────────────────────
    console.print(Rule("[bold]Step 3/3 — Registration[/bold]"))
    console.print(
        "  [bold]How do you want to authenticate?[/bold]\n"
        "  [1] Register as new primary device (SMS / voice verification)\n"
        "  [2] Link as secondary device (scan QR with existing Signal app)\n"
        "  [3] Skip — already registered, just save config\n"
    )
    mode = Prompt.ask("  Choice", choices=["1", "2", "3"], default="3")

    async with SignalClient(cfg.api_base_url, cfg.account_number) as client:
        if mode == "1":
            await _register_new_device(client, cfg)
        elif mode == "2":
            await _link_secondary_device(client, cfg)
        else:
            console.print("  Skipping registration — config will be saved as-is.")

    # ── Persist ───────────────────────────────────────────────────────────────
    save_config(cfg)
    console.print(
        Panel.fit(
            f"[bold green]✓ Configuration saved[/bold green]\n"
            f"  Account : [cyan]{cfg.account_number}[/cyan]\n"
            f"  API URL : [cyan]{cfg.api_base_url}[/cyan]",
            border_style="green",
        )
    )
    log.info("Onboarding complete for %s", cfg.account_number)
    return cfg


# ──────────────────────────────────────────────────────────────────────────────
# Registration helpers
# ──────────────────────────────────────────────────────────────────────────────

async def _register_new_device(client: SignalClient, cfg: SignalConfig) -> None:
    """Register phone number via SMS or voice OTP."""
    use_voice = Confirm.ask("  Use voice call instead of SMS?", default=False)
    captcha: Optional[str] = None

    console.print(
        "  [dim]If registration is blocked you may need a captcha token.[/dim]\n"
        "  [dim]Get one at: https://signalcaptchas.org/registration/generate.html[/dim]"
    )
    if Confirm.ask("  Enter captcha token?", default=False):
        captcha = Prompt.ask("  Captcha token")

    console.print(f"  Requesting verification code for [cyan]{cfg.account_number}[/cyan] …")
    try:
        await client.register(use_voice=use_voice, captcha=captcha)
    except SignalError as exc:
        console.print(f"  [red]Registration request failed:[/red] {exc}")
        raise

    console.print("  [green]✓[/green] Verification code sent — check your phone.")

    # Verification loop
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        code = Prompt.ask(f"  Enter verification code (attempt {attempt}/{max_attempts})")
        pin: Optional[str] = None
        if Confirm.ask("  Account has a Signal PIN?", default=False):
            pin = Prompt.ask("  PIN", password=True)
        try:
            await client.verify(code, pin=pin)
            console.print(f"  [green]✓[/green] Verified [cyan]{cfg.account_number}[/cyan]")
            return
        except SignalError as exc:
            console.print(f"  [red]Verification failed:[/red] {exc}")
            if attempt == max_attempts:
                console.print("  [red]Max attempts reached.  Registration failed.[/red]")
                raise


async def _link_secondary_device(client: SignalClient, cfg: SignalConfig) -> None:
    """Generate a link URI to scan with an existing Signal app."""
    console.print("  Generating device-link URI …")
    try:
        uri = await client.link_device()
    except SignalError as exc:
        console.print(f"  [red]Failed to generate link URI:[/red] {exc}")
        raise

    console.print(
        Panel(
            Text(uri, style="bold yellow", overflow="fold"),
            title="[bold]Device Link URI[/bold]",
            subtitle="[dim]Encode as QR code and scan with your Signal app[/dim]",
            border_style="yellow",
        )
    )
    console.print(
        "  [dim]Tip: use [bold]qrencode -t UTF8 '<uri>'[/bold] "
        "or paste into https://qr.io to generate a QR code.[/dim]"
    )
    Prompt.ask("  Press [Enter] once you've scanned the QR code in your Signal app")
    console.print("  [green]✓[/green] Device linked (verify in your Signal app).")
