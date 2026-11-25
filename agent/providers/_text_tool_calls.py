"""Recover tool calls that a model wrote as plain text.

Mistral models (Ministral 3, Mistral Small) call tools as
``[TOOL_CALLS]name[ARGS]{json}``. Ollama turns that into structured
``tool_calls`` - but when the model leaves out the ``[TOOL_CALLS]`` token, the
call arrives as answer text (``list_directory[ARGS]{"path": "..."}``) and the
turn ends without running the tool.

Only answers that consist of nothing but such calls to tools that were offered
are converted, so ordinary text is never touched.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from typing import Any

_MARKER = "[TOOL_CALLS]"
_ARGS = "[ARGS]"
_NAME = re.compile(r"[A-Za-z_][\w.\-]*")


def parse_text_tool_calls(text: str, tool_names: Collection[str]) -> list[dict[str, Any]] | None:
    """Return Ollama-style ``tool_calls`` if *text* is only Mistral-format calls.

    Each call is ``[TOOL_CALLS]?name[ARGS]{...}``; several may follow each other.
    Returns ``None`` when anything else is in the text, a name is not one of
    *tool_names* or the arguments are not a JSON object.
    """
    if _ARGS not in text or not tool_names:
        return None
    decoder = json.JSONDecoder()
    calls: list[dict[str, Any]] = []
    pos = 0
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            break
        if text.startswith(_MARKER, pos):
            pos += len(_MARKER)
            while pos < len(text) and text[pos].isspace():
                pos += 1
        m = _NAME.match(text, pos)
        if not m or m.group(0) not in tool_names or not text.startswith(_ARGS, m.end()):
            return None
        try:
            args, pos = decoder.raw_decode(text, m.end() + len(_ARGS))
        except json.JSONDecodeError:
            return None
        if not isinstance(args, dict):
            return None
        calls.append({"function": {"name": m.group(0), "arguments": args}})
    return calls or None


def may_be_text_tool_call(text: str, tool_names: Collection[str]) -> bool:
    """Whether streamed *text* so far could still turn into a text tool call.

    Used to hold back the start of a streamed answer: ``""``, ``"[TOOL_"``,
    ``"list_dir"`` (prefix of a tool name), ``"list_directory[AR"`` and any
    text after ``name[ARGS]`` keep it held; ordinary words release it at once.
    """
    if not tool_names:
        return False
    s = text.lstrip()
    if _MARKER.startswith(s):
        return True
    if s.startswith(_MARKER):
        s = s[len(_MARKER) :].lstrip()
        if not s:
            return True
    m = _NAME.match(s)
    if not m:
        return False
    name, rest = m.group(0), s[m.end() :]
    if not rest:
        return any(t.startswith(name) for t in tool_names)
    if name not in tool_names:
        return False
    return _ARGS.startswith(rest) or rest.startswith(_ARGS)
