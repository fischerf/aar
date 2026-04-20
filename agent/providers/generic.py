"""Generic OpenAI-compatible provider adapter.

Endpoint
--------
    POST https://api.provider.com/gpt/gpt-5.1

Authentication
--------------
This provider uses a custom ``api-key`` header instead of the standard
``Authorization: Bearer <token>`` scheme used by OpenAI-compatible APIs.

Confirmed capabilities (live-tested)
-------------------------------------
* Chat completions (OpenAI wire format)
* Native function / tool calling  (finish_reason="tool_calls")
* Structured output via response_format json_object
* Structured output via response_format json_schema  (strict mode)
* Server-Sent Events streaming — including streamed tool-call argument deltas

Environment variables
---------------------
GENERIC_API_KEY
    API key when ``api_key`` is not provided in ``ProviderConfig.api_key``.
GENERIC_ENDPOINT
    Fallback endpoint URL; overrides the well-known public URL when set.

Configuration example
---------------------
    from agent import AgentConfig, ProviderConfig

    config = AgentConfig(
        provider=ProviderConfig(
            name="generic",
            model="gpt-4o-2024-08-06",
            api_key="...",          # or set GENERIC_API_KEY env var
            max_tokens=1024,
            temperature=0.0,
            extra={
                # Optional: override the endpoint for a private deployment.
                "endpoint": "https://my-proxy.corp.com/chat-ai/gpt4",
                # Optional: merge extra HTTP headers into every request.
                "extra_headers": {"X-Trace-Id": "abc123"},
                # Optional: per-request HTTP timeout in seconds (default 60).
                "timeout": 120.0,
                # Optional: response_format override — "text" | "json_object"
                #           | "json_schema".  When "json_schema" you must also
                #           supply "json_schema_def" (the schema dict).
                "response_format": "json_object",
            },
        )
    )
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncIterator

import httpx

from agent.core.config import ProviderConfig
from agent.core.events import ProviderMeta, StopReason, ToolCall
from agent.providers.base import Provider, ProviderResponse, StreamDelta

logger = logging.getLogger(__name__)

# Context window for the backing GPT model.
_CONTEXT_LIMIT = 128_000


class GenericProvider(Provider):
    """Adapter for a generic OpenAI-compatible Chat-AI REST endpoint.

    The endpoint speaks the OpenAI Chat Completions wire format and has been
    live-tested to support:

    * Native tool / function calling
    * ``response_format`` → ``json_object`` and ``json_schema`` (strict)
    * Server-Sent Events streaming, including streamed tool-call argument deltas

    All constructor arguments are driven by ``ProviderConfig``:

    * ``config.api_key``      — API key (falls back to ``GENERIC_API_KEY``).
    * ``config.model``        — forwarded in the request body.
    * ``config.max_tokens``   — mapped to ``max_completion_tokens``.
    * ``config.temperature``  — sampling temperature.
    * ``config.base_url``     — optional endpoint override (also readable from
                                ``config.extra["endpoint"]`` or
                                ``GENERIC_ENDPOINT``).
    * ``config.extra["extra_headers"]``   — dict of additional HTTP headers.
    * ``config.extra["timeout"]``         — per-request timeout in seconds (default 60).
    * ``config.extra["response_format"]`` — ``"text"`` | ``"json_object"`` |
                                            ``"json_schema"`` (default: ``"text"``).
    * ``config.extra["json_schema_def"]`` — schema dict required when
                                            ``response_format`` is ``"json_schema"``.
    """

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)

        # ── API key ───────────────────────────────────────────────────────────
        self._api_key: str = config.api_key or os.environ.get("GENERIC_API_KEY", "")
        if not self._api_key:
            raise ValueError(
                "No API key provided. Pass api_key in ProviderConfig "
                "or set the GENERIC_API_KEY environment variable."
            )

        # ── Endpoint (priority: base_url > extra.endpoint > env > default) ───
        self._endpoint: str = (
            config.base_url
            or str(config.extra.get("endpoint", ""))
            or os.environ.get("GENERIC_ENDPOINT", "")
        ).rstrip("/")

        # ── Optional extras ───────────────────────────────────────────────────
        self._extra_headers: dict[str, str] = dict(config.extra.get("extra_headers", {}))
        self._timeout: float = float(config.extra.get("timeout", 60.0))

        # ── Shared async HTTP client ──────────────────────────────────────────
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout, connect=10.0),
        )

        logger.info(
            "GenericProvider initialised: endpoint=%s  model=%s",
            self._endpoint,
            self.config.model,
        )

    # ------------------------------------------------------------------
    # Provider capability interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "generic"

    @property
    def supports_tools(self) -> bool:
        return True

    @property
    def supports_streaming(self) -> bool:
        return True

    @property
    def supports_structured_output(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Core methods
    # ------------------------------------------------------------------

    @property
    def supports_reasoning(self) -> bool:
        # Enable for any OpenAI-compat model that emits <think> tags or channel tokens
        # (e.g. Qwen3-direct, OpenRouter thinking models) via extra={"supports_reasoning": true}
        return bool(self.config.extra.get("supports_reasoning", False))

    @property
    def supports_vision(self) -> bool:
        # Override via extra={"supports_vision": True/False}.
        if "supports_vision" in self.config.extra:
            return bool(self.config.extra["supports_vision"])
        model = self.config.model.lower()
        return "vision" in model or "4o" in model

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
    ) -> ProviderResponse:
        """Send *messages* to the endpoint and return the reply.

        Parameters
        ----------
        messages:
            Conversation history in the internal Anthropic-style content-block
            format.  Converted to the OpenAI wire format before sending.
        tools:
            Tool schemas in the internal format.  Converted to OpenAI
            function-calling format and sent natively.
        system:
            System prompt prepended as a ``{"role": "system", ...}`` message.
        """
        api_messages = _build_messages(messages, system)
        payload = self._build_payload(api_messages, tools, stream=False)
        headers = self._build_headers()

        logger.debug(
            "GenericProvider → POST %s  model=%s  messages=%d  tools=%d",
            self._endpoint,
            self.config.model,
            len(api_messages),
            len(tools) if tools else 0,
        )

        try:
            http_resp = await self._client.post(
                self._endpoint,
                json=payload,
                headers=headers,
            )
        except httpx.TimeoutException as exc:
            raise RuntimeError(
                f"Generic provider request timed out after {self._timeout}s: {exc}"
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"Generic provider network error: {exc}") from exc

        _raise_for_status(http_resp)

        try:
            data: dict[str, Any] = http_resp.json()
        except Exception as exc:
            raise RuntimeError(
                f"Generic provider returned a non-JSON body: {http_resp.text[:200]}"
            ) from exc

        resp = _parse_response(data, self.config.model)

        if self.supports_reasoning and not resp.reasoning:
            from agent.providers._thinking import extract_all, extract_reasoning_content

            first_choice = data.get("choices", [{}])[0]
            message = first_choice.get("message", {})
            reasoning_blocks = extract_reasoning_content(message)
            if not reasoning_blocks:
                content, reasoning_blocks = extract_all(resp.content)
                resp = ProviderResponse(
                    content=content,
                    tool_calls=resp.tool_calls,
                    stop_reason=resp.stop_reason,
                    reasoning=reasoning_blocks,
                    meta=resp.meta,
                )
            else:
                resp = ProviderResponse(
                    content=resp.content,
                    tool_calls=resp.tool_calls,
                    stop_reason=resp.stop_reason,
                    reasoning=reasoning_blocks,
                    meta=resp.meta,
                )

        return resp

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
    ) -> AsyncIterator[StreamDelta]:
        """Stream response deltas from the endpoint.

        Yields :class:`StreamDelta` objects as SSE chunks arrive.
        Tool-call argument fragments are accumulated and emitted as a single
        ``tool_call_delta`` once the stream is complete.
        """
        api_messages = _build_messages(messages, system)
        payload = self._build_payload(api_messages, tools, stream=True)
        headers = self._build_headers()

        logger.debug(
            "GenericProvider → POST (stream) %s  model=%s  messages=%d",
            self._endpoint,
            self.config.model,
            len(api_messages),
        )

        # Accumulators for in-progress tool calls indexed by their position.
        # Structure: {index: {"id": str, "name": str, "arguments": str}}
        tool_acc: dict[int, dict[str, str]] = {}
        stream_usage: dict[str, int] = {}
        stream_model: str = self.config.model

        from agent.providers._thinking import StreamThinkingRouter

        router = StreamThinkingRouter() if self.supports_reasoning else None

        try:
            async with self._client.stream(
                "POST",
                self._endpoint,
                json=payload,
                headers=headers,
            ) as http_resp:
                _raise_for_status(http_resp)

                async for line in http_resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue

                    try:
                        chunk: dict[str, Any] = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    # Capture usage if present (some endpoints include it)
                    raw_usage: dict[str, Any] = chunk.get("usage") or {}
                    if raw_usage:
                        if "prompt_tokens" in raw_usage:
                            stream_usage["input_tokens"] = int(raw_usage["prompt_tokens"])
                        if "completion_tokens" in raw_usage:
                            stream_usage["output_tokens"] = int(raw_usage["completion_tokens"])
                        if "total_tokens" in raw_usage:
                            stream_usage["total_tokens"] = int(raw_usage["total_tokens"])
                    if chunk.get("model"):
                        stream_model = chunk["model"]

                    choices: list[Any] = chunk.get("choices", [])
                    if not choices:
                        continue

                    choice = choices[0]
                    delta: dict[str, Any] = choice.get("delta", {})
                    finish_reason: str = choice.get("finish_reason") or ""

                    # ── Text delta ────────────────────────────────────────────
                    text_piece: str = delta.get("content") or ""
                    reasoning_piece: str = ""
                    if text_piece and router:
                        text_piece, reasoning_piece = router.feed(text_piece)
                    if text_piece or reasoning_piece:
                        yield StreamDelta(text=text_piece, reasoning_delta=reasoning_piece)

                    # ── Tool-call argument fragments ──────────────────────────
                    for tc_delta in delta.get("tool_calls", []):
                        idx: int = tc_delta.get("index", 0)
                        fn: dict[str, Any] = tc_delta.get("function", {})

                        if idx not in tool_acc:
                            tool_acc[idx] = {"id": "", "name": "", "arguments": ""}

                        if tc_delta.get("id"):
                            tool_acc[idx]["id"] = tc_delta["id"]
                        if fn.get("name"):
                            tool_acc[idx]["name"] = fn["name"]
                        if fn.get("arguments"):
                            tool_acc[idx]["arguments"] += fn["arguments"]

                    # ── Done ─────────────────────────────────────────────────
                    if finish_reason:
                        if router:
                            clean, leftover = router.flush()
                            if clean or leftover:
                                yield StreamDelta(text=clean, reasoning_delta=leftover)
                        # Emit fully-assembled tool calls at stream end
                        for acc in tool_acc.values():
                            try:
                                parsed_args = json.loads(acc["arguments"] or "{}")
                            except json.JSONDecodeError:
                                parsed_args = {"raw": acc["arguments"]}
                            yield StreamDelta(
                                tool_call_delta={
                                    "tool_call_id": acc["id"],
                                    "tool_name": acc["name"],
                                    "arguments": parsed_args,
                                }
                            )
                        stream_meta: ProviderMeta | None = None
                        if stream_usage:
                            stream_meta = ProviderMeta(
                                provider="generic",
                                model=stream_model,
                                usage=stream_usage,
                            )
                        yield StreamDelta(done=True, meta=stream_meta)
                        return

        except httpx.TimeoutException as exc:
            raise RuntimeError(
                f"Generic provider stream timed out after {self._timeout}s: {exc}"
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"Generic provider stream network error: {exc}") from exc

        # Fallback done sentinel if finish_reason never arrived
        stream_meta_fb: ProviderMeta | None = None
        if stream_usage:
            stream_meta_fb = ProviderMeta(
                provider="generic",
                model=stream_model,
                usage=stream_usage,
            )
        yield StreamDelta(done=True, meta=stream_meta_fb)

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_headers(self) -> dict[str, str]:
        """Return HTTP headers for every request.

        Uses a custom ``api-key`` header rather than the OpenAI-style
        ``Authorization: Bearer …`` header.
        """
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "api-key": self._api_key,
        }
        headers.update(self._extra_headers)
        return headers

    def _build_payload(
        self,
        api_messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        """Assemble the JSON request body in the OpenAI Chat Completions format."""
        payload: dict[str, Any] = {"messages": api_messages}

        if self.config.model:
            payload["model"] = self.config.model

        if self.config.max_tokens:
            # Uses max_completion_tokens, not the legacy max_tokens parameter.
            payload["max_completion_tokens"] = self.config.max_tokens

        payload["temperature"] = self.config.temperature
        payload["stream"] = stream

        # ── Native tool calling ───────────────────────────────────────────────
        if tools:
            payload["tools"] = _convert_tools(tools)

        # ── Structured output ─────────────────────────────────────────────────
        # Config-level response_format takes precedence over extra.response_format.
        fmt: str = self.config.response_format or str(
            self.config.extra.get("response_format", "text")
        )
        if fmt in ("json", "json_object"):
            payload["response_format"] = {"type": "json_object"}
        elif fmt == "json_schema":
            schema_def: dict[str, Any] = self.config.json_schema or dict(
                self.config.extra.get("json_schema_def", {})
            )
            if schema_def:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": schema_def,
                }
            else:
                logger.warning(
                    "GenericProvider: response_format='json_schema' requested but "
                    "no json_schema supplied — falling back to text."
                )

        return payload


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _build_messages(
    messages: list[dict[str, Any]],
    system: str,
) -> list[dict[str, Any]]:
    """Convert internal Anthropic-style messages to the OpenAI wire format.

    * String content  → passed through unchanged.
    * Assistant list  → text parts joined, tool_use blocks become tool_calls.
    * User list       → tool_result blocks become role=tool messages;
                        plain text blocks are joined with a space.
    """
    api_messages: list[dict[str, Any]] = []

    if system:
        api_messages.append({"role": "system", "content": system})

    for msg in messages:
        role: str = msg["role"]
        content = msg["content"]

        if isinstance(content, str):
            api_messages.append({"role": role, "content": content})
            continue

        if isinstance(content, list):
            if role == "assistant":
                api_messages.append(_convert_assistant_blocks(content))
            elif role == "user":
                tool_results = [b for b in content if b.get("type") == "tool_result"]
                if tool_results:
                    for tr in tool_results:
                        api_messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tr.get("tool_use_id", ""),
                                "content": tr.get("content", ""),
                            }
                        )
                else:
                    # Build an OpenAI-compatible content array, preserving
                    # image_url blocks alongside text blocks.
                    oai_parts: list[dict[str, Any]] = []
                    for b in content:
                        btype = b.get("type")
                        if btype == "text":
                            oai_parts.append({"type": "text", "text": b["text"]})
                        elif btype == "image_url":
                            # Already in the correct OpenAI format; pass through.
                            oai_parts.append(b)

                    if not oai_parts:
                        continue

                    # Unwrap single-text-block lists to a plain string for
                    # cleaner serialization when there are no images.
                    if len(oai_parts) == 1 and oai_parts[0].get("type") == "text":
                        api_messages.append({"role": "user", "content": oai_parts[0]["text"]})
                    else:
                        api_messages.append({"role": "user", "content": oai_parts})

    return api_messages


def _convert_assistant_blocks(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert Anthropic-style assistant content blocks to OpenAI format."""
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for block in blocks:
        if block.get("type") == "text":
            text_parts.append(block["text"])
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block["name"],
                        "arguments": json.dumps(block.get("input", {})),
                    },
                }
            )

    msg: dict[str, Any] = {"role": "assistant"}
    msg["content"] = " ".join(text_parts) if text_parts else None
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert internal tool schemas to the OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for tool in tools
    ]


