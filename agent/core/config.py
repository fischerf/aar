"""Agent configuration."""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from agent.core.guardrails import GuardrailsConfig


def _default_system_prompt(
    sandbox_mode: str = "",
    wsl_distro: str = "",
    system_prompt_hint: str = "",
) -> str:
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
        if sandbox_mode == "wsl":
            sandbox_desc = system_prompt_hint if system_prompt_hint else "Linux sandbox."
            lines += [
                "",
                f"Sandbox: commands run inside a dedicated WSL2 distro ({wsl_distro!r}).",
                sandbox_desc,
                "Shell: sh (POSIX). Standard Unix commands work (ls, cat, grep, find, pwd, …).",
                "File paths: use Windows-style paths for file tools, e.g. .\\file.py or subdirectory\\file.py.",
                f"When creating files, place them inside the working directory ({cwd}) unless told otherwise.",
                "Do NOT use Unix-style absolute paths like /file.py — on Windows they resolve to the drive root, not the project.",
            ]
        else:
            lines += [
                "",
                "Shell: commands run via WSL (bash -c). Standard bash/Unix commands work (ls, cat, grep, find, pwd, …).",
                "File paths: use Windows-style paths for file tools, e.g. .\\file.py or subdirectory\\file.py.",
                f"When creating files, place them inside the working directory ({cwd}) unless told otherwise.",
                "Do NOT use Unix-style absolute paths like /file.py — on Windows they resolve to the drive root, not the project.",
            ]
    else:
        lines.append("Shell: /bin/sh")

    return "\n".join(lines)


class PromptLayer:
    """One contributing layer of the assembled system prompt."""

    __slots__ = ("label", "source", "path", "text", "loaded")

    def __init__(self, label: str, source: str, path: Path | None, text: str, loaded: bool) -> None:
        self.label = label  # short tag, e.g. "aar-system", "global", "project-d"
        self.source = source  # human description, e.g. "~/.aar/rules.md"
        self.path = path  # absolute Path or None for built-in
        self.text = text  # actual content (empty string if not loaded)
        self.loaded = loaded  # False when file was missing / skipped


def _collect_layers(
    project_rules_dir: Path | None = None,
    sandbox_mode: str = "",
    wsl_distro: str = "",
    system_prompt_hint: str = "",
) -> list[PromptLayer]:
    """Return all prompt layers in assembly order, including missing ones."""
    layers: list[PromptLayer] = []

    base_text = _default_system_prompt(
        sandbox_mode=sandbox_mode,
        wsl_distro=wsl_distro,
        system_prompt_hint=system_prompt_hint,
    )
    layers.append(PromptLayer("aar-system", "[built-in]", None, base_text, True))

    global_dir = Path.home() / ".aar"

    global_rules = global_dir / "rules.md"
    layers.append(
        PromptLayer(
            "global",
            str(global_rules),
            global_rules,
            global_rules.read_text(encoding="utf-8").strip() if global_rules.is_file() else "",
            global_rules.is_file(),
        )
    )

    for extra in sorted((global_dir / "rules.d").glob("*.md")):
        layers.append(
            PromptLayer(
                "global-d",
                str(extra),
                extra,
                extra.read_text(encoding="utf-8").strip(),
                True,
            )
        )

    rules_dir = project_rules_dir if project_rules_dir is not None else Path(".agent")
    base = Path.cwd() / rules_dir

    project_rules = base / "rules.md"
    layers.append(
        PromptLayer(
            "project",
            str(project_rules),
            project_rules,
            project_rules.read_text(encoding="utf-8").strip() if project_rules.is_file() else "",
            project_rules.is_file(),
        )
    )

    for extra in sorted((base / "rules.d").glob("*.md")):
        layers.append(
            PromptLayer(
                "project-d",
                str(extra),
                extra,
                extra.read_text(encoding="utf-8").strip(),
                True,
            )
        )

    return layers


