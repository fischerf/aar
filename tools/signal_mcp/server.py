"""
server.py — Signal MCP Server (FastMCP edition)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Exposes Signal messaging as MCP tools backed by signal-cli REST API.

Run:
    python -m signal_mcp                     # stdio transport (Claude Desktop)
    python -m signal_mcp --transport sse     # SSE transport (remote / browser)

Tools are discovered automatically by any MCP client via tools/list.
The SignalClient HTTP session is opened eagerly during server lifespan so the
first tool call has no cold-start delay.
"""
from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncIterator, Optional

from mcp.server.fastmcp import FastMCP, Context

from .config import SignalConfig, load_config
from .logging_config import get_logger, setup_logging
from .signal_client import SignalClient, SignalError, SignalMessage

log = get_logger("server")


# ──────────────────────────────────────────────────────────────────────────────
# Lifespan — eager connection: HTTP session opens before the first tool call
# ──────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """
    Open the SignalClient HTTP session at startup and expose it to every
    tool via the FastMCP lifespan context dict.
    """
    cfg: SignalConfig = load_config()
    setup_logging(cfg.log_level, cfg.log_file)

    if not cfg.account_number:
        log.error(
            "No account configured. "
            "Run `python -m signal_mcp onboard` first, "
            "or set SIGNAL_PHONE_NUMBER environment variable."
        )
        raise SystemExit(1)

    log.info(
        "Starting Signal MCP server — account=%s  api=%s",
        cfg.account_number,
        cfg.api_base_url,
    )

    async with SignalClient(
        base_url=cfg.api_base_url,
        account=cfg.account_number,
        timeout=cfg.api_timeout,
        receive_timeout=cfg.receive_timeout,
    ) as client:
        log.info("Signal MCP server ready (eager connection established).")
        yield {"client": client, "cfg": cfg}

    log.info("Signal MCP server shut down.")


# ──────────────────────────────────────────────────────────────────────────────
# Server instance
# ──────────────────────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="signal-mcp",
    instructions=(
        "Send and receive Signal messages, manage groups, contacts, and your profile. "
        "Phone numbers must be in E.164 format (e.g. +15551234567). "
        "Group IDs are base64 strings returned by signal_list_groups."
    ),
    lifespan=_lifespan,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _client(ctx: Context) -> SignalClient:
    """Extract the shared SignalClient from the lifespan context."""
    return ctx.request_context.lifespan_context["client"]


def _cfg(ctx: Context) -> SignalConfig:
    """Extract the SignalConfig from the lifespan context."""
    return ctx.request_context.lifespan_context["cfg"]


def _fmt_message(m: SignalMessage) -> dict[str, Any]:
    """Serialise a SignalMessage to a plain dict for JSON output."""
    return {
        "sender":      m.sender,
        "sender_name": m.sender_name,
        "message":     m.message,
        "timestamp":   m.timestamp.isoformat(),
        "group_id":    m.group_id,
        "is_group":    m.is_group,
        "attachments": m.attachments,
        "has_quote":   m.quote is not None,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Tools — Messaging
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def signal_send_message(
    ctx: Context,
    recipients: Annotated[
        list[str],
        "One or more phone numbers (E.164) or base64 group IDs to send to.",
    ],
    message: Annotated[str, "Text body of the message."],
    attachments: Annotated[
        Optional[list[str]],
        "File paths or base64 data URIs to attach (optional).",
    ] = None,
) -> str:
    """Send a Signal message to one or more recipients or groups."""
    cfg = _cfg(ctx)
    result = await asyncio.wait_for(
        _client(ctx).send_message(
            recipients=recipients,
            message=message,
            attachments=attachments,
        ),
        timeout=cfg.api_timeout,
    )
    if result.success:
        return f"Message sent (timestamp={result.timestamp})."
    raise SignalError(result.error or "Unknown send error")


@mcp.tool()
async def signal_react(
    ctx: Context,
    recipient: Annotated[str, "Phone number (E.164) or group ID of the conversation."],
    emoji: Annotated[str, "Reaction emoji, e.g. 👍."],
    target_author: Annotated[str, "Phone number of the author of the message to react to."],
    target_timestamp: Annotated[int, "Unix-millisecond timestamp of the target message."],
    remove: Annotated[bool, "Set True to remove an existing reaction."] = False,
) -> str:
    """Add or remove an emoji reaction on a specific Signal message."""
    cfg = _cfg(ctx)
    result = await asyncio.wait_for(
        _client(ctx).send_reaction(
            recipient=recipient,
            emoji=emoji,
            target_author=target_author,
            target_timestamp=target_timestamp,
            remove=remove,
        ),
        timeout=cfg.api_timeout,
    )
    if result.success:
        action = "removed" if remove else "added"
        return f"Reaction {emoji} {action} (timestamp={result.timestamp})."
    raise SignalError(result.error or "Unknown reaction error")


@mcp.tool()
async def signal_receive_messages(
    ctx: Context,
    timeout: Annotated[
        Optional[float],
        "Seconds to wait for messages. Defaults to the server receive_timeout setting.",
    ] = None,
    limit: Annotated[
        int,
        "Maximum number of messages to return. 0 means return all.",
    ] = 0,
) -> str:
    """
    Poll Signal for new incoming messages.
    Returns a JSON array of message objects, each containing sender, message
    text, timestamp, group information, and attachment metadata.
    """
    cfg = _cfg(ctx)
    t = timeout if timeout is not None else cfg.receive_timeout
    messages = await asyncio.wait_for(
        _client(ctx).receive_messages(timeout=t),
        timeout=t + 5,  # outer safety margin
    )
    if limit > 0:
        messages = messages[:limit]
    if not messages:
        return "No new messages."
    return json.dumps([_fmt_message(m) for m in messages], indent=2, default=str)


# ──────────────────────────────────────────────────────────────────────────────
# Tools — Groups
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def signal_list_groups(ctx: Context) -> str:
    """List all Signal groups the configured account belongs to."""
    cfg = _cfg(ctx)
    groups = await asyncio.wait_for(
        _client(ctx).list_groups(),
        timeout=cfg.api_timeout,
    )
    if not groups:
        return "No groups found."
    return json.dumps(groups, indent=2, default=str)


@mcp.tool()
async def signal_create_group(
    ctx: Context,
    name: Annotated[str, "Display name for the new group."],
    members: Annotated[
        list[str],
        "Phone numbers (E.164) of the initial group members.",
    ],
    description: Annotated[str, "Optional group description."] = "",
) -> str:
    """Create a new Signal group and return its ID and invite link."""
    cfg = _cfg(ctx)
    result = await asyncio.wait_for(
        _client(ctx).create_group(
            name=name,
            members=members,
            description=description,
        ),
        timeout=cfg.api_timeout,
    )
    return json.dumps(result, indent=2, default=str)


# ──────────────────────────────────────────────────────────────────────────────
# Tools — Contacts
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def signal_list_contacts(ctx: Context) -> str:
    """Return the Signal address book for the configured account."""
    cfg = _cfg(ctx)
    contacts = await asyncio.wait_for(
        _client(ctx).list_contacts(),
        timeout=cfg.api_timeout,
    )
    if not contacts:
        return "No contacts found."
    return json.dumps(contacts, indent=2, default=str)


@mcp.tool()
async def signal_update_contact(
    ctx: Context,
    number: Annotated[str, "Contact phone number in E.164 format."],
    name: Annotated[str, "New local display name for the contact."],
    expiration_seconds: Annotated[
        int,
        "Disappearing-message timer in seconds. 0 disables the timer.",
    ] = 0,
) -> str:
    """Update a contact's local display name and disappearing-message timer."""
    cfg = _cfg(ctx)
    await asyncio.wait_for(
        _client(ctx).update_contact(
            number=number,
            name=name,
            expiration=expiration_seconds,
        ),
        timeout=cfg.api_timeout,
    )
    timer_info = f", timer={expiration_seconds}s" if expiration_seconds else ""
    return f"Contact {number} updated → name={name!r}{timer_info}."


