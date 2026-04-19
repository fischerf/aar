"""Multimodal support tests — content blocks, input parsing, provider conversion."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from agent.core.events import (
    AudioBlock,
    AudioData,
    ContentBlock,
    ImageURL,
    ImageURLBlock,
    TextBlock,
    UserMessage,
    VideoBlock,
    VideoData,
)
from agent.core.multimodal import (
    classify_mime,
    detect_mime,
    file_to_content_block,
    file_to_data_uri,
    parse_multimodal_input,
)


# ---------------------------------------------------------------------------
# Content block models — audio & video
# ---------------------------------------------------------------------------


class TestAudioBlock:
    def test_defaults(self):
        b = AudioBlock(audio=AudioData(url="data:audio/wav;base64,AAAA"))
        assert b.type == "audio"
        assert b.audio.url.startswith("data:audio/wav")
        assert b.audio.format == ""

    def test_with_format(self):
        b = AudioBlock(audio=AudioData(url="https://example.com/clip.mp3", format="mp3"))
        assert b.audio.format == "mp3"

    def test_model_dump_excludes_none(self):
        b = AudioBlock(audio=AudioData(url="https://example.com/clip.wav"))
        d = b.model_dump(exclude_none=True)
        assert d["type"] == "audio"
        assert d["audio"]["url"] == "https://example.com/clip.wav"

    def test_discriminated_union(self):
        ta: TypeAdapter[ContentBlock] = TypeAdapter(ContentBlock)
        block = ta.validate_python(
            {"type": "audio", "audio": {"url": "data:audio/wav;base64,AAAA"}}
        )
        assert isinstance(block, AudioBlock)


class TestVideoBlock:
    def test_defaults(self):
        b = VideoBlock(video=VideoData(url="https://example.com/clip.mp4"))
        assert b.type == "video"
        assert b.video.format == ""

    def test_with_format(self):
        b = VideoBlock(video=VideoData(url="data:video/mp4;base64,BBBB", format="mp4"))
        assert b.video.format == "mp4"

    def test_discriminated_union(self):
        ta: TypeAdapter[ContentBlock] = TypeAdapter(ContentBlock)
        block = ta.validate_python({"type": "video", "video": {"url": "https://example.com/v.mp4"}})
        assert isinstance(block, VideoBlock)


class TestUserMessageMultimodalAudio:
    def test_audio_parts_is_multimodal(self):
        parts: list[ContentBlock] = [
            AudioBlock(audio=AudioData(url="data:audio/wav;base64,AAAA", format="wav")),
            TextBlock(text="What is this sound?"),
        ]
        msg = UserMessage(content="What is this sound?", parts=parts)
        assert msg.is_multimodal
        assert len(msg.parts) == 2

    def test_mixed_image_audio_text(self):
        parts: list[ContentBlock] = [
            ImageURLBlock(image_url=ImageURL(url="https://example.com/img.png")),
            AudioBlock(audio=AudioData(url="data:audio/wav;base64,AAAA")),
            TextBlock(text="Describe both"),
        ]
        msg = UserMessage(content="Describe both", parts=parts)
        assert msg.is_multimodal
        assert len(msg.parts) == 3


# ---------------------------------------------------------------------------
# MIME detection and classification
# ---------------------------------------------------------------------------


class TestMimeDetection:
    @pytest.mark.parametrize(
        "suffix, expected",
        [
            (".png", "image/png"),
            (".jpg", "image/jpeg"),
            (".jpeg", "image/jpeg"),
            (".gif", "image/gif"),
            (".webp", "image/webp"),
            (".wav", "audio/wav"),
            (".mp3", "audio/mpeg"),
            (".ogg", "audio/ogg"),
            (".flac", "audio/flac"),
            (".m4a", "audio/mp4"),
            (".mp4", "video/mp4"),
            (".webm", "video/webm"),
            (".mov", "video/quicktime"),
        ],
    )
    def test_detect_mime(self, suffix: str, expected: str):
        assert detect_mime(Path(f"file{suffix}")) == expected

    @pytest.mark.parametrize(
        "mime, expected",
        [
            ("image/png", "image"),
            ("image/jpeg", "image"),
            ("audio/wav", "audio"),
            ("audio/mpeg", "audio"),
            ("video/mp4", "video"),
            ("video/webm", "video"),
            ("application/pdf", "unknown"),
        ],
    )
    def test_classify_mime(self, mime: str, expected: str):
        assert classify_mime(mime) == expected


# ---------------------------------------------------------------------------
# File-to-content-block conversion
# ---------------------------------------------------------------------------


class TestFileToContentBlock:
    def test_image_file(self, tmp_path: Path):
        img = tmp_path / "test.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        block = file_to_content_block(img)
        assert isinstance(block, ImageURLBlock)
        assert block.image_url.url.startswith("data:image/png;base64,")

    def test_audio_file(self, tmp_path: Path):
        wav = tmp_path / "test.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 100)
        block = file_to_content_block(wav)
        assert isinstance(block, AudioBlock)
        assert block.audio.url.startswith("data:audio/wav;base64,")
        assert block.audio.format == "wav"

    def test_video_file_raises(self, tmp_path: Path):
        vid = tmp_path / "test.mp4"
        vid.write_bytes(b"\x00" * 100)
        with pytest.raises(ValueError, match="not yet implemented"):
            file_to_content_block(vid)

    def test_unsupported_file_raises(self, tmp_path: Path):
        # Binary file with an unknown extension should raise, not be read as text
        f = tmp_path / "test.xyz"
        f.write_bytes(b"\x00\x01\x02\x03" * 100)
        with pytest.raises(ValueError, match="Unsupported file type"):
            file_to_content_block(f)

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            file_to_content_block(tmp_path / "nonexistent.png")

    def test_data_uri_round_trip(self, tmp_path: Path):
        content = b"fake audio data for testing"
        wav = tmp_path / "round.wav"
        wav.write_bytes(content)
        uri = file_to_data_uri(wav)
        assert uri.startswith("data:audio/wav;base64,")
        b64_part = uri.split(",", 1)[1]
        assert base64.b64decode(b64_part) == content


# ---------------------------------------------------------------------------
# Multimodal input parsing (@file syntax)
# ---------------------------------------------------------------------------


class TestParseMultimodalInput:
    def test_plain_text_returns_string(self):
        result = parse_multimodal_input("Hello world")
        assert result == "Hello world"
        assert isinstance(result, str)

    def test_no_at_sign_returns_string(self):
        result = parse_multimodal_input("tell me about cats")
        assert isinstance(result, str)

    def test_image_attachment(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
        result = parse_multimodal_input(f"What is this? @{img}")
        assert isinstance(result, list)
        types = [type(b).__name__ for b in result]
        assert "ImageURLBlock" in types
        assert "TextBlock" in types

    def test_audio_attachment(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        wav = tmp_path / "clip.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 100)
        result = parse_multimodal_input(f"What sound is this? @{wav}")
        assert isinstance(result, list)
        types = [type(b).__name__ for b in result]
        assert "AudioBlock" in types
        assert "TextBlock" in types

    def test_media_before_text(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        img = tmp_path / "pic.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        result = parse_multimodal_input(f"Describe @{img}")
        assert isinstance(result, list)
        # Media blocks should come before text blocks
        text_idx = next(i for i, b in enumerate(result) if isinstance(b, TextBlock))
        media_idx = next(i for i, b in enumerate(result) if isinstance(b, ImageURLBlock))
        assert media_idx < text_idx

    def test_missing_file_returns_error_string(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = parse_multimodal_input("Look at @nonexistent.png")
        assert isinstance(result, str)
        assert "Attachment errors" in result

    def test_video_attachment_returns_error(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"\x00" * 100)
        result = parse_multimodal_input(f"What is this? @{vid}")
        assert isinstance(result, str)
        assert "not yet implemented" in result

    def test_multiple_attachments(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        img = tmp_path / "a.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        wav = tmp_path / "b.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 100)
        result = parse_multimodal_input(f"Compare these @{img} @{wav}")
        assert isinstance(result, list)
        types = [type(b).__name__ for b in result]
        assert "ImageURLBlock" in types
        assert "AudioBlock" in types
        assert "TextBlock" in types


# ---------------------------------------------------------------------------
# Ollama provider — audio block conversion
# ---------------------------------------------------------------------------


class TestOllamaAudioHandling:
    """Audio blocks are dropped by Ollama (no API support as of v0.20)."""

    def test_audio_blocks_dropped_with_warning(self, caplog):
        """Audio blocks are removed and a warning is logged."""
        from agent.providers.ollama import _build_messages

        b64 = base64.b64encode(b"fake audio").decode()
        msgs = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "audio",
                        "audio": {"url": f"data:audio/wav;base64,{b64}", "format": "wav"},
                    },
                    {"type": "text", "text": "What sound is this?"},
                ],
            }
        ]
        import logging

        with caplog.at_level(logging.WARNING, logger="agent.providers.ollama"):
            result = _build_messages(msgs, "")
        assert len(result) == 1
        assert result[0]["content"] == "What sound is this?"
        assert "audio" not in result[0]
        assert "not yet supported" in caplog.text

    def test_mixed_image_and_audio_keeps_image_drops_audio(self, caplog):
        """Image blocks pass through; audio blocks are dropped."""
        from agent.providers.ollama import _build_messages

        b64 = base64.b64encode(b"fake").decode()
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {
                        "type": "audio",
                        "audio": {"url": f"data:audio/wav;base64,{b64}", "format": "wav"},
                    },
                    {"type": "text", "text": "Describe both"},
                ],
            }
        ]
        import logging

        with caplog.at_level(logging.WARNING, logger="agent.providers.ollama"):
            result = _build_messages(msgs, "")
        assert result[0]["content"] == "Describe both"
        assert result[0]["images"] == [b64]
        assert "audio" not in result[0]  # audio dropped
        assert "not yet supported" in caplog.text

    def test_image_only_native_format(self):
        """Image blocks produce top-level ``images`` list (existing behavior preserved)."""
        from agent.providers.ollama import _build_messages

        b64 = base64.b64encode(b"fake png").decode()
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": "What is this?"},
                ],
            }
        ]
        result = _build_messages(msgs, "")
        assert result[0]["content"] == "What is this?"
        assert result[0]["images"] == [b64]
        assert "audio" not in result[0]

    def test_supports_audio_returns_false(self):
        """OllamaProvider.supports_audio is always False (no Ollama API support)."""
        from agent.core.config import ProviderConfig
        from agent.providers.ollama import OllamaProvider

        provider = OllamaProvider(
            ProviderConfig(name="ollama", model="gemma4:e4b", extra={"supports_audio": True})
        )
        assert provider.supports_audio is False


# ---------------------------------------------------------------------------
# Session — multimodal message conversion with audio
# ---------------------------------------------------------------------------


class TestSessionMultimodalAudio:
    def test_audio_message_to_messages(self):
        from agent.core.session import Session

        session = Session()
        parts: list[ContentBlock] = [
            AudioBlock(audio=AudioData(url="data:audio/wav;base64,AAAA", format="wav")),
            TextBlock(text="What is this?"),
        ]
        session.add_user_message(parts)
        msgs = session.to_messages()
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        content = msgs[0]["content"]
        assert isinstance(content, list)
        assert len(content) == 2
        assert content[0]["type"] == "audio"
        assert content[1]["type"] == "text"