def build_system_prompt(
    project_rules_dir: Path | None = None,
    sandbox_mode: str = "",
    wsl_distro: str = "",
    system_prompt_hint: str = "",
) -> str:
    """Assemble the system prompt from base + global rules + project rules.

    Layers (all optional except base):
      1. Base             — runtime facts (OS, cwd, shell)
      2. Global           — ~/.aar/rules.md (user-wide preferences)
      3. Global drop-ins  — ~/.aar/rules.d/*.md (sorted; add files here for env-specific rules)
      4. Project          — <project_rules_dir>/rules.md (project-specific instructions)
      5. Project drop-ins — <project_rules_dir>/rules.d/*.md (sorted)
    """
    layers = _collect_layers(
        project_rules_dir=project_rules_dir,
        sandbox_mode=sandbox_mode,
        wsl_distro=wsl_distro,
        system_prompt_hint=system_prompt_hint,
    )
    return "\n---\n".join(layer.text for layer in layers if layer.loaded)


class ProviderConfig(BaseModel):
    name: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    api_key: str = ""
    base_url: str = ""
    max_tokens: int = 4096
    temperature: float = 0.0
    response_format: str = ""  # "" | "json" | "json_schema"
    json_schema: dict = Field(default_factory=dict)  # schema when response_format="json_schema"
    extra: dict = Field(default_factory=dict)


class ToolConfig(BaseModel):
    enabled_builtins: list[str] = Field(
        default_factory=lambda: ["read_file", "write_file", "edit_file", "list_directory", "bash"]
    )
    # Default timeout (seconds) for bash commands when the model omits the timeout argument.
    # Set higher for long-running tasks (package installs, builds, docker pulls, etc.).
    bash_default_timeout: int = 120
    # Hard outer timeout (seconds) applied by the executor regardless of what the model requests.
    # Should be >= bash_default_timeout; 0 disables the outer guard.
    command_timeout: int = 300
    max_output_chars: int = 50_000


# ---------------------------------------------------------------------------
# Per-mode sandbox configuration models
# Each mode has only the settings that apply to it — no shared flat namespace.
# ---------------------------------------------------------------------------


class LocalSandboxConfig(BaseModel):
    """No isolation — direct subprocess execution (trusted dev environments)."""

    pass  # no configuration options


class LinuxSandboxConfig(BaseModel):
    """Linux Landlock LSM — write-restricted to workspace. Falls back to env restriction + ulimit on older kernels."""

    workspace: str | None = None  # None → cwd at runtime
    max_memory_mb: int = 512


class WindowsSandboxConfig(BaseModel):
    """Windows Job Object (memory/process limits) + Low Integrity Level."""

    workspace: str | None = None  # None → cwd at runtime
    max_memory_mb: int = 512
    max_processes: int = 10
    use_low_integrity: bool = True


class WslSandboxConfig(BaseModel):
    """Dedicated WSL2 distro sandbox. Commands run via wsl -d <distro> -- <shell> -c <cmd>.

    ``profile`` may point to a JSON file (absolute path or ``~``-expanded) that
    supplies default values for all other fields.  Any field set explicitly in
    the config that references this model overrides the profile value.
    """

    profile: str | None = None  # path to a distro-profile JSON (merged as base defaults)

    distro: str = "aar-sandbox"
    shell: str = "sh"  # shell binary inside the distro
    workspace: str | None = None  # Windows path, auto-translated to /mnt/…; None → cwd
    # Provisioning fields (used by aar sandbox setup / reset)
    install_path: str | None = None  # None → %LOCALAPPDATA%\aar\wsl-distros\<distro>
    rootfs_url: str = (
        "https://dl-cdn.alpinelinux.org/alpine/v3.23/releases/x86_64/"
        "alpine-minirootfs-3.23.0-x86_64.tar.gz"
    )
    # Commands run inside the distro before package installation (e.g. enabling extra repos).
    pre_install_commands: list[str] = Field(default_factory=list)
    packages: list[str] = Field(default_factory=lambda: ["python3", "py3-pip"])
    # Template for the package install command; {packages} is replaced with a space-joined list.
    package_install_command: str = "apk add --no-cache {packages}"
    # Overrides auto-detected sandbox description in the system prompt.
    system_prompt_hint: str = ""

    @model_validator(mode="before")
    @classmethod
    def _load_profile(cls, data: Any) -> Any:
        """Merge a distro profile file as base defaults before applying inline values."""
        if not isinstance(data, dict):
            return data
        profile_path = data.get("profile")
        if not profile_path:
            return data
        path = Path(profile_path).expanduser()
        if not path.is_file():
            raise ValueError(f"WslSandboxConfig: profile not found: {path}")
        profile_data: dict = json.loads(path.read_text(encoding="utf-8"))
        profile_data.pop("profile", None)  # don't let the profile reference itself
        merged = {
            **profile_data,
            **{k: v for k, v in data.items() if k != "profile" and v is not None},
        }
        merged["profile"] = str(path)
        return merged