# ──────────────────────────────────────────────────────────────────────────────
# Tools — Profile
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def signal_set_profile(
    ctx: Context,
    name: Annotated[str, "Profile display name."],
    about: Annotated[str, "About / bio text (optional)."] = "",
    emoji: Annotated[str, "Profile emoji (optional)."] = "",
) -> str:
    """Update the Signal profile name, bio, and emoji for the configured account."""
    cfg = _cfg(ctx)
    await asyncio.wait_for(
        _client(ctx).set_profile(name=name, about=about, emoji=emoji),
        timeout=cfg.api_timeout,
    )
    return f"Profile updated → name={name!r} about={about!r} emoji={emoji!r}."


# ──────────────────────────────────────────────────────────────────────────────
# Tools — Account
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def signal_account_info(ctx: Context) -> str:
    """
    Return registration and profile information for the configured Signal account.
    Useful to confirm which number is active and check registration status.
    """
    cfg = _cfg(ctx)
    info = await asyncio.wait_for(
        _client(ctx).account_info(),
        timeout=cfg.api_timeout,
    )
    return json.dumps(info, indent=2, default=str)


@mcp.tool()
async def signal_list_accounts(ctx: Context) -> str:
    """
    List all phone numbers registered on the signal-cli REST API instance.
    Helpful when the server manages multiple accounts.
    """
    cfg = _cfg(ctx)
    accounts = await asyncio.wait_for(
        _client(ctx).list_accounts(),
        timeout=cfg.api_timeout,
    )
    if not accounts:
        return "No accounts registered on this signal-cli instance."
    return "\n".join(accounts)