def _parse_response(data: dict[str, Any], fallback_model: str) -> ProviderResponse:
    """Extract content, tool calls, and metadata from a Chat Completions body."""
    choices: list[Any] = data.get("choices", [])
    if not choices:
        raise RuntimeError(
            f"Generic provider returned an empty 'choices' array. Full response: {json.dumps(data)[:400]}"
        )

    first_choice = choices[0]
    message: dict[str, Any] = first_choice.get("message", {})
    content: str = message.get("content") or ""
    finish_reason: str = first_choice.get("finish_reason", "")

    if finish_reason == "content_filter":
        logger.warning(
            "Generic provider: response was blocked by the content filter (finish_reason='content_filter')."
        )

    # ── Native tool calls ─────────────────────────────────────────────────────
    tool_calls: list[ToolCall] = []
    for i, tc in enumerate(message.get("tool_calls", [])):
        fn = tc.get("function", {})
        raw_args = fn.get("arguments", "{}")
        try:
            arguments = json.loads(raw_args)
        except (json.JSONDecodeError, TypeError):
            arguments = {"raw": raw_args}
        tool_calls.append(
            ToolCall(
                tool_name=fn.get("name", ""),
                tool_call_id=tc.get("id", f"generic_tc_{i}"),
                arguments=arguments,
            )
        )

    stop_reason = _map_stop_reason(finish_reason)

    # ── Usage metadata ────────────────────────────────────────────────────────
    raw_usage: dict[str, Any] = data.get("usage", {})
    usage: dict[str, int] = {}
    if "prompt_tokens" in raw_usage:
        usage["input_tokens"] = int(raw_usage["prompt_tokens"])
    if "completion_tokens" in raw_usage:
        usage["output_tokens"] = int(raw_usage["completion_tokens"])
    if "total_tokens" in raw_usage:
        usage["total_tokens"] = int(raw_usage["total_tokens"])

    meta = ProviderMeta(
        provider="generic",
        model=data.get("model") or fallback_model,
        usage=usage,
        request_id=data.get("id", ""),
    )

    logger.debug(
        "GenericProvider ← finish_reason=%s  content_len=%d  tool_calls=%d  usage=%s",
        finish_reason,
        len(content),
        len(tool_calls),
        usage,
    )

    return ProviderResponse(
        content=content,
        tool_calls=tool_calls,
        stop_reason=stop_reason,
        meta=meta,
    )


