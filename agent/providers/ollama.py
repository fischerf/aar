"""Ollama provider adapter — local model invocation via HTTP API."""

from __future__ import annotations

from typing import Any

import httpx

from agent.core.config import ProviderConfig
from agent.core.events import ProviderMeta, StopReason, ToolCall
from agent.providers.base import Provider, ProviderResponse

_DEFAULT_OLLAMA_URL = "http://localhost:11434"


class OllamaProvider(Provider):
    """Adapter for the Ollama REST API (/api/chat)."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self._base_url = (config.base_url or _DEFAULT_OLLAMA_URL).rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(120.0, connect=10.0),
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

        # Tool support
        if tools and self.supports_tools:
            payload["tools"] = _convert_tools(tools)

        # Keep-alive
        payload["keep_alive"] = self._keep_alive

        # Extra options (skip known non-option keys)
        _SKIP = {"keep_alive", "supports_reasoning", "supports_tools", "supports_vision"}
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

        # Handle think mode — content between <think>...</think> tags
        reasoning_blocks = []
        if self.supports_reasoning and "<think>" in content:
            content, reasoning_blocks = _extract_thinking(content)

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

    async def close(self) -> None:
        await self._client.aclose()


def _build_messages(messages: list[dict[str, Any]], system: str) -> list[dict[str, Any]]:
    """Convert internal messages to Ollama format.

    Multimodal user messages (carrying ``image_url`` content blocks) are
    handled in two complementary ways:

    * A content *array* is produced (Ollama 0.5+ / OpenAI-compatible format).
    * Base-64 images (``data:`` URIs) are **also** placed in the top-level
      ``images`` list for backwards compatibility with Ollama < 0.5 that
      speaks the native ``/api/chat`` wire format.

    HTTP/HTTPS image URLs are passed only via the content array; fetching
    remote URLs inside the adapter would introduce unwanted side-effects and
    latency, and Ollama 0.5+ handles them natively.
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
                    text_parts = [b.get("text", "") for b in content if b.get("type") == "text"]

                    if image_blocks:
                        # Build an OpenAI-compatible content array (Ollama 0.5+)
                        oai_content: list[dict[str, Any]] = []
                        if text_parts:
                            oai_content.append(
                                {"type": "text", "text": " ".join(t for t in text_parts if t)}
                            )
                        for img in image_blocks:
                            oai_content.append(img)  # pass through as-is

                        api_msg = {"role": "user", "content": oai_content}

                        # Legacy Ollama (< 0.5): also populate top-level ``images``
                        # list with raw base-64 payloads extracted from data: URIs.
                        legacy_images: list[str] = []
                        for img in image_blocks:
                            url = img.get("image_url", {}).get("url", "")
                            if url.startswith("data:"):
                                # data:<mime>;base64,<payload>
                                try:
                                    b64 = url.split(",", 1)[1]
                                    legacy_images.append(b64)
                                except IndexError:
                                    pass  # malformed data URI — skip
                        if legacy_images:
                            api_msg["images"] = legacy_images

                        api_messages.append(api_msg)
                    else:
                        # Text-only user message
                        text = " ".join(t for t in text_parts if t)
                        api_messages.append({"role": "user", "content": text})

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


def _extract_thinking(content: str) -> tuple[str, list]:
    """Extract <think>...</think> blocks from content."""
    from agent.core.events import ReasoningBlock

    reasoning = []
    clean = content
    while "<think>" in clean:
        start = clean.index("<think>")
        end = clean.find("</think>")
        if end == -1:
            # Unclosed think tag — treat rest as reasoning
            reasoning.append(ReasoningBlock(content=clean[start + 7 :].strip()))
            clean = clean[:start].strip()
            break
        thinking_text = clean[start + 7 : end].strip()
        if thinking_text:
            reasoning.append(ReasoningBlock(content=thinking_text))
        clean = (clean[:start] + clean[end + 8 :]).strip()
    return clean, reasoning
