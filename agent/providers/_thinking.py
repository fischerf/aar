"""Shared reasoning/thinking extraction helpers used by all provider adapters.

Supported formats
-----------------
* ``<think>...</think>``  — DeepSeek R1, Qwen3, and most OpenAI-compat models
* ``<|channel>thought\\n…<channel|>``  — Gemma4

Both formats are handled for:
* Complete (non-streaming) responses via :func:`extract_think_tags` /
  :func:`extract_channel_tokens`
* Streaming responses via :class:`StreamThinkingRouter` (stateful, chunk-safe)
* OpenAI-compat/OpenRouter API response objects via
  :func:`extract_reasoning_content`
"""

from __future__ import annotations

from typing import Any

# Opener / closer pairs for streaming detection (order matters — most specific first)
_STREAM_PATTERNS: list[tuple[str, str]] = [
    ("<|channel>thought\n", "<channel|>"),
    ("<think>", "</think>"),
]

_CHAN_OPEN = "<|channel>thought"
_CHAN_CLOSE = "<channel|>"
_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"

# Longest possible partial-opener that needs to be buffered
_MAX_PARTIAL = max(len(op) for op, _ in _STREAM_PATTERNS) - 1


# ---------------------------------------------------------------------------
# Non-streaming helpers
# ---------------------------------------------------------------------------


def extract_think_tags(content: str) -> tuple[str, list]:
    """Strip ``<think>...</think>`` blocks from *content*.

    Returns ``(cleaned_content, reasoning_blocks)``.
    Unclosed tags are treated as reasoning that extends to end of string.
    """
    from agent.core.events import ReasoningBlock

    reasoning: list[ReasoningBlock] = []
    clean = content
    while _THINK_OPEN in clean:
        start = clean.index(_THINK_OPEN)
        end = clean.find(_THINK_CLOSE, start)
        if end == -1:
            text = clean[start + len(_THINK_OPEN) :].strip()
            if text:
                reasoning.append(ReasoningBlock(content=text))
            clean = clean[:start].strip()
            break
        text = clean[start + len(_THINK_OPEN) : end].strip()
        if text:
            reasoning.append(ReasoningBlock(content=text))
        clean = (clean[:start] + clean[end + len(_THINK_CLOSE) :]).strip()
    return clean, reasoning


def extract_channel_tokens(content: str) -> tuple[str, list]:
    """Strip ``<|channel>thought\\n…<channel|>`` blocks from *content* (Gemma4).

    Returns ``(cleaned_content, reasoning_blocks)``.
    Empty channel blocks (thinking disabled) are silently dropped.
    Unclosed blocks are treated as reasoning to end of string.
    """
    from agent.core.events import ReasoningBlock

    reasoning: list[ReasoningBlock] = []
    clean = content
    while _CHAN_OPEN in clean:
        start = clean.index(_CHAN_OPEN)
        content_start = start + len(_CHAN_OPEN)
        # skip the mandatory newline that follows the opener token
        if content_start < len(clean) and clean[content_start] == "\n":
            content_start += 1
        end = clean.find(_CHAN_CLOSE, content_start)
        if end == -1:
            text = clean[content_start:].strip()
            if text:
                reasoning.append(ReasoningBlock(content=text))
            clean = clean[:start].strip()
            break
        text = clean[content_start:end].strip()
        if text:  # empty block → thinking disabled; drop silently
            reasoning.append(ReasoningBlock(content=text))
        clean = (clean[:start] + clean[end + len(_CHAN_CLOSE) :]).strip()
    return clean, reasoning


def extract_all(content: str) -> tuple[str, list]:
    """Apply both :func:`extract_channel_tokens` and :func:`extract_think_tags`.

    Returns ``(cleaned_content, combined_reasoning_blocks)``.
    Channel tokens are processed first (more specific), then think tags.
    """
    content, chan_blocks = extract_channel_tokens(content)
    content, think_blocks = extract_think_tags(content)
    return content, chan_blocks + think_blocks


