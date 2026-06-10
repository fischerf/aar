"""Google Gemini provider adapter.

Two operation modes
-------------------
SDK mode  (``base_url`` is empty)
    Uses the official ``google-genai`` SDK.  Install with::

        pip install google-genai

    Configure::

        ProviderConfig(
            name="gemini",
            model="gemini-2.5-flash",   # or "gemini-2.5-pro"
            api_key="YOUR_GEMINI_API_KEY",
        )

HTTP mode  (``base_url`` is set)
    Uses direct HTTP requests with the Google GenerateContent wire format.
    Suitable for custom-endpoint deployments such as API-gateway proxies.

    The provider appends ``:generateContent`` and ``:streamGenerateContentSse``
    to ``base_url`` automatically.  Override individual URLs via ``extra``.

    Configure::

        ProviderConfig(
            name="gemini",
            model="gemini-2.5-flash",   # or "gemini-2.5-pro"
            api_key="YOUR_API_KEY",
            # base_url should end with the model path, e.g.:
            #   https://gateway.example.com/path/to/gemini/flash
            # The provider appends :generateContent / :streamGenerateContentSse
            base_url="https://gateway.example.com/path/to/gemini/flash",
            extra={
                # Override specific endpoint URLs (optional):
                "endpoint":        "https://.../:generateContent",
                "stream_endpoint": "https://.../:streamGenerateContentSse",
                # HTTP auth header name (default: "api-key"):
                "auth_header": "api-key",
                # Per-request timeout in seconds (default: 120):
                "timeout": 120.0,
                # Thinking budget: -1 = model default (thinking on for pro),
                #                   0 = disabled (flash default),
                #                   N = explicit token budget.
                "thinking_budget": 0,
            },
        )

Environment variables
---------------------
GEMINI_API_KEY
    Fallback API key when ``config.api_key`` is not set.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncIterator

import httpx

from agent.core.config import ProviderConfig
from agent.core.events import ProviderMeta, ReasoningBlock, StopReason, ToolCall
from agent.providers.base import Provider, ProviderResponse, StreamDelta

logger = logging.getLogger(__name__)


class GeminiProvider(Provider):
    """Adapter for Google Gemini (GenerateContent API).

    Operates transparently in two modes:

    * **SDK mode** – uses the ``google-genai`` package when ``base_url`` is
      empty.  Authentication is handled by the SDK via the supplied API key.
    * **HTTP mode** – uses raw ``httpx`` requests with the GenerateContent
      JSON wire format when ``base_url`` is set.  Useful for API-gateway
      proxies that mirror the standard Gemini / Vertex AI REST interface.
    """

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)

        self._api_key: str = config.api_key or os.environ.get("GEMINI_API_KEY", "")
        if not self._api_key:
            raise ValueError(
                "No API key provided. Pass api_key in ProviderConfig "
                "or set the GEMINI_API_KEY environment variable."
            )

        self._timeout: float = float(config.extra.get("timeout", 120.0))

        # ── Thinking budget ────────────────────────────────────────────────────
        # -1 = model default (thinking enabled for pro, server decides for flash)
        #  0 = explicitly disabled
        #  N = explicit token budget
        model_lower = config.model.lower()
        _is_pro = "pro" in model_lower
        _default_budget: int = -1 if _is_pro else 0
        self._thinking_budget: int = int(config.extra.get("thinking_budget", _default_budget))

        # Include thought parts in the response so the TUI can render them.
        # Defaults to True when thinking is enabled; set extra.include_thoughts=false
        # to suppress thoughts (saves output tokens at the cost of visibility).
        self._include_thoughts: bool = bool(
            config.extra.get("include_thoughts", self._thinking_budget != 0)
        )

        # ── Mode selection ────────────────────────────────────────────────────────────
        self._use_http: bool = bool(config.base_url or config.extra.get("endpoint"))

        if self._use_http:
            self._init_http(config)
        else:
            self._init_sdk(config)

    # ------------------------------------------------------------------
    # Initialisation helpers
    # ------------------------------------------------------------------

    def _init_sdk(self, config: ProviderConfig) -> None:
        try:
            from google import genai  # noqa: F401 – import check only
        except ImportError:
            raise ImportError(
                "The 'google-genai' package is required for Gemini SDK mode. "
                "Install with: pip install google-genai  "
                "or: pip install aar-agent[gemini]"
            )
        from google import genai as _genai

        self._sdk_client = _genai.Client(api_key=self._api_key)
        logger.info("GeminiProvider (SDK mode): model=%s", config.model)

    def _init_http(self, config: ProviderConfig) -> None:
        base = (config.base_url or str(config.extra.get("endpoint", ""))).rstrip("/")

        # Strip any accidentally-included operation suffix so we can append cleanly.
        for _suffix in (
            ":generateContent",
            ":streamGenerateContent",
            ":streamGenerateContentSse",
        ):
            if base.endswith(_suffix):
                base = base[: -len(_suffix)]
                break

        self._endpoint: str = str(config.extra.get("endpoint", f"{base}:generateContent"))
        self._stream_endpoint: str = str(
            config.extra.get("stream_endpoint", f"{base}:streamGenerateContentSse")
        )

        # Auth header name – default matches the Generic AI service convention
        # used by the other providers; override via extra.auth_header if needed.
        self._auth_header: str = str(config.extra.get("auth_header", "api-key"))

        self._http_client: httpx.AsyncClient = httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout, connect=10.0),
        )
        logger.info(
            "GeminiProvider (HTTP mode): endpoint=%s  model=%s",
            self._endpoint,
            config.model,
        )

    # ------------------------------------------------------------------
    # Provider capability interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def supports_reasoning(self) -> bool:
        # Thinking is active when budget is not explicitly zero.
        return self._thinking_budget != 0

    @property
    def supports_vision(self) -> bool:
        return True  # All Gemini 2.5 models support image input.

    @property
    def supports_structured_output(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
    ) -> ProviderResponse:
        if self._use_http:
            return await self._complete_http(messages, tools, system)
        return await self._complete_sdk(messages, tools, system)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
    ) -> AsyncIterator[StreamDelta]:
        if self._use_http:
            async for delta in self._stream_http(messages, tools, system):
                yield delta
        else:
            async for delta in self._stream_sdk(messages, tools, system):
                yield delta

    async def close(self) -> None:
        if self._use_http and hasattr(self, "_http_client"):
            await self._http_client.aclose()

    # ------------------------------------------------------------------
    # SDK mode – complete / stream
    # ------------------------------------------------------------------

    async def _complete_sdk(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        system: str,
    ) -> ProviderResponse:
        contents = _build_contents(messages)
        cfg = self._build_sdk_config(system, tools)

        response = await self._sdk_client.aio.models.generate_content(
            model=self.config.model,
            contents=contents,
            config=cfg,
        )
        return _parse_sdk_response(response, self.config.model)

    async def _stream_sdk(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        system: str,
    ) -> AsyncIterator[StreamDelta]:
        contents = _build_contents(messages)
        cfg = self._build_sdk_config(system, tools)

        tool_acc: list[dict[str, Any]] = []
        usage: dict[str, int] = {}

        async for chunk in await self._sdk_client.aio.models.generate_content_stream(
            model=self.config.model,
            contents=contents,
            config=cfg,
        ):
            # Capture usage – last non-empty value wins.
            if chunk.usage_metadata:
                um = chunk.usage_metadata
                if um.prompt_token_count:
                    usage["input_tokens"] = um.prompt_token_count
                if um.candidates_token_count:
                    usage["output_tokens"] = um.candidates_token_count
                if um.total_token_count:
                    usage["total_tokens"] = um.total_token_count

            if not chunk.candidates:
                continue

            candidate = chunk.candidates[0]
            if not (candidate.content and candidate.content.parts):
                continue

            for part in candidate.content.parts:
                # Thinking / reasoning token
                if getattr(part, "thought", False):
                    if part.text:
                        yield StreamDelta(reasoning_delta=part.text)
                elif part.text:
                    yield StreamDelta(text=part.text)
                elif part.function_call:
                    tool_acc.append(
                        {
                            "name": part.function_call.name,
                            "args": dict(part.function_call.args)
                            if part.function_call.args
                            else {},
                        }
                    )

            finish = str(candidate.finish_reason) if candidate.finish_reason else ""
            if finish and finish not in (
                "",
                "FINISH_REASON_UNSPECIFIED",
                "FinishReason.FINISH_REASON_UNSPECIFIED",
            ):
                for i, fc in enumerate(tool_acc):
                    yield StreamDelta(
                        tool_call_delta={
                            "tool_call_id": f"gemini_tc_{i}",
                            "tool_name": fc["name"],
                            "arguments": fc["args"],
                        }
                    )
                yield StreamDelta(
                    done=True,
                    meta=ProviderMeta(
                        provider="gemini",
                        model=self.config.model,
                        usage=usage,
                    )
                    if usage
                    else None,
                )
                return

        yield StreamDelta(done=True)

    def _build_sdk_config(
        self,
        system: str,
        tools: list[dict[str, Any]] | None,
    ):
        from google.genai import types

        kwargs: dict[str, Any] = {}

        if system:
            kwargs["system_instruction"] = system
        if self.config.max_tokens:
            kwargs["max_output_tokens"] = self.config.max_tokens
        if self.config.temperature > 0:
            kwargs["temperature"] = self.config.temperature
        if tools:
            kwargs["tools"] = _convert_tools_sdk(tools)

        # thinking_budget == 0  → explicitly disabled, no thoughts
        # thinking_budget  > 0  → explicit budget, include thoughts when requested
        # thinking_budget == -1 → model-default budget, include thoughts when requested
        if self._thinking_budget == 0:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        elif self._thinking_budget > 0:
            kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_budget=self._thinking_budget,
                include_thoughts=self._include_thoughts,
            )
        elif self._include_thoughts:
            # -1: let the model decide the budget, but ask for thoughts back
            kwargs["thinking_config"] = types.ThinkingConfig(include_thoughts=True)
        # else: -1 + include_thoughts=False → omit entirely, model thinks silently

        return types.GenerateContentConfig(**kwargs)

    # ------------------------------------------------------------------
    # HTTP mode – complete / stream
    # ------------------------------------------------------------------

    async def _complete_http(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        system: str,
    ) -> ProviderResponse:
        payload = self._build_http_payload(messages, tools, system)
        headers = self._build_http_headers()

        try:
            resp = await self._http_client.post(
                self._endpoint,
                json=payload,
                headers=headers,
            )
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"Gemini request timed out after {self._timeout}s: {exc}") from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"Gemini network error: {exc}") from exc

        _raise_for_status(resp)

        try:
            data: dict[str, Any] = resp.json()
        except Exception as exc:
            raise RuntimeError(f"Gemini returned a non-JSON body: {resp.text[:200]}") from exc

        return _parse_http_response(data, self.config.model)

    async def _stream_http(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        system: str,
    ) -> AsyncIterator[StreamDelta]:
        payload = self._build_http_payload(messages, tools, system)
        headers = self._build_http_headers()

        tool_acc: list[dict[str, Any]] = []
        usage: dict[str, int] = {}

        try:
            async with self._http_client.stream(
                "POST",
                self._stream_endpoint,
                json=payload,
                headers=headers,
            ) as http_resp:
                _raise_for_status(http_resp)

                async for line in http_resp.aiter_lines():
                    # SSE format: lines are prefixed with "data: "
                    if line.startswith("data:"):
                        raw = line[5:].strip()
                    elif line.startswith("{"):
                        # Chunked transfer: raw JSON per line
                        raw = line.strip()
                    else:
                        continue

                    if not raw or raw == "[DONE]":
                        continue

                    try:
                        chunk: dict[str, Any] = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    # Capture usage metadata
                    usage_meta: dict[str, Any] = chunk.get("usageMetadata", {})
                    if usage_meta:
                        if "promptTokenCount" in usage_meta:
                            usage["input_tokens"] = usage_meta["promptTokenCount"]
                        if "candidatesTokenCount" in usage_meta:
                            usage["output_tokens"] = usage_meta["candidatesTokenCount"]
                        if "totalTokenCount" in usage_meta:
                            usage["total_tokens"] = usage_meta["totalTokenCount"]

                    candidates: list[Any] = chunk.get("candidates", [])
                    if not candidates:
                        continue

                    candidate = candidates[0]
                    content: dict[str, Any] = candidate.get("content", {})
                    parts: list[dict[str, Any]] = content.get("parts", [])
                    finish_reason: str = candidate.get("finishReason", "")

                    for part in parts:
                        if part.get("thought", False):
                            if part.get("text"):
                                yield StreamDelta(reasoning_delta=part["text"])
                        elif "text" in part:
                            yield StreamDelta(text=part["text"])
                        elif "functionCall" in part:
                            fc = part["functionCall"]
                            tool_acc.append(
                                {
                                    "name": fc.get("name", ""),
                                    "args": fc.get("args", {}),
                                }
                            )

                    if finish_reason and finish_reason not in (
                        "",
                        "FINISH_REASON_UNSPECIFIED",
                    ):
                        for i, fc in enumerate(tool_acc):
                            yield StreamDelta(
                                tool_call_delta={
                                    "tool_call_id": f"gemini_tc_{i}",
                                    "tool_name": fc["name"],
                                    "arguments": fc["args"],
                                }
                            )
                        yield StreamDelta(
                            done=True,
                            meta=ProviderMeta(
                                provider="gemini",
                                model=self.config.model,
                                usage=usage,
                            )
                            if usage
                            else None,
                        )
                        return

        except httpx.TimeoutException as exc:
            raise RuntimeError(f"Gemini stream timed out after {self._timeout}s: {exc}") from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"Gemini stream network error: {exc}") from exc

        # Fallback sentinel if finish_reason never arrived in the stream.
        yield StreamDelta(
            done=True,
            meta=ProviderMeta(
                provider="gemini",
                model=self.config.model,
                usage=usage,
            )
            if usage
            else None,
        )

    def _build_http_payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        system: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contents": _build_contents(messages),
        }

        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        if tools:
            payload["tools"] = [{"functionDeclarations": _convert_tools_http(tools)}]

        gen_config: dict[str, Any] = {}
        if self.config.max_tokens:
            gen_config["maxOutputTokens"] = self.config.max_tokens
        if self.config.temperature > 0:
            gen_config["temperature"] = self.config.temperature
        # thinking_budget == 0  → explicitly disabled, no thoughts returned
        # thinking_budget  > 0  → explicit budget, request thoughts when enabled
        # thinking_budget == -1 → model-default budget, request thoughts when enabled
        if self._thinking_budget == 0:
            gen_config["thinkingConfig"] = {"thinkingBudget": 0}
        elif self._thinking_budget > 0:
            thinking_cfg: dict[str, Any] = {"thinkingBudget": self._thinking_budget}
            if self._include_thoughts:
                thinking_cfg["includeThoughts"] = True
            gen_config["thinkingConfig"] = thinking_cfg
        elif self._include_thoughts:
            # -1: let the server pick the budget, but return thoughts in the response
            gen_config["thinkingConfig"] = {"includeThoughts": True}
        # else: -1 + include_thoughts=False → omit, model thinks silently

        if gen_config:
            payload["generationConfig"] = gen_config

        return payload

    def _build_http_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            self._auth_header: self._api_key,
        }


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _build_contents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert internal Anthropic-style messages to Gemini ``contents`` format.

    Role mapping:  ``"assistant"`` → ``"model"``,  ``"user"`` → ``"user"``.

    Gemini uses ``parts`` instead of ``content``, and tool interactions are
    represented as ``functionCall`` / ``functionResponse`` parts rather than
    OpenAI-style ``tool_calls`` / ``role=tool`` messages.

    A forward-scan mapping of ``tool_use_id → function_name`` is maintained so
    that ``tool_result`` blocks can be emitted as ``functionResponse`` parts
    with the correct function name.
    """
    contents: list[dict[str, Any]] = []
    # Built up as we scan forward through the conversation history.
    _tool_id_to_name: dict[str, str] = {}

    for msg in messages:
        role: str = msg["role"]
        content = msg["content"]
        gemini_role = "model" if role == "assistant" else "user"

        # ── Plain string ────────────────────────────────────────────────────────────────────
        if isinstance(content, str):
            if content.strip():
                contents.append({"role": gemini_role, "parts": [{"text": content}]})
            continue

        if not isinstance(content, list):
            continue

        # ── Tool-result turn (user message containing tool_result blocks) ─────
        tool_results = [b for b in content if b.get("type") == "tool_result"]
        if tool_results and role == "user":
            parts: list[dict[str, Any]] = []
            for tr in tool_results:
                tid = tr.get("tool_use_id", "")
                # Look up the original function name registered during the
                # preceding assistant turn.
                fname = _tool_id_to_name.get(tid, tid)
                result = tr.get("content", "")
                response_data: Any = {"content": result} if isinstance(result, str) else result
                parts.append(
                    {
                        "functionResponse": {
                            "name": fname,
                            "response": response_data,
                        }
                    }
                )
            if parts:
                contents.append({"role": "user", "parts": parts})
            continue

        # ── Structured content turn (text + tool_use blocks) ─────────────────
        parts = []
        for block in content:
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                parts.append({"text": block["text"]})
            elif btype == "tool_use":
                _tool_id_to_name[block.get("id", "")] = block.get("name", "")
                parts.append(
                    {
                        "functionCall": {
                            "name": block.get("name", ""),
                            "args": block.get("input", {}),
                        }
                    }
                )

        if parts:
            contents.append({"role": gemini_role, "parts": parts})

    return contents


