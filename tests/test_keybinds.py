"""Tests for agent.transports.keybinds — KeyBind, KeyBinds, and app-level integration."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.core.agent import Agent
from agent.core.config import AgentConfig
from agent.transports.keybinds import _UNRELIABLE_KEYS, KeyBind, KeyBinds

# ---------------------------------------------------------------------------
# KeyBind unit tests
# ---------------------------------------------------------------------------


class TestKeyBind:
    def test_from_dict(self) -> None:
        kb = KeyBind(key="ctrl+s", label="send")
        assert kb.key == "ctrl+s"
        assert kb.label == "send"

    def test_default_label_is_empty(self) -> None:
        kb = KeyBind(key="ctrl+s")
        assert kb.label == ""

    def test_coerce_from_string(self) -> None:
        """A bare string is accepted and becomes the key; label defaults to ''."""
        kb = KeyBind.model_validate("ctrl+m")
        assert kb.key == "ctrl+m"
        assert kb.label == ""

    def test_coerce_from_string_preserves_key(self) -> None:
        kb = KeyBind.model_validate("pageup")
        assert kb.key == "pageup"

    def test_dict_round_trip(self) -> None:
        original = KeyBind(key="ctrl+t", label="theme")
        restored = KeyBind.model_validate(original.model_dump())
        assert restored.key == "ctrl+t"
        assert restored.label == "theme"

    # ------------------------------------------------------------------
    # validate_key
    # ------------------------------------------------------------------

    def test_validate_key_valid_ctrl(self) -> None:
        assert KeyBind(key="ctrl+s", label="send").validate_key() == []

    def test_validate_key_valid_pageup(self) -> None:
        assert KeyBind(key="pageup", label="").validate_key() == []

    def test_validate_key_valid_function_key(self) -> None:
        assert KeyBind(key="f5", label="").validate_key() == []

    def test_validate_key_empty_string(self) -> None:
        warnings = KeyBind(key="", label="").validate_key()
        assert len(warnings) == 1
        assert "empty" in warnings[0]

    def test_validate_key_trailing_plus(self) -> None:
        warnings = KeyBind(key="ctrl+", label="").validate_key()
        assert len(warnings) == 1
        assert "malformed" in warnings[0]

    def test_validate_key_double_plus(self) -> None:
        warnings = KeyBind(key="ctrl++s", label="").validate_key()
        assert len(warnings) == 1
        assert "malformed" in warnings[0]

    def test_validate_key_ctrl_enter(self) -> None:
        warnings = KeyBind(key="ctrl+enter", label="").validate_key()
        assert len(warnings) == 1
        assert "ctrl+enter" in warnings[0]

    def test_validate_key_unreliable_case_insensitive(self) -> None:
        """Unreliable-key check is case-insensitive."""
        warnings = KeyBind(key="Ctrl+Enter", label="").validate_key()
        assert len(warnings) == 1

    def test_validate_key_all_unreliable_keys(self) -> None:
        for bad_key in _UNRELIABLE_KEYS:
            kb = KeyBind(key=bad_key, label="")
            assert kb.validate_key(), f"Expected a warning for unreliable key '{bad_key}'"

    def test_validate_key_shift_enter(self) -> None:
        warnings = KeyBind(key="shift+enter", label="").validate_key()
        assert len(warnings) == 1


# ---------------------------------------------------------------------------
# KeyBinds defaults
# ---------------------------------------------------------------------------


class TestKeyBindsDefaults:
    def test_default_send_key(self) -> None:
        assert KeyBinds().send.key == "ctrl+s"

    def test_default_send_label(self) -> None:
        assert KeyBinds().send.label == "send"

    def test_default_cancel_key(self) -> None:
        assert KeyBinds().cancel.key == "ctrl+x"

    def test_default_cancel_label(self) -> None:
        assert KeyBinds().cancel.label == "cancel"

    def test_default_cycle_theme(self) -> None:
        assert KeyBinds().cycle_theme.key == "ctrl+t"

    def test_default_toggle_thinking(self) -> None:
        assert KeyBinds().toggle_thinking.key == "ctrl+k"

    def test_default_clear_screen(self) -> None:
        assert KeyBinds().clear_screen.key == "ctrl+l"

    def test_default_terminal(self) -> None:
        assert KeyBinds().terminal.key == "ctrl+p"

    def test_default_history_prev(self) -> None:
        assert KeyBinds().history_prev.key == "ctrl+up"

    def test_default_history_next(self) -> None:
        assert KeyBinds().history_next.key == "ctrl+down"

    def test_default_scroll_up(self) -> None:
        assert KeyBinds().scroll_up.key == "pageup"

    def test_default_scroll_down(self) -> None:
        assert KeyBinds().scroll_down.key == "pagedown"

    def test_all_defaults_have_non_empty_labels(self) -> None:
        kb = KeyBinds()
        for field_name in KeyBinds.model_fields:
            value = getattr(kb, field_name)
            assert isinstance(value, KeyBind), f"{field_name} is not a KeyBind"
            assert value.label, f"Default label for '{field_name}' is empty"


# ---------------------------------------------------------------------------
# KeyBinds — construction with strings (coercion)
# ---------------------------------------------------------------------------


class TestKeyBindsStringCoercion:
    def test_string_coercion_for_send(self) -> None:
        kb = KeyBinds(send="ctrl+m")
        assert kb.send.key == "ctrl+m"
        assert kb.send.label == ""  # label not provided → empty

    def test_string_coercion_for_terminal(self) -> None:
        kb = KeyBinds(terminal="ctrl+g")
        assert kb.terminal.key == "ctrl+g"

    def test_dict_construction_preserves_label(self) -> None:
        kb = KeyBinds(send={"key": "ctrl+m", "label": "submit"})
        assert kb.send.key == "ctrl+m"
        assert kb.send.label == "submit"

    def test_keybind_object_construction(self) -> None:
        kb = KeyBinds(cancel=KeyBind(key="ctrl+b", label="abort"))
        assert kb.cancel.key == "ctrl+b"
        assert kb.cancel.label == "abort"

    def test_partial_override_leaves_others_at_default(self) -> None:
        kb = KeyBinds(send="ctrl+m")
        assert kb.cancel.key == "ctrl+x"
        assert kb.cycle_theme.key == "ctrl+t"


# ---------------------------------------------------------------------------
# KeyBinds.load()
# ---------------------------------------------------------------------------


class TestKeyBindsLoad:
    def test_load_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        kb = KeyBinds.load(tmp_path / "nonexistent.json")
        assert kb.send.key == "ctrl+s"

    def test_load_string_value(self, tmp_path: Path) -> None:
        path = tmp_path / "keybinds.json"
        path.write_text(json.dumps({"send": "ctrl+m"}), encoding="utf-8")
        kb = KeyBinds.load(path)
        assert kb.send.key == "ctrl+m"
        assert kb.cancel.key == "ctrl+x"  # unchanged default

    def test_load_full_object_value(self, tmp_path: Path) -> None:
        path = tmp_path / "keybinds.json"
        path.write_text(
            json.dumps({"send": {"key": "ctrl+m", "label": "submit"}}),
            encoding="utf-8",
        )
        kb = KeyBinds.load(path)
        assert kb.send.key == "ctrl+m"
        assert kb.send.label == "submit"

    def test_load_multiple_overrides(self, tmp_path: Path) -> None:
        path = tmp_path / "keybinds.json"
        path.write_text(
            json.dumps({"send": "ctrl+m", "cancel": {"key": "ctrl+b", "label": "abort"}}),
            encoding="utf-8",
        )
        kb = KeyBinds.load(path)
        assert kb.send.key == "ctrl+m"
        assert kb.cancel.key == "ctrl+b"
        assert kb.cancel.label == "abort"
        assert kb.cycle_theme.key == "ctrl+t"  # default intact

    def test_load_invalid_json_falls_back_to_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "keybinds.json"
        path.write_text("not { valid } json!!!", encoding="utf-8")
        kb = KeyBinds.load(path)
        assert kb.send.key == "ctrl+s"

    def test_load_invalid_json_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "keybinds.json"
        path.write_text("%%%", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="agent.transports.keybinds"):
            KeyBinds.load(path)
        assert caplog.records, "Expected at least one log record"
        assert any("keybinds" in r.message.lower() for r in caplog.records)

    def test_load_unknown_fields_are_ignored(self, tmp_path: Path) -> None:
        """Extra unknown JSON fields should not crash the loader."""
        path = tmp_path / "keybinds.json"
        path.write_text(json.dumps({"send": "ctrl+m", "unknown_field": "value"}), encoding="utf-8")
        # Pydantic v2 ignores extra fields by default; should not raise
        kb = KeyBinds.load(path)
        assert kb.send.key == "ctrl+m"

    def test_load_default_path_used_when_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When path=None, _USER_KEYBINDS is used (monkeypatched to tmp_path)."""
        import agent.transports.keybinds as kb_module

        fake_path = tmp_path / "keybinds.json"
        fake_path.write_text(json.dumps({"send": "ctrl+r"}), encoding="utf-8")
        monkeypatch.setattr(kb_module, "_USER_KEYBINDS", fake_path)
        kb = KeyBinds.load()
        assert kb.send.key == "ctrl+r"


