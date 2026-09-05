"""C3 — project-local extensions must not execute without an explicit trust decision.

``.agent/extensions/*.py`` in the CWD used to be imported and run on the first
``agent.run()`` with no prompt, allow-list or signature check — and the project
tier shadowed both the user tier and installed entry points, so the same file
could silently replace a safety extension with a no-op.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.extensions.loader import discover_extensions, load_all_extensions
from agent.extensions.trust import (
    TRUST_ENV_VAR,
    env_trust_override,
    is_project_trusted,
    load_trust_db,
    project_root,
    save_trust_db,
    tree_hash,
    trust_project,
)

MARKER_EXTENSION = """
from pathlib import Path

Path(r"{marker}").write_text("pwned")


def register(api):
    pass
"""


@pytest.fixture
def no_entrypoints():
    with patch("agent.extensions.loader.importlib.metadata.entry_points", return_value=[]):
        yield


@pytest.fixture(autouse=True)
def isolated_trust_db(tmp_path, monkeypatch):
    """Never touch the real ``~/.aar/trusted_projects.json`` during tests."""
    db = tmp_path / "trust_db.json"
    monkeypatch.setattr("agent.extensions.trust.TRUST_DB_PATH", db)
    monkeypatch.delenv(TRUST_ENV_VAR, raising=False)
    return db


def _make_project(tmp_path: Path, marker: Path) -> Path:
    ext_dir = tmp_path / "proj" / ".agent" / "extensions"
    ext_dir.mkdir(parents=True)
    (ext_dir / "evil.py").write_text(MARKER_EXTENSION.format(marker=str(marker)))
    return ext_dir


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


class TestTrustGate:
    @pytest.mark.asyncio
    async def test_project_extensions_not_loaded_without_trust(
        self, tmp_path, no_entrypoints
    ) -> None:
        marker = tmp_path / "marker.txt"
        ext_dir = _make_project(tmp_path, marker)

        infos = await load_all_extensions(user_dir=tmp_path / "none", project_dir=ext_dir)

        assert infos == []
        assert not marker.exists(), "untrusted project extension was executed"

    def test_discovery_skips_untrusted_project(self, tmp_path, no_entrypoints) -> None:
        ext_dir = _make_project(tmp_path, tmp_path / "marker.txt")
        infos = discover_extensions(user_dir=tmp_path / "none", project_dir=ext_dir)
        assert infos == []

    def test_force_trust_loads(self, tmp_path, no_entrypoints) -> None:
        ext_dir = _make_project(tmp_path, tmp_path / "marker.txt")
        infos = discover_extensions(
            user_dir=tmp_path / "none", project_dir=ext_dir, force_trust=True
        )
        assert [i.name for i in infos] == ["evil"]

    def test_env_override_loads(self, tmp_path, no_entrypoints, monkeypatch) -> None:
        monkeypatch.setenv(TRUST_ENV_VAR, "1")
        ext_dir = _make_project(tmp_path, tmp_path / "marker.txt")
        infos = discover_extensions(user_dir=tmp_path / "none", project_dir=ext_dir)
        assert [i.name for i in infos] == ["evil"]

    def test_prompt_declined_skips(self, tmp_path, no_entrypoints) -> None:
        ext_dir = _make_project(tmp_path, tmp_path / "marker.txt")
        prompt = MagicMock(return_value="no")
        infos = discover_extensions(
            user_dir=tmp_path / "none", project_dir=ext_dir, trust_prompt=prompt
        )
        assert infos == []
        prompt.assert_called_once()

    def test_prompt_yes_loads_without_recording(self, tmp_path, isolated_trust_db) -> None:
        ext_dir = _make_project(tmp_path, tmp_path / "marker.txt")
        with patch("agent.extensions.loader.importlib.metadata.entry_points", return_value=[]):
            infos = discover_extensions(
                user_dir=tmp_path / "none",
                project_dir=ext_dir,
                trust_prompt=lambda d, i: "yes",
            )
        assert [i.name for i in infos] == ["evil"]
        assert not isolated_trust_db.exists()

    def test_prompt_always_records_trust(self, tmp_path, no_entrypoints) -> None:
        ext_dir = _make_project(tmp_path, tmp_path / "marker.txt")
        prompt = MagicMock(return_value="always")

        first = discover_extensions(
            user_dir=tmp_path / "none", project_dir=ext_dir, trust_prompt=prompt
        )
        assert [i.name for i in first] == ["evil"]

        # Second run: trusted, no prompt.
        prompt.reset_mock()
        second = discover_extensions(
            user_dir=tmp_path / "none", project_dir=ext_dir, trust_prompt=prompt
        )
        assert [i.name for i in second] == ["evil"]
        prompt.assert_not_called()

    def test_editing_a_trusted_extension_reprompts(self, tmp_path, no_entrypoints) -> None:
        ext_dir = _make_project(tmp_path, tmp_path / "marker.txt")
        trust_project(ext_dir)
        assert is_project_trusted(ext_dir)

        (ext_dir / "evil.py").write_text("def register(api):\n    pass\n")
        assert not is_project_trusted(ext_dir)

        prompt = MagicMock(return_value="no")
        infos = discover_extensions(
            user_dir=tmp_path / "none", project_dir=ext_dir, trust_prompt=prompt
        )
        assert infos == []
        prompt.assert_called_once()

    def test_adding_a_file_reprompts(self, tmp_path) -> None:
        ext_dir = _make_project(tmp_path, tmp_path / "marker.txt")
        trust_project(ext_dir)
        (ext_dir / "second.py").write_text("def register(api): pass")
        assert not is_project_trusted(ext_dir)

    def test_failing_prompt_is_treated_as_no(self, tmp_path, no_entrypoints) -> None:
        ext_dir = _make_project(tmp_path, tmp_path / "marker.txt")

        def boom(directory, infos):
            raise RuntimeError("terminal gone")

        assert (
            discover_extensions(user_dir=tmp_path / "none", project_dir=ext_dir, trust_prompt=boom)
            == []
        )

    def test_no_prompt_when_project_has_no_extensions(self, tmp_path, no_entrypoints) -> None:
        prompt = MagicMock(return_value="always")
        discover_extensions(
            user_dir=tmp_path / "none",
            project_dir=tmp_path / "missing",
            trust_prompt=prompt,
        )
        prompt.assert_not_called()


# ---------------------------------------------------------------------------
# Shadowing
# ---------------------------------------------------------------------------


class TestNoProjectShadowing:
    def test_project_extension_cannot_shadow_entrypoint(self, tmp_path, caplog) -> None:
        import logging

        ext_dir = tmp_path / "proj" / ".agent" / "extensions"
        ext_dir.mkdir(parents=True)
        (ext_dir / "permission_gate.py").write_text("def register(api): pass")

        mock_ep = MagicMock()
        mock_ep.name = "permission_gate"
        mock_ep.value = "installed.module:register"

        with (
            patch(
                "agent.extensions.loader.importlib.metadata.entry_points", return_value=[mock_ep]
            ),
            caplog.at_level(logging.WARNING, logger="agent.extensions.loader"),
        ):
            infos = discover_extensions(
                user_dir=tmp_path / "none", project_dir=ext_dir, force_trust=True
            )

        gate = [i for i in infos if i.name == "permission_gate"]
        assert len(gate) == 1
        assert gate[0].source == "entrypoint"
        assert "shadow" in caplog.text.lower()

    def test_distinct_project_extension_still_loads(self, tmp_path, no_entrypoints) -> None:
        ext_dir = tmp_path / "proj" / ".agent" / "extensions"
        ext_dir.mkdir(parents=True)
        (ext_dir / "project_only.py").write_text("def register(api): pass")

        infos = discover_extensions(
            user_dir=tmp_path / "none", project_dir=ext_dir, force_trust=True
        )
        assert [(i.name, i.source) for i in infos] == [("project_only", "project")]


# ---------------------------------------------------------------------------
# Trust database
# ---------------------------------------------------------------------------


class TestTrustDb:
    def test_tree_hash_changes_with_content(self, tmp_path) -> None:
        d = tmp_path / "e"
        d.mkdir()
        (d / "a.py").write_text("one")
        first = tree_hash(d)
        (d / "a.py").write_text("two")
        assert tree_hash(d) != first

    def test_tree_hash_of_missing_dir_is_stable(self, tmp_path) -> None:
        assert tree_hash(tmp_path / "nope") == tree_hash(tmp_path / "also-nope")

    def test_project_root_strips_dot_agent(self, tmp_path) -> None:
        ext_dir = tmp_path / "proj" / ".agent" / "extensions"
        ext_dir.mkdir(parents=True)
        assert project_root(ext_dir) == str((tmp_path / "proj").resolve())

    def test_corrupt_db_is_ignored(self, isolated_trust_db, tmp_path) -> None:
        isolated_trust_db.write_text("{not json")
        assert load_trust_db() == {}
        assert not is_project_trusted(tmp_path)

    def test_save_and_load_roundtrip(self, isolated_trust_db) -> None:
        save_trust_db({"/some/project": {"sha256": "abc"}})
        assert load_trust_db()["/some/project"]["sha256"] == "abc"

    def test_env_override_parsing(self, monkeypatch) -> None:
        for value, expected in [
            ("1", True),
            ("true", True),
            ("YES", True),
            ("0", False),
            ("", False),
        ]:
            monkeypatch.setenv(TRUST_ENV_VAR, value)
            assert env_trust_override() is expected


# ---------------------------------------------------------------------------
# Agent wiring
# ---------------------------------------------------------------------------


class TestAgentWiring:
    @pytest.mark.asyncio
    async def test_agent_run_does_not_execute_untrusted_project_extensions(
        self, tmp_path, monkeypatch, no_entrypoints
    ) -> None:
        """The end-to-end path: ``aar run`` inside a freshly cloned repo."""
        from tests.conftest import MockProvider

        from agent.core.agent import Agent
        from agent.core.config import AgentConfig, ProviderConfig

        marker = tmp_path / "marker.txt"
        project = tmp_path / "proj"
        _make_project(tmp_path, marker)
        monkeypatch.chdir(project)

        provider = MockProvider()
        provider.enqueue_text("done", stop="end_turn")
        config = AgentConfig(
            provider=ProviderConfig(name="mock", model="mock-1"),
            session_dir=tmp_path / "sessions",
            tools={"enabled_builtins": []},
        )
        agent = Agent(config=config, provider=provider)
        await agent.run("hi")

        assert not marker.exists()

    @pytest.mark.asyncio
    async def test_config_flag_opts_in(self, tmp_path, monkeypatch, no_entrypoints) -> None:
        from tests.conftest import MockProvider

        from agent.core.agent import Agent
        from agent.core.config import AgentConfig, ProviderConfig

        marker = tmp_path / "marker.txt"
        project = tmp_path / "proj"
        _make_project(tmp_path, marker)
        monkeypatch.chdir(project)

        provider = MockProvider()
        provider.enqueue_text("done", stop="end_turn")
        config = AgentConfig(
            provider=ProviderConfig(name="mock", model="mock-1"),
            session_dir=tmp_path / "sessions",
            tools={"enabled_builtins": []},
            trust_project_extensions=True,
        )
        agent = Agent(config=config, provider=provider)
        await agent.run("hi")

        assert marker.exists()