def _convert_tools_sdk(tools: list[dict[str, Any]]):
    """Convert internal tool schemas to a ``google.genai`` ``Tool`` object."""
    from google.genai import types

    declarations = [
        types.FunctionDeclaration(
            name=t["name"],
            description=t.get("description", ""),
            parameters_json_schema=t.get("input_schema", {}),
        )
        for t in tools
    ]
    return [types.Tool(function_declarations=declarations)]


def _convert_tools_http(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert internal tool schemas to Gemini ``functionDeclarations`` dicts."""
    return [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
        }
        for t in tools
    ]


def _parse_sdk_response(response: Any, fallback_model: str) -> ProviderResponse:
    """Extract a :class:`ProviderResponse` from a ``google.genai`` response."""
    content_text = ""
    tool_calls: list[ToolCall] = []
    reasoning_blocks: list[ReasoningBlock] = []

    if response.candidates:
        candidate = response.candidates[0]
        if candidate.content and candidate.content.parts:
            for part in candidate.content.parts:
                if getattr(part, "thought", False):
                    if part.text:
                        reasoning_blocks.append(ReasoningBlock(content=part.text))
                elif part.text:
                    content_text += part.text
                elif part.function_call:
                    tool_calls.append(
                        ToolCall(
                            tool_name=part.function_call.name,
                            tool_call_id=f"gemini_tc_{len(tool_calls)}",
                            arguments=dict(part.function_call.args)
                            if part.function_call.args
                            else {},
                        )
                    )

    usage: dict[str, int] = {}
    if response.usage_metadata:
        um = response.usage_metadata
        if um.prompt_token_count:
            usage["input_tokens"] = um.prompt_token_count
        if um.candidates_token_count:
            usage["output_tokens"] = um.candidates_token_count
        if um.total_token_count:
            usage["total_tokens"] = um.total_token_count

    finish = ""
    if response.candidates:
        finish = str(response.candidates[0].finish_reason or "")

    model_version = getattr(response, "model_version", None) or fallback_model

    return ProviderResponse(
        content=content_text,
        tool_calls=tool_calls,
        stop_reason=_map_stop_reason(finish, bool(tool_calls)),
        reasoning=reasoning_blocks,
        meta=ProviderMeta(
            provider="gemini",
            model=model_version,
            usage=usage,
        ),
    )


def _parse_http_response(data: dict[str, Any], fallback_model: str) -> ProviderResponse:
    """Extract a :class:`ProviderResponse` from a raw GenerateContent JSON body."""
    candidates: list[Any] = data.get("candidates", [])
    if not candidates:
        raise RuntimeError(
            f"Gemini returned an empty 'candidates' array. Full response: {json.dumps(data)[:400]}"
        )

    candidate = candidates[0]
    parts: list[dict[str, Any]] = candidate.get("content", {}).get("parts", [])
    finish_reason: str = candidate.get("finishReason", "")

    content_text = ""
    tool_calls: list[ToolCall] = []
    reasoning_blocks: list[ReasoningBlock] = []

    for part in parts:
        if part.get("thought", False):
            if part.get("text"):
                reasoning_blocks.append(ReasoningBlock(content=part["text"]))
        elif "text" in part:
            content_text += part["text"]
        elif "functionCall" in part:
            fc = part["functionCall"]
            tool_calls.append(
                ToolCall(
                    tool_name=fc.get("name", ""),
                    tool_call_id=f"gemini_tc_{len(tool_calls)}",
                    arguments=fc.get("args", {}),
                )
            )

    usage_meta: dict[str, Any] = data.get("usageMetadata", {})
    usage: dict[str, int] = {}
    if "promptTokenCount" in usage_meta:
        usage["input_tokens"] = usage_meta["promptTokenCount"]
    if "candidatesTokenCount" in usage_meta:
        usage["output_tokens"] = usage_meta["candidatesTokenCount"]
    if "totalTokenCount" in usage_meta:
        usage["total_tokens"] = usage_meta["totalTokenCount"]

    logger.debug(
        "GeminiProvider (HTTP) ← finishReason=%s  content_len=%d  tool_calls=%d  usage=%s",
        finish_reason,
        len(content_text),
        len(tool_calls),
        usage,
    )

    return ProviderResponse(
        content=content_text,
        tool_calls=tool_calls,
        stop_reason=_map_stop_reason(finish_reason, bool(tool_calls)),
        reasoning=reasoning_blocks,
        meta=ProviderMeta(
            provider="gemini",
            model=data.get("modelVersion", fallback_model),
            usage=usage,
        ),
    )


def _map_stop_reason(finish_reason: str, has_tool_calls: bool = False) -> str:
    """Translate a Gemini ``finishReason`` to an internal :class:`StopReason`."""
    if has_tool_calls:
        return StopReason.TOOL_USE.value
    mapping: dict[str, str] = {
        "STOP": StopReason.END_TURN.value,
        "MAX_TOKENS": StopReason.MAX_TOKENS.value,
        "SAFETY": StopReason.ERROR.value,
        "RECITATION": StopReason.ERROR.value,
        "OTHER": StopReason.END_TURN.value,
    }
    upper = finish_reason.upper() if finish_reason else ""
    return mapping.get(upper, StopReason.END_TURN.value)


def _raise_for_status(resp: httpx.Response) -> None:
    """Raise a descriptive ``RuntimeError`` for non-200 HTTP responses."""
    status = resp.status_code
    if status == 200:
        return

    # The response may be a streaming response that hasn't been read yet.
    # Guard against httpx.ResponseNotRead by falling back to an empty body.
    try:
        body: str = resp.text or ""
    except httpx.ResponseNotRead:
        body = ""

    if status in (401, 403):
        raise PermissionError(
            f"Gemini authentication failed (HTTP {status}). "
            "Check your API key in ProviderConfig or GEMINI_API_KEY."
        )
    if status == 429:
        raise RuntimeError("Gemini rate limit exceeded (HTTP 429). Back off and retry.")
    body_lower = body.lower()
    if status == 400 and any(
        phrase in body_lower for phrase in ("context_length", "maximum context", "token limit")
    ):
        raise RuntimeError(f"Gemini context limit exceeded (HTTP 400): {body[:200]}")
    raise RuntimeError(f"Gemini returned HTTP {status}: {body[:400]}")