def _raise_for_status(resp: httpx.Response) -> None:
    """Raise a typed ``RuntimeError`` for non-200 HTTP responses."""
    status = resp.status_code
    if status == 200:
        return

    body: str = resp.text or ""

    if status in (401, 403):
        raise PermissionError(
            f"Generic provider authentication failed (HTTP {status}). Check your GENERIC_API_KEY."
        )
    if status == 429:
        raise RuntimeError("Generic provider rate limit exceeded (HTTP 429). Back off and retry.")
    body_lower = body.lower()
    if status == 400 and any(
        phrase in body_lower for phrase in ("context_length", "maximum context", "token limit")
    ):
        raise RuntimeError(f"Generic provider context limit exceeded (HTTP 400): {body[:200]}")
    if status == 400 and "unsupported parameter" in body_lower:
        raise RuntimeError(
            f"Generic provider rejected a request parameter (HTTP 400): {body[:400]}"
        )

    raise RuntimeError(f"Generic provider returned HTTP {status}: {body[:400]}")


def _map_stop_reason(finish_reason: str) -> str:
    mapping: dict[str, str] = {
        "stop": StopReason.END_TURN.value,
        "tool_calls": StopReason.TOOL_USE.value,
        "length": StopReason.MAX_TOKENS.value,
        "content_filter": StopReason.ERROR.value,
    }
    return mapping.get(finish_reason, StopReason.END_TURN.value)
