"""Agent configuration."""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path

from pydantic import BaseModel, Field


def _default_system_prompt() -> str:
    """Generate a base system prompt with OS, cwd, and shell context."""
    os_name = platform.system()  # "Windows", "Linux", "Darwin"
    cwd = str(Path.cwd())

    lines = [
        "You are a helpful assistant with access to tools.",
        "",
        f"Operating system: {os_name}",
        f"Working directory: {cwd}",
    ]

    if os.name == "nt":
        lines += [
            "",
            "Shell: commands run via Git Bash (bash -c). Standard bash/Unix commands work (ls, cat, grep, find, pwd, …).",
            "File paths: use Windows-style paths for file tools, e.g. .\\file.py or subdirectory\\file.py.",
            f"When creating files, place them inside the working directory ({cwd}) unless told otherwise.",
            "Do NOT use Unix-style absolute paths like /file.py — on Windows they resolve to the drive root, not the project.",
        ]
    else:
        lines.append("Shell: /bin/sh")

    return "\n".join(lines)


def build_system_prompt() -> str:
    """Assemble the system prompt from base + global rules + project rules.

    Layers (all optional except base):
      1. Base     — runtime facts (OS, cwd, shell)
      2. Global   — ~/.aar/rules.md (user-wide preferences)
      3. Project  — .agent/rules.md (project-specific instructions)
    """
    sections = [_default_system_prompt()]

    global_rules = Path.home() / ".aar" / "rules.md"
    if global_rules.is_file():
        sections.append(global_rules.read_text(encoding="utf-8").strip())

    project_rules = Path.cwd() / ".agent" / "rules.md"
    if project_rules.is_file():
        sections.append(project_rules.read_text(encoding="utf-8").strip())

    return "\n---\n".join(sections)


class ProviderConfig(BaseModel):
    name: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    api_key: str = ""
    base_url: str = ""
    max_tokens: int = 4096
    temperature: float = 0.0
    extra: dict = Field(default_factory=dict)


class ToolConfig(BaseModel):
    enabled_builtins: list[str] = Field(
        default_factory=lambda: ["read_file", "write_file", "edit_file", "list_directory", "bash"]
    )
    allowed_paths: list[str] = Field(default_factory=list)
    command_timeout: int = 30
    max_output_chars: int = 50_000


class SafetyConfig(BaseModel):
    read_only: bool = False
    require_approval_for_writes: bool = True
    require_approval_for_execute: bool = True
    denied_paths: list[str] = Field(
        default_factory=lambda: [
            # Unix system files
            "/etc/shadow",
            "/etc/passwd",
            "/etc/sudoers",
            "/etc/sudoers.d/**",
            # Generic secret/credential globs
            "**/.env",
            "**/.env.*",
            "**/credentials",
            "**/credentials.*",
            "**/secrets",
            "**/secrets.*",
            # Key material
            "**/*.pem",
            "**/*.key",
            "**/*.p12",
            "**/*.pfx",
            # SSH
            "**/.ssh/**",
            "**/id_rsa",
            "**/id_dsa",
            "**/id_ecdsa",
            "**/id_ed25519",
            # Cloud provider credential stores
            "**/.aws/**",
            "**/.azure/**",
            "**/.config/gcloud/**",
            # Package manager tokens
            "**/.netrc",
            "**/.npmrc",
            "**/.pypirc",
        ]
    )
    allowed_paths: list[str] = Field(default_factory=list)
    sandbox: str = "local"  # "local", "subprocess", or "container"
    sandbox_max_memory_mb: int = 512
    log_all_commands: bool = True


class AgentConfig(BaseModel):
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    tools: ToolConfig = Field(default_factory=ToolConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    max_steps: int = 50
    max_tokens_per_turn: int = 4096
    timeout: float = 300.0
    session_dir: Path = Field(default_factory=lambda: Path(".agent/sessions"))
    system_prompt: str = Field(default_factory=build_system_prompt)


def load_config(path: Path) -> AgentConfig:
    """Load and validate an AgentConfig from a JSON file."""
    return AgentConfig.model_validate(json.loads(path.read_text()))
