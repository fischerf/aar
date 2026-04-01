"""Agent configuration."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field


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
    require_approval_for_writes: bool = False
    require_approval_for_execute: bool = False
    denied_paths: list[str] = Field(
        default_factory=lambda: [
            # Unix system files
            "/etc/shadow", "/etc/passwd", "/etc/sudoers", "/etc/sudoers.d/**",
            # Generic secret/credential globs
            "**/.env", "**/.env.*",
            "**/credentials", "**/credentials.*",
            "**/secrets", "**/secrets.*",
            # Key material
            "**/*.pem", "**/*.key", "**/*.p12", "**/*.pfx",
            # SSH
            "**/.ssh/**", "**/id_rsa", "**/id_dsa", "**/id_ecdsa", "**/id_ed25519",
            # Cloud provider credential stores
            "**/.aws/**", "**/.azure/**", "**/.config/gcloud/**",
            # Package manager tokens
            "**/.netrc", "**/.npmrc", "**/.pypirc",
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
    system_prompt: str = "You are a helpful assistant with access to tools."


def load_config(path: Path) -> AgentConfig:
    """Load and validate an AgentConfig from a JSON file."""
    return AgentConfig.model_validate(json.loads(path.read_text()))
