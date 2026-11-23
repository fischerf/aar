"""Multimodal input parsing — detect file attachments and build content blocks."""

from __future__ import annotations

import base64
import mimetypes
import re
from pathlib import Path

from agent.core.events import (
    AudioBlock,
    AudioData,
    ContentBlock,
    ImageURL,
    ImageURLBlock,
    TextBlock,
)

# Supported MIME prefixes for each modality
_IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/tiff"}
_AUDIO_MIMES = {"audio/wav", "audio/mpeg", "audio/ogg", "audio/flac", "audio/mp4", "audio/x-wav"}
_VIDEO_MIMES = {"video/mp4", "video/webm", "video/mpeg", "video/quicktime"}

# File extensions as fallback when mimetypes module has no mapping
_EXT_TO_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}

# Pattern to detect file attachments in user input: @path/to/file
_ATTACHMENT_RE = re.compile(r"@((?:[A-Za-z]:)?[\\/]?[^\s@]+\.[a-zA-Z0-9]+)")


def is_binary_file(path: Path, sample: int = 8192) -> bool:
    """Return True if the file looks binary (contains null bytes in the first *sample* bytes)."""
    try:
        chunk = path.read_bytes()[:sample]
    except OSError:
        return True
    return b"\x00" in chunk


def detect_mime(path: Path) -> str:
    """Return the MIME type for a file path, using extension-based lookup."""
    mime = _EXT_TO_MIME.get(path.suffix.lower())
    if mime:
        return mime
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def classify_mime(mime: str) -> str:
    """Classify a MIME type as 'image', 'audio', 'video', or 'unknown'."""
    if mime in _IMAGE_MIMES or mime.startswith("image/"):
        return "image"
    if mime in _AUDIO_MIMES or mime.startswith("audio/"):
        return "audio"
    if mime in _VIDEO_MIMES or mime.startswith("video/"):
        return "video"
    return "unknown"


def file_to_data_uri(path: Path) -> str:
    """Read a file and return a ``data:<mime>;base64,<payload>`` URI."""
    mime = detect_mime(path)
    raw = path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def file_to_content_block(path: Path) -> ContentBlock:
    """Convert a local file to the appropriate content block.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file type is not supported or is video (not yet implemented).
    """
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    mime = detect_mime(path)
    kind = classify_mime(mime)

    if kind == "image":
        data_uri = file_to_data_uri(path)
        return ImageURLBlock(image_url=ImageURL(url=data_uri))

    if kind == "audio":
        data_uri = file_to_data_uri(path)
        fmt = path.suffix.lstrip(".").lower()
        if fmt == "mp3":
            fmt = "mp3"
        return AudioBlock(audio=AudioData(url=data_uri, format=fmt))

    if kind == "video":
        raise ValueError(
            f"Video input is prepared but not yet implemented: {path.name}. "
            "Video support will be added in a future release."
        )

    if is_binary_file(path):
        raise ValueError(f"Unsupported file type ({mime}): {path.name}")
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValueError(f"Could not read file: {path.name}") from exc
    return TextBlock(text=f"[File: {path}]\n{content}")


# ---------------------------------------------------------------------------
# Images returned by tools
# ---------------------------------------------------------------------------

# Formats every vision provider aar talks to accepts (Anthropic, OpenAI,
# Gemini, Ollama). Anything else stays a text placeholder.
TOOL_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


def image_block_from_base64(data: str, mime: str) -> ImageURLBlock:
    """Wrap base-64 image data in an :class:`ImageURLBlock` (``data:`` URI)."""
    return ImageURLBlock(image_url=ImageURL(url=f"data:{mime};base64,{data}"))


def split_data_uri(url: str) -> tuple[str, str] | None:
    """Return ``(mime, base64_payload)`` for a ``data:`` URI, else ``None``."""
    if not url.startswith("data:") or "," not in url:
        return None
    meta, payload = url[5:].split(",", 1)
    if not meta.endswith(";base64"):
        return None
    return meta[: -len(";base64")] or "application/octet-stream", payload


def base64_size(payload: str) -> int:
    """Decoded byte size of a base-64 string (without decoding it)."""
    n = len(payload.rstrip("="))
    return n * 3 // 4


def format_size(nbytes: int) -> str:
    """Human-readable size: ``"812 B"``, ``"120 KB"``, ``"3.4 MB"``."""
    if nbytes < 1024:
        return f"{nbytes} B"
    if nbytes < 1024 * 1024:
        return f"{nbytes // 1024} KB"
    return f"{nbytes / (1024 * 1024):.1f} MB"


def check_tool_image(block: ImageURLBlock, max_bytes: int) -> str | None:
    """Return why a tool image cannot go to a model, or ``None`` if it can.

    Only inline (``data:``) images in :data:`TOOL_IMAGE_MIMES` up to
    *max_bytes* decoded bytes pass. aar does not re-encode images (that would
    need an imaging library), so an oversized one is dropped and the tool
    should be asked for a smaller one.
    """
    parts = split_data_uri(block.image_url.url)
    if parts is None:
        return "not an inline base64 image"
    mime, payload = parts
    if mime not in TOOL_IMAGE_MIMES:
        return f"unsupported image type {mime}"
    size = base64_size(payload)
    if size > max_bytes:
        return f"{format_size(size)} exceeds tools.max_image_bytes ({format_size(max_bytes)})"
    return None


def tool_images_followup_message(
    tool_results: list[dict], intro: str = "Images returned by tool call"
) -> dict | None:
    """Build an OpenAI-style user message carrying the images of *tool_results*.

    OpenAI Chat Completions (and most OpenAI-compatible servers) accept images
    only in ``user`` messages, not in ``tool`` messages, so the images ride in
    one user message placed right after the tool messages of the turn.
    Returns ``None`` when no tool result has images.
    """
    parts: list[dict] = []
    for tr in tool_results:
        images = tr.get("images") or []
        if not images:
            continue
        parts.append({"type": "text", "text": f"[{intro} {tr.get('tool_use_id', '')}]"})
        parts.extend(images)
    if not parts:
        return None
    return {"role": "user", "content": parts}


def parse_multimodal_input(user_input: str) -> str | list[ContentBlock]:
    """Parse user input, extracting ``@file`` attachments into content blocks.

    Syntax: prefix any local file path with ``@`` to attach it.
    Example: ``What's in this image? @photo.jpg``

    Returns a plain string when no attachments are found, or a list of
    :class:`ContentBlock` objects when at least one valid attachment exists.
    """
    matches = list(_ATTACHMENT_RE.finditer(user_input))
    if not matches:
        return user_input

    blocks: list[ContentBlock] = []
    errors: list[str] = []

    # Extract the text (with @file references removed)
    text = _ATTACHMENT_RE.sub("", user_input).strip()

    # Process attachments — media blocks BEFORE text for optimal model performance
    for m in matches:
        raw_path = m.group(1)
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            block = file_to_content_block(path)
            blocks.append(block)
        except (FileNotFoundError, ValueError) as exc:
            errors.append(str(exc))

    if errors and not blocks:
        # All attachments failed — return the original text with error info
        error_text = "; ".join(errors)
        return (
            f"{text}\n[Attachment errors: {error_text}]"
            if text
            else f"[Attachment errors: {error_text}]"
        )

    # Append error info for partially failed attachments
    if errors:
        text += f"\n[Attachment errors: {'; '.join(errors)}]"

    # Add text block last (media before text for Gemma 4 best results)
    if text:
        blocks.append(TextBlock(text=text))

    return blocks