class SandboxConfig(BaseModel):
    """Top-level sandbox configuration.

    Set ``mode`` to choose the sandbox backend, then configure only the
    matching sub-section.  Settings in other sub-sections are ignored.

    Modes:
      local   — no isolation (default, trusted dev)
      linux   — Linux Landlock, write-restricted to workspace (+ ulimit memory cap)
      windows — Windows Job Object + Low Integrity Level
      wsl     — dedicated WSL2 distro
      auto    — picks linux (Linux), windows (Windows), local (other)
    """

    mode: str = "local"
    local: LocalSandboxConfig = Field(default_factory=LocalSandboxConfig)
    linux: LinuxSandboxConfig = Field(default_factory=LinuxSandboxConfig)
    windows: WindowsSandboxConfig = Field(default_factory=WindowsSandboxConfig)
    wsl: WslSandboxConfig = Field(default_factory=WslSandboxConfig)

    @model_validator(mode="before")
    @classmethod
    def _coerce_string(cls, data: Any) -> Any:
        """Allow ``"sandbox": "local"`` as shorthand for ``{"mode": "local"}``."""
        if isinstance(data, str):
            return {"mode": data}
        return data


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
    allowed_paths: list[str] = Field(default_factory=lambda: ["<cwd>/**"])
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    log_all_commands: bool = True
    acp_approval_timeout: float = (
        0.0  # seconds the ACP client has to respond; 0 = wait indefinitely
    )


class TUIConfig(BaseModel):
    theme: str = "default"
    layout: dict = Field(default_factory=dict)


class AgentConfig(BaseModel):
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    tools: ToolConfig = Field(default_factory=ToolConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    tui: TUIConfig = Field(default_factory=TUIConfig)
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    max_steps: int = 50
    timeout: float = 0.0  # wall-clock seconds for the whole run; 0.0 = no limit
    max_retries: int = 3
    streaming: bool = False  # use token-level streaming when the provider supports it
    context_window: int = 0  # model context limit in tokens; 0 = no automatic management
    context_strategy: str = "sliding_window"  # "sliding_window" | "none"
    token_budget: int = 0  # max total tokens across the run; 0 = unlimited
    cost_limit: float = 0.0  # max USD cost across the run; 0.0 = unlimited
    token_warning_threshold: float = 0.8  # fraction of budget to trigger warning style
    cost_warning_threshold: float = 0.8  # fraction of cost_limit to trigger warning style
    session_dir: Path = Field(default_factory=lambda: Path(".agent/sessions"))
    project_rules_dir: Path = Field(default_factory=lambda: Path(".agent"))
    system_prompt: str = ""
    log_level: str = "WARNING"  # DEBUG | INFO | WARNING | ERROR | CRITICAL
    log_file: Path | None = None  # opt-in file logging (append mode)

    def model_post_init(self, __context: Any) -> None:
        """Build the system prompt from config if not explicitly provided."""
        if not self.system_prompt:
            sb = self.safety.sandbox
            self.system_prompt = build_system_prompt(
                project_rules_dir=self.project_rules_dir,
                sandbox_mode=sb.mode,
                wsl_distro=sb.wsl.distro,
                system_prompt_hint=sb.wsl.system_prompt_hint,
            )


def load_config(path: Path) -> AgentConfig:
    """Load and validate an AgentConfig from a JSON file."""
    return AgentConfig.model_validate(json.loads(path.read_text()))
