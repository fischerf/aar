"""Tests for agent.core.skills — frontmatter parsing, discovery, validation, and prompt formatting."""

from __future__ import annotations

from pathlib import Path

from agent.core.skills import (
    Skill,
    format_skills_for_prompt,
    load_skill_from_file,
    load_skills,
    parse_frontmatter,
    strip_frontmatter,
)

# ── Frontmatter parsing ─────────────────────────────────────────────────


class TestParseFrontmatter:
    def test_simple_frontmatter(self):
        text = "---\nname: my-skill\ndescription: Does something\n---\n# Content"
        fm = parse_frontmatter(text)
        assert fm == {"name": "my-skill", "description": "Does something"}

    def test_no_frontmatter(self):
        text = "# Just a heading\nSome content"
        assert parse_frontmatter(text) == {}

    def test_quoted_values(self):
        text = "---\nname: \"my-skill\"\ndescription: 'Does something'\n---\n"
        fm = parse_frontmatter(text)
        assert fm["name"] == "my-skill"
        assert fm["description"] == "Does something"

    def test_extra_fields_preserved(self):
        text = "---\nname: x\ndescription: y\nlicense: MIT\n---\n"
        fm = parse_frontmatter(text)
        assert fm["license"] == "MIT"

    def test_comments_ignored(self):
        text = "---\n# comment\nname: x\ndescription: y\n---\n"
        fm = parse_frontmatter(text)
        assert "comment" not in fm
        assert fm["name"] == "x"


class TestStripFrontmatter:
    def test_removes_frontmatter(self):
        text = "---\nname: x\n---\n# Body\nContent"
        body = strip_frontmatter(text)
        assert body == "# Body\nContent"

    def test_no_frontmatter_unchanged(self):
        text = "# Body\nContent"
        assert strip_frontmatter(text) == text


# ── Loading a single skill ───────────────────────────────────────────────


class TestLoadSkillFromFile:
    def test_loads_valid_skill(self, tmp_path):
        skill_file = tmp_path / "my-skill.md"
        skill_file.write_text("---\nname: my-skill\ndescription: A test skill\n---\n# Docs\n")

        skill, warnings = load_skill_from_file(skill_file)
        assert skill is not None
        assert skill.name == "my-skill"
        assert skill.description == "A test skill"
        assert skill.file_path == skill_file
        assert len(warnings) == 0

    def test_derives_name_from_filename(self, tmp_path):
        skill_file = tmp_path / "auto-name.md"
        skill_file.write_text("---\ndescription: Auto-named skill\n---\n# Docs\n")

        skill, _ = load_skill_from_file(skill_file)
        assert skill is not None
        assert skill.name == "auto-name"

    def test_derives_name_from_parent_for_skill_md(self, tmp_path):
        skill_dir = tmp_path / "my-tool"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("---\ndescription: A tool skill\n---\n")

        skill, _ = load_skill_from_file(skill_file)
        assert skill is not None
        assert skill.name == "my-tool"

    def test_missing_description_returns_none(self, tmp_path):
        skill_file = tmp_path / "bad.md"
        skill_file.write_text("---\nname: bad\n---\n# No description\n")

        skill, warnings = load_skill_from_file(skill_file)
        assert skill is None
        assert any("missing required" in w for w in warnings)

    def test_nonexistent_file(self, tmp_path):
        skill, warnings = load_skill_from_file(tmp_path / "nope.md")
        assert skill is None
        assert len(warnings) == 1

    def test_name_validation_warning(self, tmp_path):
        skill_file = tmp_path / "BAD_NAME.md"
        skill_file.write_text("---\nname: BAD_NAME\ndescription: Uppercased\n---\n")

        skill, warnings = load_skill_from_file(skill_file)
        assert skill is not None  # loaded despite warning
        assert any("must be lowercase" in w for w in warnings)

    def test_long_description_warning(self, tmp_path):
        skill_file = tmp_path / "verbose.md"
        skill_file.write_text(f"---\nname: verbose\ndescription: {'x' * 1025}\n---\n")

        skill, warnings = load_skill_from_file(skill_file)
        assert skill is not None
        assert any("exceeds 1024" in w for w in warnings)


# ── Directory scanning ───────────────────────────────────────────────────


