"""CLI command tests — all 7 commands, unit (mock) + live (Ollama) variants.

Unit tests use MockProvider and never touch a real LLM.
Live tests are marked @pytest.mark.live and skipped unless --live is passed.

Run live tests:
    pytest tests/test_cli.py -m live --live
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from agent.core.config import AgentConfig, ProviderConfig
from agent.core.session import Session
from agent.memory.session_store import SessionStore
from agent.transports.cli import app
from tests.conftest import MockProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

runner = CliRunner()


def _make_mock_provider(*texts: str) -> MockProvider:
    """Create a MockProvider with pre-queued text responses."""
    p = MockProvider()
    for text in texts:
        p.enqueue_text(text)
    return p


def _mock_config(tmp_path: Path, provider_name: str = "mock") -> AgentConfig:
    """Build an AgentConfig that points sessions at a temp dir."""
    return AgentConfig(
        provider=ProviderConfig(name=provider_name, model="mock-1"),
        session_dir=tmp_path,
        max_steps=5,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_session_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


# ---------------------------------------------------------------------------
# `agent tools`
# ---------------------------------------------------------------------------


class TestToolsCommand:
    def test_lists_all_builtin_tools(self):
        result = runner.invoke(app, ["tools"])
        assert result.exit_code == 0
        for name in ["read_file", "write_file", "edit_file", "list_directory", "bash"]:
            assert name in result.output

    def test_shows_side_effects(self):
        result = runner.invoke(app, ["tools"])
        assert result.exit_code == 0
        assert "read" in result.output
        assert "write" in result.output
        assert "execute" in result.output

    def test_shows_descriptions(self):
        result = runner.invoke(app, ["tools"])
        assert result.exit_code == 0
        assert "Read a file" in result.output


# ---------------------------------------------------------------------------
# `agent sessions`
# ---------------------------------------------------------------------------


class TestSessionsCommand:
    def test_no_sessions(self, tmp_session_dir):
        with patch("agent.transports.cli.SessionStore") as MockStore:
            instance = MockStore.return_value
            instance.list_sessions.return_value = []
            result = runner.invoke(app, ["sessions"])
        assert result.exit_code == 0
        assert "No saved sessions" in result.output

    def test_lists_session_ids(self, tmp_session_dir):
        fake_ids = ["abc123def456", "xyz789uvw012"]
        with patch("agent.transports.cli.SessionStore") as MockStore:
            instance = MockStore.return_value
            instance.list_sessions.return_value = fake_ids
            result = runner.invoke(app, ["sessions"])
        assert result.exit_code == 0
        for sid in fake_ids:
            assert sid in result.output

    def test_real_store_with_saved_sessions(self, tmp_session_dir):
        store = SessionStore(tmp_session_dir)
        s1, s2 = Session(), Session()
        store.save(s1)
        store.save(s2)

        with patch("agent.transports.cli.SessionStore", return_value=store):
            result = runner.invoke(app, ["sessions"])

        assert result.exit_code == 0
        assert s1.session_id in result.output
        assert s2.session_id in result.output


# ---------------------------------------------------------------------------
# `aar prompt`
# ---------------------------------------------------------------------------


class TestPromptCommand:
    def test_prints_base_prompt_content(self):
        """Default invocation shows the assembled system prompt inside a Rich panel."""
        result = runner.invoke(app, ["prompt"])
        assert result.exit_code == 0
        assert "You are a helpful assistant" in result.output
        assert "Working directory" in result.output

    def test_panel_title_present_by_default(self):
        """The Rich panel title 'System Prompt' is shown in normal mode."""
        result = runner.invoke(app, ["prompt"])
        assert result.exit_code == 0
        assert "System Prompt" in result.output

    def test_raw_flag_prints_plain_text(self):
        """--raw emits the prompt without any Rich panel decoration."""
        result = runner.invoke(app, ["prompt", "--raw"])
        assert result.exit_code == 0
        assert "You are a helpful assistant" in result.output
        # The Panel subtitle "N chars · M lines" is only present in rich mode
        assert "chars" not in result.output or "\xb7" not in result.output
        # Specifically, the middle-dot separator from the subtitle must be absent
        assert "\xb7" not in result.output

    def test_raw_output_matches_config_system_prompt(self):
        """--raw output equals config.system_prompt exactly (modulo trailing newline)."""
        from agent.transports.cli import _build_config

        config = _build_config()
        result = runner.invoke(app, ["prompt", "--raw"])
        assert result.exit_code == 0
        assert config.system_prompt in result.output

    def test_custom_config_file(self, tmp_path):
        """--config loads a JSON file and its system_prompt is displayed."""
        cfg = AgentConfig(system_prompt="CUSTOM_SYSTEM_PROMPT_VALUE")
        config_path = tmp_path / "cfg.json"
        config_path.write_text(cfg.model_dump_json())

        result = runner.invoke(app, ["prompt", "--config", str(config_path)])
        assert result.exit_code == 0
        assert "CUSTOM_SYSTEM_PROMPT_VALUE" in result.output

    def test_custom_config_file_raw(self, tmp_path):
        """--config combined with --raw prints the custom prompt as plain text."""
        cfg = AgentConfig(system_prompt="RAW_CUSTOM_PROMPT_XYZ")
        config_path = tmp_path / "cfg.json"
        config_path.write_text(cfg.model_dump_json())

        result = runner.invoke(app, ["prompt", "--raw", "--config", str(config_path)])
        assert result.exit_code == 0
        assert "RAW_CUSTOM_PROMPT_XYZ" in result.output
        # The middle-dot subtitle separator is only present in rich panel mode
        assert "\xb7" not in result.output


# ---------------------------------------------------------------------------
# `agent run <task>`
# ---------------------------------------------------------------------------


class TestRunCommand:
    def test_run_simple_task(self, tmp_session_dir):
        mock_p = _make_mock_provider("The task is done.")
        config = _mock_config(tmp_session_dir)

        with (
            patch("agent.core.agent._create_provider", return_value=mock_p),
            patch("agent.transports.cli._build_config", return_value=config),
        ):
            result = runner.invoke(app, ["run", "do something"])

        assert result.exit_code == 0
        assert "The task is done." in result.output

    def test_run_saves_session(self, tmp_session_dir):
        mock_p = _make_mock_provider("Done!")
        config = _mock_config(tmp_session_dir)

        with (
            patch("agent.core.agent._create_provider", return_value=mock_p),
            patch("agent.transports.cli._build_config", return_value=config),
        ):
            result = runner.invoke(app, ["run", "do something"])

        assert result.exit_code == 0
        store = SessionStore(tmp_session_dir)
        assert len(store.list_sessions()) == 1

    def test_run_prints_session_id(self, tmp_session_dir):
        mock_p = _make_mock_provider("All done.")
        config = _mock_config(tmp_session_dir)

        with (
            patch("agent.core.agent._create_provider", return_value=mock_p),
            patch("agent.transports.cli._build_config", return_value=config),
        ):
            result = runner.invoke(app, ["run", "task"])

        assert result.exit_code == 0
        # Session ID should be printed
        store = SessionStore(tmp_session_dir)
        saved_id = store.list_sessions()[0]
        assert saved_id in result.output

    def test_run_accepts_model_and_provider_options(self, tmp_session_dir):
        """Options are accepted without error even when provider is patched."""
        mock_p = _make_mock_provider("Done.")
        config = _mock_config(tmp_session_dir)

        with (
            patch("agent.core.agent._create_provider", return_value=mock_p),
            patch("agent.transports.cli._build_config", return_value=config),
        ):
            result = runner.invoke(
                app, ["run", "task", "--model", "gpt-4o", "--provider", "openai"]
            )
        assert result.exit_code == 0

    def test_run_with_tool_call(self, tmp_session_dir):
        """Provider requests a tool, then gives final answer."""
        mock_p = MockProvider()
        mock_p.enqueue_tool_call("echo", {"message": "hi"}, "tc_1")
        mock_p.enqueue_text("Tool returned: echo: hi")
        config = _mock_config(tmp_session_dir)

        with (
            patch("agent.core.agent._create_provider", return_value=mock_p),
            patch("agent.transports.cli._build_config", return_value=config),
        ):
            result = runner.invoke(app, ["run", "call echo"])

        assert result.exit_code == 0
        # Tool call should be shown
        assert "echo" in result.output


# ---------------------------------------------------------------------------
# `agent chat` (interactive)
# ---------------------------------------------------------------------------


class TestChatCommand:
    def test_chat_single_message_then_quit(self, tmp_session_dir):
        mock_p = _make_mock_provider("Hello back!")
        config = _mock_config(tmp_session_dir)

        with (
            patch("agent.core.agent._create_provider", return_value=mock_p),
            patch("agent.transports.cli._build_config", return_value=config),
        ):
            # Inject: one message, then /quit
            result = runner.invoke(app, ["chat"], input="hello\n/quit\n")

        assert result.exit_code == 0
        assert "Hello back!" in result.output

    def test_chat_eof_exits_gracefully(self, tmp_session_dir):
        mock_p = _make_mock_provider("Hi!")
        config = _mock_config(tmp_session_dir)

        with (
            patch("agent.core.agent._create_provider", return_value=mock_p),
            patch("agent.transports.cli._build_config", return_value=config),
        ):
            # EOF immediately — no messages sent
            result = runner.invoke(app, ["chat"], input="")

        assert result.exit_code == 0

    def test_chat_empty_input_ignored(self, tmp_session_dir):
        mock_p = MockProvider()  # no responses queued — empty input shouldn't call provider
        config = _mock_config(tmp_session_dir)

        with (
            patch("agent.core.agent._create_provider", return_value=mock_p),
            patch("agent.transports.cli._build_config", return_value=config),
        ):
            result = runner.invoke(app, ["chat"], input="   \n/quit\n")

        assert result.exit_code == 0
        assert not mock_p.call_history  # provider never called

    def test_chat_exit_commands(self, tmp_session_dir):
        for cmd in ["/quit", "/exit", "/q"]:
            mock_p = MockProvider()
            config = _mock_config(tmp_session_dir)

            with (
                patch("agent.core.agent._create_provider", return_value=mock_p),
                patch("agent.transports.cli._build_config", return_value=config),
            ):
                result = runner.invoke(app, ["chat"], input=f"{cmd}\n")

            assert result.exit_code == 0

    def test_chat_saves_session_on_exit(self, tmp_session_dir):
        mock_p = _make_mock_provider("Great!")
        config = _mock_config(tmp_session_dir)

        with (
            patch("agent.core.agent._create_provider", return_value=mock_p),
            patch("agent.transports.cli._build_config", return_value=config),
        ):
            runner.invoke(app, ["chat"], input="hi\n/quit\n")

        store = SessionStore(tmp_session_dir)
        assert len(store.list_sessions()) == 1

    def test_chat_multi_turn(self, tmp_session_dir):
        mock_p = MockProvider()
        mock_p.enqueue_text("First response")
        mock_p.enqueue_text("Second response")
        config = _mock_config(tmp_session_dir)

        with (
            patch("agent.core.agent._create_provider", return_value=mock_p),
            patch("agent.transports.cli._build_config", return_value=config),
        ):
            result = runner.invoke(app, ["chat"], input="message one\nmessage two\n/quit\n")

        assert result.exit_code == 0
        assert "First response" in result.output
        assert "Second response" in result.output
        assert len(mock_p.call_history) == 2

    def test_chat_invalid_session_id_exits_with_error(self, tmp_session_dir):
        config = _mock_config(tmp_session_dir)

        with (
            patch("agent.core.agent._create_provider", return_value=MockProvider()),
            patch("agent.transports.cli._build_config", return_value=config),
        ):
            result = runner.invoke(app, ["chat", "--session", "nonexistent_id"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_chat_with_valid_session_id_resumes(self, tmp_session_dir):
        """Passing --session with an existing ID should resume it."""
        store = SessionStore(tmp_session_dir)
        existing = Session()
        existing.add_user_message("original message")
        existing.add_assistant_message("original reply")
        store.save(existing)

        mock_p = _make_mock_provider("Continued!")
        config = _mock_config(tmp_session_dir)

        with (
            patch("agent.core.agent._create_provider", return_value=mock_p),
            patch("agent.transports.cli._build_config", return_value=config),
        ):
            result = runner.invoke(
                app, ["chat", "--session", existing.session_id], input="follow up\n/quit\n"
            )

        assert result.exit_code == 0
        assert "Continued!" in result.output
        # Provider should have received prior history
        assert len(mock_p.call_history) == 1
        prior_msgs = mock_p.call_history[0]["messages"]
        contents = [m.get("content", "") for m in prior_msgs]
        assert any("original message" in str(c) for c in contents)


# ---------------------------------------------------------------------------
# `run --session` (resume into one-shot)
# ---------------------------------------------------------------------------


class TestRunWithSession:
    def test_run_with_valid_session_resumes(self, tmp_session_dir):
        """Passing --session with an existing ID should resume it."""
        store = SessionStore(tmp_session_dir)
        existing = Session()
        existing.add_user_message("original message")
        existing.add_assistant_message("original reply")
        store.save(existing)

        mock_p = _make_mock_provider("Continued from run!")
        config = _mock_config(tmp_session_dir)

        with (
            patch("agent.core.agent._create_provider", return_value=mock_p),
            patch("agent.transports.cli._build_config", return_value=config),
        ):
            result = runner.invoke(app, ["run", "follow up task", "--session", existing.session_id])

        assert result.exit_code == 0
        assert "Continued from run!" in result.output

    def test_run_with_nonexistent_session(self, tmp_session_dir):
        config = _mock_config(tmp_session_dir)

        with (
            patch("agent.core.agent._create_provider", return_value=MockProvider()),
            patch("agent.transports.cli._build_config", return_value=config),
        ):
            result = runner.invoke(app, ["run", "task", "--session", "no_such_session"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_run_with_session_preserves_history(self, tmp_session_dir):
        """History from the original session should be sent to the provider."""
        store = SessionStore(tmp_session_dir)
        s = Session()
        s.add_user_message("what is 2+2")
        s.add_assistant_message("4")
        store.save(s)

        mock_p = _make_mock_provider("And 3+3 is 6.")
        config = _mock_config(tmp_session_dir)

        with (
            patch("agent.core.agent._create_provider", return_value=mock_p),
            patch("agent.transports.cli._build_config", return_value=config),
        ):
            runner.invoke(app, ["run", "and 3+3?", "--session", s.session_id])

        assert len(mock_p.call_history) == 1
        messages = mock_p.call_history[0]["messages"]
        all_content = " ".join(str(m.get("content", "")) for m in messages)
        assert "2+2" in all_content
        assert "4" in all_content

    def test_run_with_session_saves_updated_session(self, tmp_session_dir):
        """After resuming, the session on disk should contain both old and new events."""
        store = SessionStore(tmp_session_dir)
        s = Session()
        s.add_user_message("first turn")
        s.add_assistant_message("first reply")
        store.save(s)

        mock_p = _make_mock_provider("second reply")
        config = _mock_config(tmp_session_dir)

        with (
            patch("agent.core.agent._create_provider", return_value=mock_p),
            patch("agent.transports.cli._build_config", return_value=config),
        ):
            runner.invoke(app, ["run", "second turn", "--session", s.session_id])

        loaded = store.load(s.session_id)
        # Original 2 events + SessionEvent(started) is NOT added on resume
        # + 1 new user message + 1 new assistant message = 4 events
        assert len(loaded.events) >= 4


# ---------------------------------------------------------------------------
# `agent tui`
# ---------------------------------------------------------------------------


class TestTuiCommand:
    def test_tui_launches_and_exits(self, tmp_session_dir):
        """TUI should boot without error when run_tui is patched to exit immediately."""
        config = _mock_config(tmp_session_dir)

        async def noop_tui(cfg, agent=None, verbose=False, session_id=None, theme_name=None):
            pass

        with (
            patch("agent.transports.tui.run_tui", new=noop_tui),
            patch("agent.transports.cli._build_config", return_value=config),
            patch("agent.core.agent._create_provider", return_value=MockProvider()),
        ):
            result = runner.invoke(app, ["tui"])

        assert result.exit_code == 0

    def test_tui_passes_model_option(self, tmp_session_dir):
        received_config = {}

        async def capture_tui(cfg, agent=None, verbose=False, session_id=None, theme_name=None):
            received_config["model"] = cfg.provider.model

        with (
            patch("agent.transports.tui.run_tui", new=capture_tui),
            patch("agent.core.agent._create_provider", return_value=MockProvider()),
        ):
            result = runner.invoke(app, ["tui", "--model", "gpt-4o", "--provider", "openai"])

        assert result.exit_code == 0
        assert received_config.get("model") == "gpt-4o"

    def test_tui_passes_provider_option(self, tmp_session_dir):
        received_config = {}

        async def capture_tui(cfg, agent=None, verbose=False, session_id=None, theme_name=None):
            received_config["provider"] = cfg.provider.name

        with (
            patch("agent.transports.tui.run_tui", new=capture_tui),
            patch("agent.core.agent._create_provider", return_value=MockProvider()),
        ):
            result = runner.invoke(app, ["tui", "--provider", "ollama", "--model", "llama3"])

        assert result.exit_code == 0
        assert received_config.get("provider") == "ollama"

    def test_tui_passes_session_id(self, tmp_session_dir):
        received = {}

        async def capture_tui(cfg, agent=None, verbose=False, session_id=None, theme_name=None):
            received["session_id"] = session_id

        with (
            patch("agent.transports.tui.run_tui", new=capture_tui),
            patch("agent.core.agent._create_provider", return_value=MockProvider()),
        ):
            result = runner.invoke(
                app, ["tui", "--session", "abc123", "--provider", "ollama", "--model", "llama3"]
            )

        assert result.exit_code == 0
        assert received.get("session_id") == "abc123"


# ---------------------------------------------------------------------------
# `agent serve`
# ---------------------------------------------------------------------------


class TestServeCommand:
    def test_serve_exits_when_uvicorn_missing(self, tmp_session_dir):
        config = _mock_config(tmp_session_dir)

        with (
            patch("agent.transports.cli._build_config", return_value=config),
            patch.dict("sys.modules", {"uvicorn": None}),
        ):
            result = runner.invoke(app, ["serve"])

        assert result.exit_code == 1
        assert "uvicorn" in result.output.lower()

    def test_serve_starts_with_default_options(self, tmp_session_dir):
        config = _mock_config(tmp_session_dir)
        captured: dict[str, Any] = {}

        mock_uvicorn = MagicMock()
        mock_uvicorn.run.side_effect = lambda app, **kw: captured.update(kw)

        with (
            patch("agent.transports.cli._build_config", return_value=config),
            patch("agent.core.agent._create_provider", return_value=MockProvider()),
            patch.dict("sys.modules", {"uvicorn": mock_uvicorn}),
        ):
            result = runner.invoke(app, ["serve"])

        assert result.exit_code == 0
        assert captured.get("host") == "127.0.0.1"
        assert captured.get("port") == 8080

    def test_serve_respects_host_and_port_options(self, tmp_session_dir):
        config = _mock_config(tmp_session_dir)
        captured: dict[str, Any] = {}

        mock_uvicorn = MagicMock()
        mock_uvicorn.run.side_effect = lambda app, **kw: captured.update(kw)

        with (
            patch("agent.transports.cli._build_config", return_value=config),
            patch("agent.core.agent._create_provider", return_value=MockProvider()),
            patch.dict("sys.modules", {"uvicorn": mock_uvicorn}),
        ):
            result = runner.invoke(app, ["serve", "--host", "0.0.0.0", "--port", "9090"])

        assert result.exit_code == 0
        assert captured.get("host") == "0.0.0.0"
        assert captured.get("port") == 9090

    def test_serve_prints_startup_message(self, tmp_session_dir):
        config = _mock_config(tmp_session_dir)
        mock_uvicorn = MagicMock()

        with (
            patch("agent.transports.cli._build_config", return_value=config),
            patch("agent.core.agent._create_provider", return_value=MockProvider()),
            patch.dict("sys.modules", {"uvicorn": mock_uvicorn}),
        ):
            result = runner.invoke(app, ["serve", "--port", "8080"])

        assert result.exit_code == 0
        assert "8080" in result.output

    def test_serve_log_level_flag(self, tmp_session_dir):
        """serve --log-level should wire through to uvicorn and aar logging."""
        config = _mock_config(tmp_session_dir)
        captured: dict[str, Any] = {}

        mock_uvicorn = MagicMock()
        mock_uvicorn.run.side_effect = lambda app, **kw: captured.update(kw)

        with (
            patch("agent.transports.cli._build_config", return_value=config) as mock_build,
            patch("agent.core.agent._create_provider", return_value=MockProvider()),
            patch.dict("sys.modules", {"uvicorn": mock_uvicorn}),
        ):
            result = runner.invoke(app, ["serve", "--log-level", "DEBUG"])

        assert result.exit_code == 0
        # _build_config should have received the log_level kwarg
        _, kwargs = mock_build.call_args
        assert kwargs.get("log_level") == "DEBUG"

    def test_serve_log_file_flag(self, tmp_session_dir):
        """serve --log-file should be forwarded to _build_config."""
        config = _mock_config(tmp_session_dir)

        mock_uvicorn = MagicMock()
        mock_uvicorn.run.side_effect = lambda app, **kw: None

        with (
            patch("agent.transports.cli._build_config", return_value=config) as mock_build,
            patch("agent.core.agent._create_provider", return_value=MockProvider()),
            patch.dict("sys.modules", {"uvicorn": mock_uvicorn}),
        ):
            result = runner.invoke(app, ["serve", "--log-file", "/tmp/aar.log"])

        assert result.exit_code == 0
        _, kwargs = mock_build.call_args
        assert kwargs.get("log_file") == "/tmp/aar.log"


# ---------------------------------------------------------------------------
# Live tests — Ollama (skipped unless --live flag is passed)
# ---------------------------------------------------------------------------


@pytest.mark.live
class TestLiveOllama:
    """Integration tests against a local Ollama instance.

    Requires Ollama running with qwen3.5:9b pulled:
        ollama pull qwen3.5:9b

    Run with:
        pytest tests/test_cli.py -m live --live
    """

    PROVIDER = "ollama"
    MODEL = "qwen3.5:9b"

    def test_run_produces_output(self, tmp_session_dir):
        result = runner.invoke(
            app,
            [
                "run",
                "Reply with exactly the word PONG and nothing else.",
                "--provider",
                self.PROVIDER,
                "--model",
                self.MODEL,
            ],
        )
        assert result.exit_code == 0
        # Should have some non-empty text response
        assert len(result.output.strip()) > 0

    def test_run_saves_session(self, tmp_session_dir):
        with patch("agent.transports.cli._build_config") as mock_cfg:
            real_config = AgentConfig(
                provider=ProviderConfig(name=self.PROVIDER, model=self.MODEL),
                session_dir=tmp_session_dir,
                max_steps=3,
            )
            mock_cfg.return_value = real_config
            result = runner.invoke(app, ["run", "Say hello."])

        assert result.exit_code == 0
        store = SessionStore(tmp_session_dir)
        assert len(store.list_sessions()) == 1

    def test_chat_single_turn(self, tmp_session_dir):
        with patch("agent.transports.cli._build_config") as mock_cfg:
            real_config = AgentConfig(
                provider=ProviderConfig(name=self.PROVIDER, model=self.MODEL),
                session_dir=tmp_session_dir,
                max_steps=3,
            )
            mock_cfg.return_value = real_config
            result = runner.invoke(app, ["chat"], input="Reply with the single word YES.\n/quit\n")

        assert result.exit_code == 0
        assert len(result.output.strip()) > 0

    def test_sessions_shows_saved_ids(self, tmp_session_dir):
        """After a live run, sessions command should list the session."""
        with patch("agent.transports.cli._build_config") as mock_cfg:
            real_config = AgentConfig(
                provider=ProviderConfig(name=self.PROVIDER, model=self.MODEL),
                session_dir=tmp_session_dir,
                max_steps=3,
            )
            mock_cfg.return_value = real_config
            runner.invoke(app, ["run", "Say hi."])

            store = SessionStore(tmp_session_dir)
            saved_id = store.list_sessions()[0]

            with patch("agent.transports.cli.SessionStore", return_value=store):
                result = runner.invoke(app, ["sessions"])

        assert saved_id in result.output

    def test_chat_resume_live_session(self, tmp_session_dir):
        """Save a session then resume it with a follow-up."""
        with patch("agent.transports.cli._build_config") as mock_cfg:
            real_config = AgentConfig(
                provider=ProviderConfig(name=self.PROVIDER, model=self.MODEL),
                session_dir=tmp_session_dir,
                max_steps=3,
            )
            mock_cfg.return_value = real_config

            # First turn
            runner.invoke(app, ["run", "Remember the number 42."])
            store = SessionStore(tmp_session_dir)
            saved_id = store.list_sessions()[0]

            # Resume via chat --session
            result = runner.invoke(app, ["chat", "--session", saved_id], input="Say OK.\n/quit\n")

        assert result.exit_code == 0
