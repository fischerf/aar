"""Ollama provider adapter — local model invocation via HTTP API."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from agent.core.config import ProviderConfig
from agent.core.events import ProviderMeta, StopReason, ToolCall
from agent.providers.base import Provider, ProviderResponse, StreamDelta

logger = logging.getLogger(__name__)

_DEFAULT_OLLAMA_URL = "http://localhost:11434"


class OllamaProvider(Provider):
    """Adapter for the Ollama REST API (/api/chat)."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self._base_url = (config.base_url or _DEFAULT_OLLAMA_URL).rstrip("/")
        # Local models can be slow; default read timeout is None (unlimited).
        # Set provider.extra.read_timeout in config to cap it (seconds).
        read_timeout: float | None = config.extra.get("read_timeout", None)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(read_timeout, connect=10.0),
        )
        self._keep_alive = config.extra.get("keep_alive", "5m")

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def supports_reasoning(self) -> bool:
        # Some Ollama models support think mode (e.g. deepseek-r1)
        return self.config.extra.get("supports_reasoning", False)

    @property
    def supports_tools(self) -> bool:
        # Not all Ollama models support tools; opt-in via config
        return self.config.extra.get("supports_tools", True)

    @property
    def supports_vision(self) -> bool:
        # Most current Ollama vision models support image input; opt-out via config.
        return self.config.extra.get("supports_vision", True)

    @property
    def supports_audio(self) -> bool:
        # Ollama does not support audio input as of v0.20.  This returns
        # False unconditionally; the config flag is kept for forward compat.
        return False

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
    ) -> ProviderResponse:
        api_messages = _build_messages(messages, system)

        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": api_messages,
            "stream": False,
            "options": {},
        }

        if self.config.temperature > 0:
            payload["options"]["temperature"] = self.config.temperature
        if self.config.max_tokens:
            payload["options"]["num_predict"] = self.config.max_tokens

        # Structured output (Ollama uses "format" parameter)
        fmt = self.config.response_format
        if fmt == "json":
            payload["format"] = "json"
        elif fmt == "json_schema" and self.config.json_schema:
            payload["format"] = self.config.json_schema

        # Tool support
        if tools and self.supports_tools:
            payload["tools"] = _convert_tools(tools)

        # Thinking mode (Ollama 0.20+)
        if self.supports_reasoning:
            payload["think"] = True

        # Keep-alive
        payload["keep_alive"] = self._keep_alive

        # Extra options (skip known non-option keys)
        _SKIP = {
            "keep_alive",
            "read_timeout",
            "supports_reasoning",
            "supports_tools",
            "supports_vision",
            "supports_audio",
        }
        for k, v in self.config.extra.items():
            if k not in _SKIP:
                payload["options"][k] = v

        resp = await self._client.post("/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()

        message = data.get("message", {})
        content = message.get("content", "")

        # Parse tool calls from Ollama response
        tool_calls: list[ToolCall] = []
        raw_tool_calls = message.get("tool_calls", [])
        for i, tc in enumerate(raw_tool_calls):
            fn = tc.get("function", {})
            tool_calls.append(
                ToolCall(
                    tool_name=fn.get("name", ""),
                    tool_call_id=f"ollama_tc_{i}",
                    arguments=fn.get("arguments", {}),
                )
            )

        # Determine stop reason
        done_reason = data.get("done_reason", "")
        stop_reason = _map_stop_reason(done_reason, bool(tool_calls))

        # Handle think mode — Ollama 0.20+ returns thinking in message.thinking.
        # Always clean raw thinking tokens from content regardless of supports_reasoning,
        # because models like Gemma4 emit them unconditionally (even when thinking is off).
        reasoning_blocks = []
        thinking_text = message.get("thinking", "")
        if thinking_text:
            from agent.core.events import ReasoningBlock

            reasoning_blocks = [ReasoningBlock(content=thinking_text.strip())]
        else:
            from agent.providers._thinking import extract_all

            content, reasoning_blocks = extract_all(content)

        # Usage metadata
        usage: dict[str, int] = {}
        if "prompt_eval_count" in data:
            usage["input_tokens"] = data["prompt_eval_count"]
        if "eval_count" in data:
            usage["output_tokens"] = data["eval_count"]

        meta = ProviderMeta(
            provider="ollama",
            model=data.get("model", self.config.model),
            usage=usage,
        )

        return ProviderResponse(
            content=content,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            reasoning=reasoning_blocks,
            meta=meta,
        )

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
    ) -> AsyncIterator[StreamDelta]:
        """Stream response deltas from Ollama.

        Ollama streams newline-delimited JSON objects from ``/api/chat``
        when ``stream: true``.  Each object has a ``message`` with partial
        ``content`` (and optionally ``thinking``).  The final object carries
        ``done: true`` and usage statistics.
        """
        api_messages = _build_messages(messages, system)

        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": api_messages,
            "stream": True,
            "options": {},
        }

        if self.config.temperature > 0:
            payload["options"]["temperature"] = self.config.temperature
        if self.config.max_tokens:
            payload["options"]["num_predict"] = self.config.max_tokens

        # Structured output
        fmt = self.config.response_format
        if fmt == "json":
            payload["format"] = "json"
        elif fmt == "json_schema" and self.config.json_schema:
            payload["format"] = self.config.json_schema

        if tools and self.supports_tools:
            payload["tools"] = _convert_tools(tools)

        if self.supports_reasoning:
            payload["think"] = True

        payload["keep_alive"] = self._keep_alive

        _SKIP = {
            "keep_alive",
            "read_timeout",
            "supports_reasoning",
            "supports_tools",
            "supports_vision",
            "supports_audio",
        }
        for k, v in self.config.extra.items():
            if k not in _SKIP:
                payload["options"][k] = v

        # Accumulators for tool calls (streamed models may return them in the final chunk)
        tool_acc: list[dict[str, Any]] = []

        from agent.providers._thinking import StreamThinkingRouter

        # Always route thinking tokens — models like Gemma4 emit channel tokens
        # unconditionally even when thinking is disabled.
        router = StreamThinkingRouter()

        async with self._client.stream("POST", "/api/chat", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                message = data.get("message", {})

                # Text content delta
                text = message.get("content", "")

                # Reasoning delta — Ollama 0.20+ extracts this natively
                reasoning = message.get("thinking", "")

                # Fallback: route raw thinking tokens embedded in content
                if text and not reasoning:
                    text, reasoning = router.feed(text)

                # Tool calls (usually only in the final chunk)
                raw_tcs = message.get("tool_calls", [])
                if raw_tcs:
                    tool_acc.extend(raw_tcs)

                is_done = data.get("done", False)

                if text or reasoning:
                    yield StreamDelta(
                        text=text,
                        reasoning_delta=reasoning,
                    )

                if is_done:
                    # Flush any partial-opener buffer
                    clean, leftover_reasoning = router.flush()
                    if clean or leftover_reasoning:
                        yield StreamDelta(text=clean, reasoning_delta=leftover_reasoning)

                if is_done:
                    # Emit accumulated tool calls
                    for i, tc in enumerate(tool_acc):
                        fn = tc.get("function", {})
                        yield StreamDelta(
                            tool_call_delta={
                                "tool_call_id": f"ollama_tc_{i}",
                                "tool_name": fn.get("name", ""),
                                "arguments": fn.get("arguments", {}),
                            }
                        )
                    # Build usage metadata from the final chunk
                    usage: dict[str, int] = {}
                    if "prompt_eval_count" in data:
                        usage["input_tokens"] = data["prompt_eval_count"]
                    if "eval_count" in data:
                        usage["output_tokens"] = data["eval_count"]
                    stream_meta = ProviderMeta(
                        provider="ollama",
                        model=data.get("model", self.config.model),
                        usage=usage,
                    )
                    yield StreamDelta(done=True, meta=stream_meta)
                    return

        # Safety fallback
        yield StreamDelta(done=True)

    async def close(self) -> None:
        await self._client.aclose()


def _build_messages(messages: list[dict[str, Any]], system: str) -> list[dict[str, Any]]:
    """Convert internal messages to Ollama's native ``/api/chat`` format.

    Ollama's native API requires ``content`` to be a **string** — content
    arrays (OpenAI-compatible format) are only accepted on the ``/v1/``
    endpoint.  Images use the top-level ``images`` field (list of raw
    base-64 strings, no ``data:`` prefix).

    **Audio** is not yet supported by Ollama's API (as of v0.20).  Audio
    blocks are dropped with a warning.  The framework-level ``AudioBlock``
    type remains so callers can build multimodal pipelines that will work
    once Ollama adds audio support.
    """
    api_messages: list[dict[str, Any]] = []

    if system:
        api_messages.append({"role": "system", "content": system})

    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if isinstance(content, str):
            api_messages.append({"role": role, "content": content})

        elif isinstance(content, list):
            if role == "assistant":
                # Extract text and tool calls
                text_parts: list[str] = []
                tool_calls: list[dict[str, Any]] = []
                for block in content:
                    if block.get("type") == "text":
                        text_parts.append(block["text"])
                    elif block.get("type") == "tool_use":
                        tool_calls.append(
                            {
                                "function": {
                                    "name": block["name"],
                                    "arguments": block.get("input", {}),
                                }
                            }
                        )
                api_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": " ".join(text_parts) if text_parts else "",
                }
                if tool_calls:
                    api_msg["tool_calls"] = tool_calls
                api_messages.append(api_msg)

            elif role == "user":
                tool_results = [b for b in content if b.get("type") == "tool_result"]
                if tool_results:
                    # Tool results go as individual "tool" role messages
                    for tr in tool_results:
                        api_messages.append(
                            {
                                "role": "tool",
                                "content": tr.get("content", ""),
                            }
                        )
                else:
                    image_blocks = [b for b in content if b.get("type") == "image_url"]
                    audio_blocks = [b for b in content if b.get("type") == "audio"]
                    text_parts = [b.get("text", "") for b in content if b.get("type") == "text"]

                    text = " ".join(t for t in text_parts if t)
                    api_msg = {"role": "user", "content": text}

                    # Extract base-64 payloads into top-level ``images`` list
                    images: list[str] = []
                    for img in image_blocks:
                        url = img.get("image_url", {}).get("url", "")
                        if url.startswith("data:"):
                            try:
                                images.append(url.split(",", 1)[1])
                            except IndexError:
                                pass
                    if images:
                        api_msg["images"] = images

                    if audio_blocks:
                        logger.warning(
                            "audio not yet supported by Ollama — dropping %d audio block(s)",
                            len(audio_blocks),
                        )

                    api_messages.append(api_msg)

    return api_messages


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert internal tool schemas to Ollama format."""
    ollama_tools = []
    for tool in tools:
        ollama_tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
        )
    return ollama_tools


def _map_stop_reason(done_reason: str, has_tool_calls: bool) -> str:
    if has_tool_calls:
        return StopReason.TOOL_USE.value
    mapping = {
        "stop": StopReason.END_TURN,
        "length": StopReason.MAX_TOKENS,
    }
    if done_reason in mapping:
        return mapping[done_reason].value
    return StopReason.END_TURN.value


