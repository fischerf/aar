"""Provider error taxonomy — common types raised by every provider adapter.

#4 — Pre-Wave-4 the loop classified provider exceptions via substring
matching on ``type(exc).__name__`` in two separate spots in
``agent/core/provider_runner.py``. That worked for retry/log decisions but
made it impossible for any other consumer (extensions, custom transports,
tests) to ask "was this a rate limit?" without copying the same string
table.

This module centralises the taxonomy. Each adapter wraps its
``complete`` and ``stream`` bodies in ``translate_sdk_errors()``; any raw
SDK exception that escapes is re-raised as one of the typed
``ProviderError`` subclasses below, preserving the original via
``__cause__``. Downstream code (``provider_runner``, observability
extensions, programmatic embedders) then asks via ``isinstance``.

Backwards compatibility: legacy callers that catch broad ``Exception``
keep working — every ``ProviderError`` is an ``Exception``. The
classifier is also exposed standalone, so anyone with a stray exception
(e.g. caught from a non-wrapped code path) can still get a typed
verdict.
"""

from __future__ import annotations

import functools
import inspect
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable


class ProviderError(Exception):
    """Base class for provider-adapter errors.

    Subclasses carry a ``retryable`` class attribute used by the loop's
    retry logic. All instances preserve the original SDK exception in
    ``__cause__`` (set automatically by ``raise X from exc``).
    """

    retryable: bool = False


class RateLimited(ProviderError):
    """Provider rate-limit (HTTP 429-ish). Caller should back off and retry."""

    retryable = True


class Transient(ProviderError):
    """Transient infrastructure error — timeout, connection drop, 5xx,
    malformed response. Caller should retry with backoff."""

    retryable = True


class AuthFailure(ProviderError):
    """Authentication / authorisation failure. Retrying won't help; the
    user must fix the API key or grant permission."""

    retryable = False


class InvalidRequest(ProviderError):
    """Request was malformed in a way retrying won't fix (HTTP 400 / 422,
    schema validation errors, unsupported parameter)."""

    retryable = False


# SDK exception class-name substrings, grouped by typed error. The first
# matching family wins. Centralised here so every adapter — and any
# future one — relies on the same translation table instead of growing
# its own copy. Patterns are substrings of ``type(exc).__name__``, the
# same shape the legacy provider_runner.py code used.
_RATE_LIMIT_NAMES: tuple[str, ...] = ("RateLimitError",)
_AUTH_NAMES: tuple[str, ...] = (
    "AuthenticationError",
    "PermissionDeniedError",
    "PermissionDenied",
)
_TRANSIENT_NAMES: tuple[str, ...] = (
    # httpx timeouts
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "ConnectTimeout",
    # httpx / stdlib connection errors
    "ConnectError",
    "ConnectionError",
    "NetworkError",
    # httpx protocol-level errors
    "RemoteProtocolError",
    "LocalProtocolError",
    # generic HTTP status wrappers (anthropic, openai, httpx)
    "APIStatusError",
    "HTTPStatusError",
    # 5xx server errors with named wrappers
    "InternalServerError",
    "ServiceUnavailableError",
)
_INVALID_NAMES: tuple[str, ...] = (
    "BadRequestError",
    "UnprocessableEntityError",
)


def classify_provider_exception(exc: BaseException) -> ProviderError | None:
    """Translate an SDK-specific exception into a typed ``ProviderError``.

    Returns ``None`` when no rule matches; the caller should re-raise the
    original exception unchanged. Already-typed ``ProviderError`` instances
    are returned unchanged so callers can compose multiple translation
    layers without double-wrapping.
    """
    if isinstance(exc, ProviderError):
        return exc
    type_name = type(exc).__name__
    msg = str(exc) or type_name
    if any(n in type_name for n in _RATE_LIMIT_NAMES):
        return RateLimited(msg)
    if any(n in type_name for n in _AUTH_NAMES):
        return AuthFailure(msg)
    if any(n in type_name for n in _INVALID_NAMES):
        return InvalidRequest(msg)
    if any(n in type_name for n in _TRANSIENT_NAMES):
        return Transient(msg)
    return None


@asynccontextmanager
async def translate_sdk_errors() -> AsyncIterator[None]:
    """Re-raise SDK exceptions as typed ``ProviderError`` subclasses.

    Wrap every provider adapter's ``complete`` / ``stream`` body in this
    so the runner — and every downstream consumer — gets a stable
    taxonomy. Usage::

        async def complete(self, ...) -> ProviderResponse:
            async with translate_sdk_errors():
                return await self._client.messages.create(**kwargs)

        async def stream(self, ...) -> AsyncIterator[StreamDelta]:
            async with translate_sdk_errors():
                async for chunk in self._client.stream(...):
                    yield _to_delta(chunk)

    Async generators work because ``async with`` composes with ``yield``.
    ``GeneratorExit`` and ``asyncio.CancelledError`` are ``BaseException``
    subclasses and are deliberately NOT caught here, so cooperative
    cancellation still propagates untouched.

    Already-typed ``ProviderError`` exceptions pass through unchanged so
    inner code that wants to raise a specific subclass directly (e.g.
    a parser that knows the response was malformed) keeps its semantics.
    """
    try:
        yield
    except ProviderError:
        raise
    except Exception as exc:  # noqa: BLE001 — by design: classify everything
        translated = classify_provider_exception(exc)
        if translated is None:
            raise
        raise translated from exc


def translate_provider_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator: wrap a provider method's body with ``translate_sdk_errors()``.

    Use on a provider adapter's ``complete`` (async coroutine) and
    ``stream`` (async generator) methods to translate any escaping SDK
    exception into a typed ``ProviderError`` subclass. The decorator
    auto-detects which kind of function it's wrapping:

        @translate_provider_errors
        async def complete(self, ...): ...

        @translate_provider_errors
        async def stream(self, ...):
            ...
            yield delta

    Picking the right wrapper at decoration time (rather than at call
    time) is important: ``yield`` inside a regular ``async def`` makes
    the function an async generator, and an async generator wrapper that
    accidentally ``await``s its inner call would consume the generator
    in one shot.
    """
    if inspect.isasyncgenfunction(fn):

        @functools.wraps(fn)
        async def _asyncgen_wrapper(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            async with translate_sdk_errors():
                async for item in fn(*args, **kwargs):
                    yield item

        return _asyncgen_wrapper

    @functools.wraps(fn)
    async def _coro_wrapper(*args: Any, **kwargs: Any) -> Any:
        async with translate_sdk_errors():
            return await fn(*args, **kwargs)

    return _coro_wrapper