# ---------------------------------------------------------------------------
# KeyBinds.validate_all()
# ---------------------------------------------------------------------------


class TestKeyBindsValidateAll:
    def test_no_warnings_for_all_defaults(self) -> None:
        assert KeyBinds().validate_all() == []

    def test_warning_for_unreliable_key_in_send(self) -> None:
        kb = KeyBinds(send=KeyBind(key="ctrl+enter", label="send"))
        problems = kb.validate_all()
        assert len(problems) == 1
        field, msg = problems[0]
        assert field == "send"
        assert "ctrl+enter" in msg

    def test_warning_for_empty_key(self) -> None:
        kb = KeyBinds(send=KeyBind(key="", label=""))
        problems = kb.validate_all()
        fields = [f for f, _ in problems]
        assert "send" in fields

    def test_warning_for_malformed_key(self) -> None:
        kb = KeyBinds(cancel=KeyBind(key="ctrl+", label=""))
        problems = kb.validate_all()
        fields = [f for f, _ in problems]
        assert "cancel" in fields

    def test_multiple_bad_keys_reported(self) -> None:
        kb = KeyBinds(
            send=KeyBind(key="ctrl+enter", label=""),
            cancel=KeyBind(key="alt+return", label=""),
        )
        problems = kb.validate_all()
        fields = [f for f, _ in problems]
        assert "send" in fields
        assert "cancel" in fields

    def test_validate_all_covers_all_fields(self) -> None:
        """validate_all() inspects every KeyBind field in the model."""
        all_fields = set(KeyBinds.model_fields.keys())
        # Make every field bad; all should appear in problems
        bad_bind = KeyBind(key="ctrl+enter", label="")
        kb = KeyBinds(**{f: bad_bind for f in all_fields})  # type: ignore[arg-type]
        problems = kb.validate_all()
        reported_fields = {f for f, _ in problems}
        assert reported_fields == all_fields


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_agent() -> Agent:
    """Create a minimal Agent with a stub provider for testing."""
    config = AgentConfig()
    provider = MagicMock()
    provider.name = "test"
    provider.model = "test-model"
    provider.supports_audio = False
    provider.supports_reasoning = False
    provider.supports_vision = False
    registry = MagicMock()
    registry.names.return_value = []
    registry.list_tools.return_value = []
    agent = MagicMock(spec=Agent)
    agent.config = config
    agent.provider = provider
    agent.registry = registry
    agent.on_event = MagicMock()
    agent.run = AsyncMock()
    return agent


