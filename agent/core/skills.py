"""Skills — lazy-load specialized instructions.

A *skill* is a Markdown file with YAML frontmatter (``name`` and
``description``).  Only the name and a one-line description are injected
into the system prompt so the model knows what's available.  The full
instructions are loaded on demand when the model calls ``read_file``
on the skill's path.

Discovery order (first match for a given name wins):

1. Global:  ``~/.aar/skills/`` — user-wide skills
2. Project: ``<project_rules_dir>/skills/`` — repo-specific skills
3. Extra:   paths listed in ``AgentConfig.skills_dirs``

Within each directory:
- A file named ``SKILL.md`` makes its parent directory a *skill root*
  (no deeper recursion).
- Loose ``.md`` files at the directory root are loaded as standalone skills.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Skill model ──────────────────────────────────────────────────────────


@dataclass
class Skill:
    """A single discovered skill."""

    name: str
    description: str
    file_path: Path
    base_dir: Path


@dataclass
class LoadSkillsResult:
    """Return type of :func:`load_skills`."""

    skills: list[Skill] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ── Frontmatter parsing ─────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


def parse_frontmatter(text: str) -> dict[str, str]:
    """Parse simple YAML-ish frontmatter from a Markdown string.

    Supports only ``key: value`` lines (no nesting, no lists).
    Returns an empty dict if no frontmatter is found.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    result: dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        colon = line.find(":")
        if colon < 1:
            continue
        key = line[:colon].strip()
        value = line[colon + 1 :].strip().strip("\"'")
        result[key] = value
    return result


def strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter, returning just the body."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return text
    return text[m.end() :]


# ── Validation ───────────────────────────────────────────────────────────


def _validate_name(name: str, parent_dir_name: str, file_path: Path) -> list[str]:
    """Validate a skill name against the Agent Skills spec."""
    warnings: list[str] = []
    if not name:
        warnings.append(f"{file_path}: skill has no name")
        return warnings
    if len(name) > 64:
        warnings.append(f"{file_path}: name exceeds 64 chars ({len(name)})")
    if not _NAME_RE.match(name):
        warnings.append(
            f"{file_path}: name '{name}' must be lowercase a-z, 0-9, hyphens; "
            "no leading/trailing/consecutive hyphens"
        )
    if "--" in name:
        warnings.append(f"{file_path}: name '{name}' has consecutive hyphens")
    if name != parent_dir_name and file_path.name == "SKILL.md":
        warnings.append(
            f"{file_path}: name '{name}' doesn't match parent directory '{parent_dir_name}'"
        )
    return warnings


# ── Loading a single skill ───────────────────────────────────────────────


def load_skill_from_file(
    file_path: Path, base_dir: Path | None = None
) -> tuple[Skill | None, list[str]]:
    """Load a skill from a single Markdown file.

    Returns ``(Skill, warnings)`` on success or ``(None, warnings)`` if the
    file is missing a required field.
    """
    warnings: list[str] = []
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [f"{file_path}: cannot read file — {exc}"]

    fm = parse_frontmatter(text)
    name = fm.get("name", "")
    description = fm.get("description", "")

    if not description:
        return None, [f"{file_path}: missing required 'description' — skipped"]

    # Derive name from filename if not in frontmatter
    if not name:
        if file_path.name == "SKILL.md":
            name = file_path.parent.name
        else:
            name = file_path.stem

    parent_dir = file_path.parent.name
    warnings.extend(_validate_name(name, parent_dir, file_path))

    if len(description) > 1024:
        warnings.append(f"{file_path}: description exceeds 1024 chars ({len(description)})")

    if base_dir is None:
        base_dir = file_path.parent

    return Skill(
        name=name, description=description, file_path=file_path, base_dir=base_dir
    ), warnings


# ── Directory scanning ───────────────────────────────────────────────────


def _load_skills_from_dir(
    directory: Path,
    source_label: str,
) -> tuple[list[Skill], list[str]]:
    """Scan a directory for skills.

    - Directories containing ``SKILL.md`` are treated as skill roots (no deeper recursion).
    - Loose ``.md`` files at the directory root are loaded as standalone skills.
    """
    skills: list[Skill] = []
    warnings: list[str] = []

    if not directory.is_dir():
        return skills, warnings

    # First pass: check for SKILL.md in subdirectories
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return skills, warnings

    for entry in entries:
        if entry.is_dir():
            skill_md = entry / "SKILL.md"
            if skill_md.is_file():
                skill, warns = load_skill_from_file(skill_md, base_dir=entry)
                warnings.extend(warns)
                if skill:
                    skills.append(skill)
                    logger.debug(
                        "Loaded skill '%s' from %s (%s)", skill.name, skill_md, source_label
                    )

    # Second pass: loose .md files at the root
    for entry in entries:
        if entry.is_file() and entry.suffix == ".md" and entry.name != "SKILL.md":
            skill, warns = load_skill_from_file(entry)
            warnings.extend(warns)
            if skill:
                skills.append(skill)
                logger.debug("Loaded skill '%s' from %s (%s)", skill.name, entry, source_label)

    return skills, warnings


