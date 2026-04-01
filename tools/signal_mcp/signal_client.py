"""
signal_client.py — Async wrapper around the signal-cli REST API
https://github.com/bbernhard/signal-cli-rest-api
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, Optional

import httpx

from .logging_config import get_logger

log = get_logger("client")


# ──────────────────────────────────────────────────────────────────────────────
# Data models
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class SignalMessage:
    """A parsed inbound Signal message."""

    sender: str
    sender_name: str
    message: str
    timestamp: datetime
    group_id: Optional[str] = None
    attachments: list[dict] = field(default_factory=list)
    quote: Optional[dict] = None

    @property
    def is_group(self) -> bool:
        return self.group_id is not None

    def __str__(self) -> str:
        ctx = f"[group {self.group_id}]" if self.is_group else "[DM]"
        return f"{ctx} {self.sender_name} ({self.sender}): {self.message}"


@dataclass
class SendResult:
    """Result of a send operation."""

    success: bool
    timestamp: Optional[int] = None
    error: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────────────
# Client
# ──────────────────────────────────────────────────────────────────────────────


class SignalClient:
    """
    Async HTTP client for the signal-cli REST API.

    All public methods raise ``SignalError`` on failure so callers
    can react uniformly.
    """

    def __init__(
        self,
        base_url: str,
        account: str,
        timeout: float = 30.0,
        receive_timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.account = account
        self._timeout = timeout
        self._receive_timeout = receive_timeout
        self._client: Optional[httpx.AsyncClient] = None
        log.debug("SignalClient initialised (base_url=%s account=%s)", base_url, account)

    # ── lifecycle ──────────────────────────────────────────────────────────────

    async def __aenter__(self) -> "SignalClient":
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self._timeout),
            headers={"Content-Type": "application/json"},
        )
        log.info("HTTP session opened → %s", self.base_url)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()
            log.info("HTTP session closed")

    # ── internals ─────────────────────────────────────────────────────────────

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("SignalClient must be used as an async context manager")
        return self._client

    async def _get(self, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        log.debug("GET %s", url)
        try:
            resp = await self._http.get(url, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            raise SignalError(f"HTTP {exc.response.status_code}: {exc.response.text}") from exc
        except httpx.RequestError as exc:
            raise SignalError(f"Request failed: {exc}") from exc

    async def _post(self, path: str, payload: dict, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        log.debug("POST %s  payload=%s", url, payload)
        try:
            resp = await self._http.post(url, content=json.dumps(payload), **kwargs)
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except httpx.HTTPStatusError as exc:
            raise SignalError(f"HTTP {exc.response.status_code}: {exc.response.text}") from exc
        except httpx.RequestError as exc:
            raise SignalError(f"Request failed: {exc}") from exc

    # ── account info ───────────────────────────────────────────────────────────

    async def list_accounts(self) -> list[str]:
        """Return all registered accounts on this signal-cli instance."""
        data = await self._get("/v1/accounts")
        accounts = data if isinstance(data, list) else data.get("accounts", [])
        log.info("Accounts on server: %s", accounts)
        return accounts

    async def account_info(self) -> dict:
        """Fetch profile info for the configured account."""
        return await self._get(f"/v1/accounts/{self.account}")

    # ── registration / onboarding ─────────────────────────────────────────────

    async def register(self, use_voice: bool = False, captcha: Optional[str] = None) -> None:
        """
        Trigger SMS (or voice) verification for a new number.
        Optionally provide a captcha token obtained from https://signalcaptchas.org/registration/generate.html
        """
        payload: dict[str, Any] = {"use_voice": use_voice}
        if captcha:
            payload["captcha"] = captcha
        log.info("Registering %s (voice=%s) …", self.account, use_voice)
        await self._post(f"/v1/register/{self.account}", payload)
        log.info("Verification code dispatched — check your phone.")

    async def verify(self, code: str, pin: Optional[str] = None) -> None:
        """Submit the 6-digit verification code received via SMS/voice."""
        payload: dict[str, Any] = {"token": code.replace("-", "").strip()}
        if pin:
            payload["pin"] = pin
        log.info("Verifying %s …", self.account)
        await self._post(f"/v1/register/{self.account}/verify/{payload['token']}", {})
        log.info("✓ Account %s verified successfully.", self.account)

    async def link_device(self) -> str:
        """
        Generate a device-link URI (for linking as a secondary device).
        Returns the 'tsdevice:/?uuid=…&pub_key=…' URI.
        """
        data = await self._get(f"/v1/devices/link")
        uri: str = data.get("deviceLinkUri", data.get("uri", ""))
        log.info("Link URI generated (encode as QR for the Signal app): %s", uri)
        return uri

    # ── send ──────────────────────────────────────────────────────────────────

    async def send_message(
        self,
        recipients: list[str],
        message: str,
        attachments: Optional[list[str]] = None,
        quote_timestamp: Optional[int] = None,
        quote_author: Optional[str] = None,
        quote_message: Optional[str] = None,
        mention: Optional[list[dict]] = None,
    ) -> SendResult:
        """
        Send a text message to one or more recipients (phone numbers or group IDs).
        """
        payload: dict[str, Any] = {
            "message": message,
            "number": self.account,
            "recipients": recipients,
        }
        if attachments:
            payload["base64_attachments"] = attachments
        if quote_timestamp:
            payload["quote_timestamp"] = quote_timestamp
            payload["quote_author"] = quote_author or self.account
            payload["quote_message"] = quote_message or ""
        if mention:
            payload["mentions"] = mention

        log.info("→ Sending to %s: %r", recipients, message[:80])
        try:
            data = await self._post("/v2/send", payload)
            ts = data.get("timestamp")
            log.info("✓ Message sent (timestamp=%s)", ts)
            return SendResult(success=True, timestamp=ts)
        except SignalError as exc:
            log.error("✗ Send failed: %s", exc)
            return SendResult(success=False, error=str(exc))

    async def send_reaction(
        self,
        recipient: str,
        emoji: str,
        target_author: str,
        target_timestamp: int,
        remove: bool = False,
    ) -> SendResult:
        """React (or un-react) to a specific message."""
        payload = {
            "number": self.account,
            "recipient": recipient,
            "emoji": emoji,
            "target_author": target_author,
            "target_timestamp": target_timestamp,
            "remove": remove,
        }
        log.info("→ Reaction %s on msg %s", emoji, target_timestamp)
        try:
            data = await self._post("/v1/reactions", payload)
            return SendResult(success=True, timestamp=data.get("timestamp"))
        except SignalError as exc:
            log.error("✗ Reaction failed: %s", exc)
            return SendResult(success=False, error=str(exc))

    # ── receive ───────────────────────────────────────────────────────────────

    async def receive_messages(self, timeout: Optional[float] = None) -> list[SignalMessage]:
        """
        Poll the REST API for new messages.
        Returns parsed ``SignalMessage`` objects.
        """
        t = timeout or self._receive_timeout
        log.debug("Polling for messages (timeout=%ss) …", t)
        try:
            raw = await asyncio.wait_for(
                self._get(f"/v1/receive/{self.account}"),
                timeout=t + 2,  # outer safety margin
            )
        except asyncio.TimeoutError:
            log.warning("Receive timed out after %ss", t)
            return []
        except SignalError as exc:
            log.error("Receive error: %s", exc)
            return []

        messages: list[SignalMessage] = []
        for envelope in raw if isinstance(raw, list) else []:
            msg = _parse_envelope(envelope)
            if msg:
                log.info("← %s", msg)
                messages.append(msg)

        log.debug("Received %d message(s)", len(messages))
        return messages

    async def stream_messages(self, poll_interval: float = 5.0) -> AsyncIterator[SignalMessage]:
        """
        Continuously yield incoming messages with ``poll_interval`` seconds
        between polls.  Designed to run inside an asyncio task.
        """
        log.info("Starting message stream (poll_interval=%ss)", poll_interval)
        while True:
            try:
                for msg in await self.receive_messages():
                    yield msg
            except Exception as exc:  # noqa: BLE001
                log.exception("Unexpected error in stream: %s", exc)
            await asyncio.sleep(poll_interval)

    # ── groups ────────────────────────────────────────────────────────────────

    async def list_groups(self) -> list[dict]:
        """Return all groups the account is a member of."""
        return await self._get(f"/v1/groups/{self.account}")

    async def create_group(
        self,
        name: str,
        members: list[str],
        description: str = "",
        avatar: Optional[str] = None,
    ) -> dict:
        """Create a new Signal group."""
        payload: dict[str, Any] = {
            "name": name,
            "members": members,
            "description": description,
        }
        if avatar:
            payload["base64_avatar"] = avatar
        log.info("Creating group %r with %d member(s)", name, len(members))
        return await self._post(f"/v1/groups/{self.account}", payload)

    # ── profiles ──────────────────────────────────────────────────────────────

    async def set_profile(
        self,
        name: str,
        about: str = "",
        emoji: str = "",
        avatar_path: Optional[str] = None,
    ) -> None:
        """Update the account's Signal profile."""
        payload: dict[str, Any] = {"name": name, "about": about, "emoji": emoji}
        if avatar_path:
            payload["avatar"] = avatar_path
        await self._post(f"/v1/profiles/{self.account}", payload)
        log.info("Profile updated → name=%r about=%r", name, about)

    # ── contacts ──────────────────────────────────────────────────────────────

    async def list_contacts(self) -> list[dict]:
        """Fetch address book."""
        return await self._get(f"/v1/contacts/{self.account}")

    async def update_contact(self, number: str, name: str, expiration: int = 0) -> None:
        """Update a contact's local name / disappearing-message timer."""
        payload = {"recipient": number, "name": name, "expiration_in_seconds": expiration}
        await self._post(f"/v1/contacts/{self.account}", payload)
        log.info("Contact %s updated → name=%r", number, name)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _parse_envelope(envelope: dict) -> Optional[SignalMessage]:
    """
    Convert a raw signal-cli REST envelope into a ``SignalMessage``.
    Returns ``None`` for non-data envelopes (receipts, typing events …).

    Handles both the flat format (older signal-cli-rest-api) and the nested
    format used by newer versions where message data lives under an
    ``"envelope"`` key:
        {"envelope": {"source": "+...", "dataMessage": {...}}, "account": "+..."}
    """
    try:
        # Unwrap nested "envelope" key used by newer signal-cli-rest-api versions
        inner: dict = envelope.get("envelope", envelope)
        source = inner.get("source", inner.get("sourceNumber", ""))
        source_name = inner.get("sourceName", source)
        data = inner.get("dataMessage") or inner.get("syncMessage", {}).get("sentMessage", {})
        if not data:
            return None

        body = data.get("message") or data.get("body") or ""
        if not body:
            return None

        ts_ms = data.get("timestamp", inner.get("timestamp", 0))
        ts = (
            datetime.fromtimestamp(ts_ms / 1000)
            if ts_ms > 1_000_000_000_000
            else datetime.fromtimestamp(ts_ms)
        )

        group_info = data.get("groupInfo") or data.get("groupV2")
        group_id = group_info.get("groupId") if group_info else None

        return SignalMessage(
            sender=source,
            sender_name=source_name,
            message=body,
            timestamp=ts,
            group_id=group_id,
            attachments=data.get("attachments", []),
            quote=data.get("quote"),
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("Failed to parse envelope: %s — %s", exc, envelope)
        return None


class SignalError(Exception):
    """Raised when signal-cli REST API returns an error."""