# ──────────────────────────────────────────────────────────────────────────────
# Resources — raw JSON for programmatic / agentic consumers
# ──────────────────────────────────────────────────────────────────────────────

@mcp.resource("signal://messages/inbox")
async def resource_inbox() -> str:
    """Raw JSON snapshot of the current message inbox (one poll)."""
    cfg = load_config()
    async with SignalClient(
        base_url=cfg.api_base_url,
        account=cfg.account_number or "",
        timeout=cfg.api_timeout,
        receive_timeout=cfg.receive_timeout,
    ) as client:
        messages = await client.receive_messages()
    return json.dumps([_fmt_message(m) for m in messages], indent=2, default=str)


@mcp.resource("signal://groups/list")
async def resource_groups() -> str:
    """Raw JSON list of all groups the account belongs to."""
    cfg = load_config()
    async with SignalClient(
        base_url=cfg.api_base_url,
        account=cfg.account_number or "",
        timeout=cfg.api_timeout,
    ) as client:
        groups = await client.list_groups()
    return json.dumps(groups, indent=2, default=str)


@mcp.resource("signal://contacts/list")
async def resource_contacts() -> str:
    """Raw JSON address book for the configured Signal account."""
    cfg = load_config()
    async with SignalClient(
        base_url=cfg.api_base_url,
        account=cfg.account_number or "",
        timeout=cfg.api_timeout,
    ) as client:
        contacts = await client.list_contacts()
    return json.dumps(contacts, indent=2, default=str)


# ──────────────────────────────────────────────────────────────────────────────
# Entry-point  (called by __main__.py's `serve` command)
# ──────────────────────────────────────────────────────────────────────────────

async def serve(cfg: Optional[SignalConfig] = None) -> None:
    """
    Async entry-point kept for backward compatibility with __main__.py.

    Config validation is handled inside _lifespan; the optional `cfg`
    argument is accepted but ignored — lifespan always loads from file/env
    so that the config is re-read fresh on every server start.
    """
    # Delegate entirely to FastMCP; lifespan handles all setup.
    transport = _pick_transport()
    mcp.run(transport=transport)


def _pick_transport() -> str:
    """Return 'sse' if --transport sse is in argv, else 'stdio'."""
    if "--transport" in sys.argv:
        idx = sys.argv.index("--transport")
        if idx + 1 < len(sys.argv):
            return sys.argv[idx + 1]
    return "stdio"


# ──────────────────────────────────────────────────────────────────────────────
# Direct invocation  (python server.py [--transport sse])
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport=_pick_transport())