class TestLoadSkills:
    def test_discovers_loose_md_files(self, tmp_path, monkeypatch):
        skills_dir = tmp_path / ".aar" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "web-search.md").write_text(
            "---\nname: web-search\ndescription: Search the web\n---\n"
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.chdir(tmp_path)

        result = load_skills()
        assert len(result.skills) == 1
        assert result.skills[0].name == "web-search"

    def test_discovers_skill_md_in_subdirs(self, tmp_path, monkeypatch):
        skill_dir = tmp_path / ".aar" / "skills" / "pdf-tools"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: pdf-tools\ndescription: Process PDFs\n---\n"
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.chdir(tmp_path)

        result = load_skills()
        assert len(result.skills) == 1
        assert result.skills[0].name == "pdf-tools"
        assert result.skills[0].base_dir == skill_dir

    def test_discovers_project_skills(self, tmp_path, monkeypatch):
        project_skills = tmp_path / ".agent" / "skills"
        project_skills.mkdir(parents=True)
        (project_skills / "lint.md").write_text("---\nname: lint\ndescription: Run linting\n---\n")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.chdir(tmp_path)

        result = load_skills()
        assert len(result.skills) == 1
        assert result.skills[0].name == "lint"

    def test_name_collision_first_wins(self, tmp_path, monkeypatch):
        # Global has priority over project
        global_dir = tmp_path / ".aar" / "skills"
        global_dir.mkdir(parents=True)
        (global_dir / "dupe.md").write_text("---\nname: dupe\ndescription: Global version\n---\n")

        project_dir = tmp_path / ".agent" / "skills"
        project_dir.mkdir(parents=True)
        (project_dir / "dupe.md").write_text("---\nname: dupe\ndescription: Project version\n---\n")

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.chdir(tmp_path)

        result = load_skills()
        assert len(result.skills) == 1
        assert result.skills[0].description == "Global version"
        assert any("collision" in w.lower() for w in result.warnings)

    def test_extra_dirs(self, tmp_path, monkeypatch):
        extra = tmp_path / "custom-skills"
        extra.mkdir()
        (extra / "custom.md").write_text("---\nname: custom\ndescription: Custom skill\n---\n")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.chdir(tmp_path)

        result = load_skills(extra_dirs=[str(extra)])
        assert len(result.skills) == 1
        assert result.skills[0].name == "custom"

    def test_extra_dirs_single_file(self, tmp_path, monkeypatch):
        skill_file = tmp_path / "single.md"
        skill_file.write_text("---\nname: single\ndescription: One file\n---\n")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.chdir(tmp_path)

        result = load_skills(extra_dirs=[str(skill_file)])
        assert len(result.skills) == 1

    def test_nonexistent_dir_warns(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.chdir(tmp_path)

        result = load_skills(extra_dirs=[str(tmp_path / "no-such-dir")])
        assert len(result.skills) == 0
        assert any("not found" in w for w in result.warnings)

    def test_skills_disabled_in_agent_config(self):
        from agent.core.config import AgentConfig

        config = AgentConfig(skills_enabled=False)
        assert config.skills_enabled is False

    def test_empty_discovery(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.chdir(tmp_path)

        result = load_skills()
        assert len(result.skills) == 0
        assert len(result.warnings) == 0


# ── Prompt formatting ────────────────────────────────────────────────────


class TestFormatSkillsForPrompt:
    def test_empty_skills(self):
        assert format_skills_for_prompt([]) == ""

    def test_single_skill(self):
        skill = Skill(
            name="web-search",
            description="Search the web for information",
            file_path=Path("/home/user/.aar/skills/web-search.md"),
            base_dir=Path("/home/user/.aar/skills"),
        )
        text = format_skills_for_prompt([skill])
        assert "<available_skills>" in text
        assert 'name="web-search"' in text
        assert "Search the web" in text
        assert "</available_skills>" in text
        assert "read_file" in text

    def test_multiple_skills(self):
        skills = [
            Skill(
                name="a",
                description="Skill A",
                file_path=Path("a.md"),
                base_dir=Path("."),
            ),
            Skill(
                name="b",
                description="Skill B",
                file_path=Path("b.md"),
                base_dir=Path("."),
            ),
        ]
        text = format_skills_for_prompt(skills)
        assert text.count("<skill ") == 2
        assert 'name="a"' in text
        assert 'name="b"' in text

    def test_xml_escaping(self):
        skill = Skill(
            name="test",
            description='Uses "special" <chars> & more',
            file_path=Path("test.md"),
            base_dir=Path("."),
        )
        text = format_skills_for_prompt([skill])
        assert "&lt;" in text
        assert "&gt;" in text
        assert "&amp;" in text
        assert "&quot;" in text


# ── Config integration ───────────────────────────────────────────────────


class TestConfigIntegration:
    def test_agent_config_has_skills_fields(self):
        from agent.core.config import AgentConfig

        config = AgentConfig()
        assert config.skills_enabled is True
        assert config.skills_dirs == []

    def test_agent_config_serialization(self):
        from agent.core.config import AgentConfig

        config = AgentConfig(skills_enabled=False, skills_dirs=["/extra/skills"])
        data = config.model_dump()
        assert data["skills_enabled"] is False
        assert data["skills_dirs"] == ["/extra/skills"]

        config2 = AgentConfig.model_validate(data)
        assert config2.skills_enabled is False

    def test_build_system_prompt_with_skills(self, tmp_path, monkeypatch):
        from agent.core.config import build_system_prompt

        monkeypatch.chdir(tmp_path)

        prompt = build_system_prompt(skills_text="<available_skills>test</available_skills>")
        assert "<available_skills>" in prompt

    def test_collect_layers_includes_skills(self, tmp_path, monkeypatch):
        from agent.core.config import _collect_layers

        monkeypatch.chdir(tmp_path)

        layer_list = _collect_layers(skills_text="<available_skills>test</available_skills>")
        labels = [layer.label for layer in layer_list]
        assert "skills" in labels

    def test_collect_layers_no_skills_when_none(self, tmp_path, monkeypatch):
        from agent.core.config import _collect_layers

        monkeypatch.chdir(tmp_path)

        layer_list = _collect_layers()
        labels = [layer.label for layer in layer_list]
        assert "skills" not in labels
