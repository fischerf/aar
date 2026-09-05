"""Shared authentication / CORS / safety-override hardening for the HTTP transports.

C1 — Both ``aar serve`` and ``aar acp --http`` used to be completely open:
no authentication, ``Access-Control-Allow-Origin: *``, every tool call
auto-approved, and a request body that could rewrite the safety policy.  Any
web page the user visited could ``fetch("http://127.0.0.1:8080/chat", …)`` and
get arbitrary command execution — binding to loopback is no defence because the
victim's browser *is* on loopback.

This module holds the pieces both transports share so the two apps can't drift.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import secrets
from collections.abc import Iterable, Sequence
from typing import Any

from agent.core.config import SafetyConfig

logger = logging.getLogger(__name__)

#: Environment variable read when no explicit token is supplied.
TOKEN_ENV_VAR = "AAR_HTTP_TOKEN"

#: Paths served without authentication (liveness probes only — no data).
PUBLIC_PATHS = frozenset({"/health", "/ping"})


class BearerAuth:
    """``Authorization: Bearer <token>`` check for a raw ASGI scope.

    With no token supplied (and none in ``$AAR_HTTP_TOKEN``) a random one is
    generated: the server still starts, but only a caller who was told the
    token — printed once at startup — can reach it.  A drive-by page cannot
    guess it, and cannot read it cross-origin either (see :func:`cors_headers`).
    """

    __slots__ = ("token", "generated", "enabled")

    def __init__(
        self,
        token: str | None = None,
        *,
        enabled: bool = True,
        env_var: str | None = TOKEN_ENV_VAR,
    ) -> None:
        env_token = os.environ.get(env_var) if env_var else None
        supplied = token or env_token or None
        self.enabled = enabled
        self.token = supplied or secrets.token_urlsafe(32)
        self.generated = supplied is None
        if not enabled:
            logger.warning(
                "HTTP authentication is DISABLED. Any local process — including a web page "
                "the user visits — can drive this agent. Only do this behind your own auth."
            )

    @classmethod
    def disabled(cls) -> BearerAuth:
        """Explicit opt-out, for embedding behind an existing auth layer."""
        return cls(token="", enabled=False)

    def check(self, scope: dict) -> bool:
        """Return True when the request carries the right bearer token."""
        if not self.enabled:
            return True
        header = b""
        for key, value in scope.get("headers") or []:
            if key.lower() == b"authorization":
                header = value
                break
        if not header.startswith(b"Bearer "):
            return False
        return hmac.compare_digest(header[len(b"Bearer ") :], self.token.encode())


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


def normalize_origins(origins: Sequence[str] | None) -> set[bytes]:
    """Normalise configured origins to the byte form seen in ASGI headers."""
    return {o.strip().rstrip("/").encode() for o in (origins or []) if o.strip()}


def cors_headers(scope: dict, allowed: Iterable[bytes]) -> list[list[bytes]]:
    """Return CORS headers for this request — empty unless the origin matches.

    C1 — The old ``Access-Control-Allow-Origin: *`` let every page on the
    internet read responses from the local agent.  Now the default allow-list
    is empty, so no CORS headers are emitted at all and the browser refuses
    cross-origin reads; operators opt specific origins in explicitly.
    """
    allowed_set = set(allowed)
    if not allowed_set:
        return []
    origin = b""
    for key, value in scope.get("headers") or []:
        if key.lower() == b"origin":
            origin = value.strip().rstrip(b"/")
            break
    if not origin or origin not in allowed_set:
        return []
    return [
        [b"access-control-allow-origin", origin],
        [b"access-control-allow-methods", b"GET, POST, OPTIONS"],
        [b"access-control-allow-headers", b"content-type, authorization"],
        [b"access-control-allow-credentials", b"true"],
        [b"vary", b"origin"],
    ]


# ---------------------------------------------------------------------------
# Client-supplied safety overrides
# ---------------------------------------------------------------------------

# Boolean fields a client may switch ON (never off) — each one only restricts.
_TIGHTEN_ONLY_FLAGS = frozenset(
    {"read_only", "require_approval_for_writes", "require_approval_for_execute"}
)

# List fields a client may *extend* — entries are appended, never replaced.
_EXTEND_ONLY_LISTS = ("denied_paths", "denied_commands")


def apply_safety_override(
    base: SafetyConfig,
    override: dict | None,
    permissive: bool = False,
) -> SafetyConfig:
    """Merge a client-supplied safety override into *base*.

    C1 — The old code did an unconditional ``base.model_copy(update=override)``,
    so a request body could send ``{"allowed_paths": [], "denied_paths": [],
    "denied_commands": [], "sandbox": {"mode": "local"}}`` and delete the
    server's entire policy for that request.  A client may now only *tighten*:
    turn the approval/read-only flags on, and append to the deny-lists.
    Everything else is ignored and logged.  ``permissive=True``
    (``--allow-safety-override``) restores the old behaviour for deployments
    that genuinely need it.
    """
    if not override:
        return base
    if permissive:
        logger.warning(
            "Applying unrestricted client safety override (--allow-safety-override): %s",
            sorted(override),
        )
        return base.model_copy(update=override)

    update: dict[str, Any] = {}
    for key in _TIGHTEN_ONLY_FLAGS:
        if override.get(key) is True:
            update[key] = True
    for key in _EXTEND_ONLY_LISTS:
        value = override.get(key)
        if isinstance(value, list):
            update[key] = _current_list(base, key) + [str(item) for item in value]

    dropped = set(override) - set(update)
    if dropped:
        logger.warning(
            "Ignoring non-tightening safety override keys: %s",
            sorted(dropped),
        )
    if not update:
        return base
    return base.model_copy(update=update)


# ---------------------------------------------------------------------------
# Bind-address policy
# ---------------------------------------------------------------------------


def _current_list(base: SafetyConfig, key: str) -> list[str]:
    """Effective value of a deny-list field, resolving ``None`` to the defaults.

    ``SafetyConfig.denied_commands`` defaults to ``None`` meaning "use the
    policy engine's built-in list"; appending to it must not silently drop
    those built-ins.
    """
    current = getattr(base, key, None)
    if current is not None:
        return list(current)
    from agent.safety.policy import PolicyConfig

    field = PolicyConfig.model_fields.get(key)
    if field is not None and field.default_factory is not None:
        return list(field.default_factory())  # type: ignore[call-arg]
    return []


def is_loopback(host: str) -> bool:
    """True when *host* only accepts connections from this machine."""
    candidate = (host or "").strip().strip("[]")
    if candidate in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False