# ---------------------------------------------------------------------------
# App-level keybind warning integration
# ---------------------------------------------------------------------------


class TestAppKeybindWarning:
    """Verify that AarFixedApp.on_mount() logs warnings for invalid keybinds."""

    @pytest.mark.asyncio
    async def test_app_warns_on_mount_for_bad_keybind(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from agent.transports.tui_fixed import AarFixedApp

        bad_kb = KeyBinds(send=KeyBind(key="ctrl+enter", label="send"))
        app = AarFixedApp(agent=_make_mock_agent(), config=AgentConfig(), keybinds=bad_kb)

        with caplog.at_level(logging.WARNING):
            async with app.run_test(size=(120, 40)):
                pass

        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("ctrl+enter" in msg for msg in warning_messages), (
            f"Expected a warning mentioning 'ctrl+enter'; got: {warning_messages}"
        )

    @pytest.mark.asyncio
    async def test_app_no_warnings_for_default_keybinds(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from agent.transports.tui_fixed import AarFixedApp

        app = AarFixedApp(agent=_make_mock_agent(), config=AgentConfig())

        with caplog.at_level(logging.WARNING):
            async with app.run_test(size=(120, 40)):
                pass

        keybind_warnings = [
            r for r in caplog.records if r.levelno >= logging.WARNING and "keybind" in r.message
        ]
        assert keybind_warnings == [], f"Unexpected keybind warnings: {keybind_warnings}"


# ---------------------------------------------------------------------------
# HistoryTextArea respects configured send / history keys
# ---------------------------------------------------------------------------


class TestHistoryTextAreaKeys:
    """Unit tests for HistoryTextArea key configuration (no Textual app needed)."""

    def _make_textarea(
        self,
        send_key: str = "ctrl+s",
        history_prev_key: str = "ctrl+up",
        history_next_key: str = "ctrl+down",
    ):
        from agent.transports.tui_widgets.input import HistoryTextArea

        ta = HistoryTextArea.__new__(HistoryTextArea)
        ta._send_key = send_key
        ta._history_prev_key = history_prev_key
        ta._history_next_key = history_next_key
        ta._history = []
        ta._history_index = -1
        ta._draft = ""
        return ta

    def test_default_send_key(self) -> None:
        ta = self._make_textarea()
        assert ta._send_key == "ctrl+s"

    def test_custom_send_key(self) -> None:
        ta = self._make_textarea(send_key="ctrl+m")
        assert ta._send_key == "ctrl+m"

    def test_custom_history_keys(self) -> None:
        ta = self._make_textarea(history_prev_key="alt+up", history_next_key="alt+down")
        assert ta._history_prev_key == "alt+up"
        assert ta._history_next_key == "alt+down"

    def test_history_navigation_state(self) -> None:
        ta = self._make_textarea()
        ta._history = ["first", "second"]
        ta._history_index = 1
        ta._draft = "draft"
        assert ta._history[ta._history_index] == "second"

    @pytest.mark.asyncio
    async def test_app_passes_configured_send_key_to_textarea(self) -> None:
        """AarFixedApp composes HistoryTextArea with send_key from keybinds."""
        from agent.transports.tui_fixed import AarFixedApp
        from agent.transports.tui_widgets.input import HistoryTextArea

        kb = KeyBinds(send=KeyBind(key="ctrl+m", label="send"))
        app = AarFixedApp(agent=_make_mock_agent(), config=AgentConfig(), keybinds=kb)

        async with app.run_test(size=(120, 40)):
            ta = app.query_one("#user-input", HistoryTextArea)
            assert ta._send_key == "ctrl+m"