# ── Main entry point ─────────────────────────────────────────────────────


def load_skills(
    project_rules_dir: Path | None = None,
    extra_dirs: list[str] | None = None,
) -> LoadSkillsResult:
    """Discover and load all skills from standard locations.

    Discovery order (first match for a given name wins):

    1. Global:  ``~/.aar/skills/``
    2. Project: ``<project_rules_dir>/skills/`` (default: ``.agent/skills/``)
    3. Extra:   each path in *extra_dirs* (from ``AgentConfig.skills_dirs``)
    """
    result = LoadSkillsResult()
    seen_names: dict[str, Path] = {}

    def _add(skills: list[Skill], warnings: list[str]) -> None:
        result.warnings.extend(warnings)
        for skill in skills:
            if skill.name in seen_names:
                result.warnings.append(
                    f"Skill name collision: '{skill.name}' from {skill.file_path} "
                    f"ignored — already loaded from {seen_names[skill.name]}"
                )
                continue
            seen_names[skill.name] = skill.file_path
            result.skills.append(skill)

    # 1. Global
    global_dir = Path.home() / ".aar" / "skills"
    _add(*_load_skills_from_dir(global_dir, "global"))

    # 2. Project
    rules_dir = project_rules_dir if project_rules_dir is not None else Path(".agent")
    project_dir = Path.cwd() / rules_dir / "skills"
    _add(*_load_skills_from_dir(project_dir, "project"))

    # 3. Extra dirs
    if extra_dirs:
        for extra_path in extra_dirs:
            p = Path(extra_path).expanduser()
            if p.is_file() and p.suffix == ".md":
                skill, warns = load_skill_from_file(p)
                _add([skill] if skill else [], warns)
            elif p.is_dir():
                _add(*_load_skills_from_dir(p, f"extra:{p}"))
            else:
                result.warnings.append(f"Skills dir not found: {p}")

    if result.skills:
        logger.info("Loaded %d skill(s)", len(result.skills))
    for w in result.warnings:
        logger.warning("Skills: %s", w)

    return result


# ── Policy integration ───────────────────────────────────────────────────


def skill_read_paths(skills: list[Skill]) -> list[str]:
    """Return glob patterns granting read-only access to *skills*.

    Each skill's ``base_dir`` is exposed recursively (``<base_dir>/**``) so the
    model can read the skill file plus any supporting resources bundled
    alongside it. These patterns feed the safety policy's ``read_only_paths``
    allowlist so skills stored outside the workspace (e.g. ``~/.aar/skills``)
    stay readable even when ``allowed_paths`` restricts the agent to the
    project directory. Duplicates are collapsed; order is preserved.
    """
    patterns: list[str] = []
    seen: set[str] = set()
    for skill in skills:
        try:
            base = str(skill.base_dir.resolve()).replace("\\", "/")
        except OSError:
            base = str(skill.base_dir).replace("\\", "/")
        pattern = f"{base}/**"
        if pattern not in seen:
            seen.add(pattern)
            patterns.append(pattern)
    return patterns


# ── Prompt formatting ────────────────────────────────────────────────────


def _escape_xml(s: str) -> str:
    """Escape XML special characters."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def format_skills_for_prompt(skills: list[Skill]) -> str:
    """Render an ``<available_skills>`` XML block for the system prompt.

    Only the name, description, and file path are included — the model
    can ``read_file`` the path to load the full instructions on demand.
    """
    if not skills:
        return ""

    lines = ["<available_skills>"]
    for skill in skills:
        lines.append(
            f'  <skill name="{_escape_xml(skill.name)}" '
            f'file_path="{_escape_xml(str(skill.file_path))}">'
        )
        lines.append(f"    {_escape_xml(skill.description)}")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    lines.append("")
    lines.append("To use a skill, read its file with read_file to get the full instructions.")

    return "\n".join(lines)