def extract_reasoning_content(message: Any) -> list:
    """Extract reasoning from OpenAI-compat/OpenRouter API response objects.

    Checks for (in order):
    * ``message.reasoning_details`` — OpenRouter normalized array
    * ``message.reasoning_content`` — Some OpenAI-compat providers (str field)
    * Dict equivalents of the above

    Returns a list of :class:`~agent.core.events.ReasoningBlock` (empty if none found).
    """
    from agent.core.events import ReasoningBlock

    # Attribute-style (SDK objects)
    if hasattr(message, "reasoning_details") and message.reasoning_details:
        parts: list[str] = []
        for item in message.reasoning_details:
            text = getattr(item, "summary", None) or getattr(item, "text", None)
            if text:
                parts.append(text)
        if parts:
            return [ReasoningBlock(content="\n".join(parts))]

    if hasattr(message, "reasoning_content") and message.reasoning_content:
        return [ReasoningBlock(content=message.reasoning_content.strip())]

    # Dict-style (raw JSON response)
    if isinstance(message, dict):
        if rc := message.get("reasoning_content", ""):
            return [ReasoningBlock(content=rc.strip())]
        if rd := message.get("reasoning_details"):
            parts = []
            for item in rd:
                if isinstance(item, dict):
                    text = item.get("summary") or item.get("text", "")
                    if text:
                        parts.append(text)
            if parts:
                return [ReasoningBlock(content="\n".join(parts))]

    return []


# ---------------------------------------------------------------------------
# Streaming router
# ---------------------------------------------------------------------------


class StreamThinkingRouter:
    """Stateful per-stream router that splits thinking tokens from regular text.

    Feed each streaming chunk through :meth:`feed`; it returns
    ``(clean_text, reasoning_delta)`` for that chunk.  Both values may be
    empty strings.  Call :meth:`flush` at end-of-stream to drain any buffered
    partial state.

    Supports:
    * ``<think>`` / ``</think>``
    * ``<|channel>thought\\n`` / ``<channel|>``
    """

    def __init__(self) -> None:
        self._in_thinking = False
        self._closer = ""
        self._buf = ""  # partial-opener buffer

    def feed(self, chunk: str) -> tuple[str, str]:
        """Process one streaming chunk.

        Returns ``(clean_text, reasoning_delta)``.
        """
        if not chunk:
            return "", ""
        if self._in_thinking:
            return self._process_thinking(chunk)
        return self._process_normal(chunk)

    def flush(self) -> tuple[str, str]:
        """Drain state at end of stream.

        If we're still inside a thinking block (unclosed tag), the remaining
        buffer is returned as reasoning.  If we have a partial opener buffer
        that never completed, it's returned as clean text.
        """
        if self._in_thinking:
            buf, self._buf = self._buf, ""
            self._in_thinking = False
            self._closer = ""
            return "", buf
        buf, self._buf = self._buf, ""
        return buf, ""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _process_thinking(self, chunk: str) -> tuple[str, str]:
        idx = chunk.find(self._closer)
        if idx == -1:
            return "", chunk
        # Found the closer
        reasoning = chunk[:idx]
        remainder = chunk[idx + len(self._closer) :]
        self._in_thinking = False
        self._closer = ""
        # Process any text after the closer as normal
        clean_after, more_reasoning = self._process_normal(remainder)
        return clean_after, reasoning + more_reasoning

    def _process_normal(self, chunk: str) -> tuple[str, str]:
        combined = self._buf + chunk
        self._buf = ""

        # Find the earliest opener in combined
        earliest_idx = len(combined)
        earliest_opener: str | None = None
        earliest_closer: str | None = None
        for opener, closer in _STREAM_PATTERNS:
            idx = combined.find(opener)
            if idx != -1 and idx < earliest_idx:
                earliest_idx = idx
                earliest_opener = opener
                earliest_closer = closer

        if earliest_opener is not None:
            clean = combined[:earliest_idx]
            self._in_thinking = True
            self._closer = earliest_closer  # type: ignore[assignment]
            after_opener = combined[earliest_idx + len(earliest_opener) :]
            thinking_clean, thinking_delta = self._process_thinking(after_opener)
            return clean + thinking_clean, thinking_delta

        # Check if the tail of combined could be a partial opener
        for length in range(min(_MAX_PARTIAL, len(combined)), 0, -1):
            suffix = combined[-length:]
            if any(op.startswith(suffix) for op, _ in _STREAM_PATTERNS):
                self._buf = suffix
                return combined[:-length], ""

        return combined, ""
