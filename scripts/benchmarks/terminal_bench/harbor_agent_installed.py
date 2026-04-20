"""Aar — Harbor Framework installed agent for Terminal-Bench 2.0.

The agent is deployed *inside* Harbor's container, so all file I/O and shell
commands run on the task filesystem without any proxying.

Usage:
    harbor run -d "terminal-bench@2.0" \
        --agent-import-path scripts.benchmarks.terminal_bench.harbor_agent_installed:AarInstalledAgent \
        --model anthropic/claude-sonnet-4-6
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

# Repo root — three levels up from scripts/benchmarks/terminal_bench/
_REPO_ROOT = Path(__file__).resolve().parents[3]

# API key env vars forwarded into the container (add others as needed)
_API_KEY_VARS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OLLAMA_BASE_URL",
)


class AarInstalledAgent(BaseInstalledAgent):
    """Aar agent running inside Harbor's container environment."""

    @staticmethod
    def name() -> str:
        return "aar"

    def version(self) -> str | None:
        return "0.1.0"

    # ------------------------------------------------------------------
    # install — called once per environment before the first run
    # ------------------------------------------------------------------

    async def install(self, environment: BaseEnvironment) -> None:
        # Ensure pip is available (most Harbor base images have Python 3)
        await self.exec_as_root(
            environment,
            command=(
                "if command -v apt-get &>/dev/null; then"
                "  apt-get update -q && apt-get install -y --no-install-recommends python3-pip;"
                " elif command -v apk &>/dev/null; then"
                "  apk add --no-cache python3 py3-pip;"
                " fi"
            ),
            env={"DEBIAN_FRONTEND": "noninteractive"},
        )

        # Build a wheel from the local source tree and upload it to the container.
        # Installing 'aar' from PyPI would pull a completely different unrelated
        # package that happens to share the name.
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(
                [
                    "pip", "wheel", "--no-deps", "--quiet",
                    "-w", tmp,
                    str(_REPO_ROOT),
                ],
                check=True,
            )
            wheel = next(Path(tmp).glob("aar*.whl"))
            await environment.upload_file(wheel, f"/tmp/{wheel.name}")
            remote_wheel = f"/tmp/{wheel.name}"

        # Install the wheel globally (as root → /usr/local/bin/aar always on PATH)
        # then add the provider extras as separate packages.
        await self.exec_as_root(
            environment,
            command=(
                f"pip install --quiet --break-system-packages {remote_wheel} && "
                "pip install --quiet --break-system-packages anthropic openai ollama"
            ),
        )

    # ------------------------------------------------------------------
    # run — called for each task trial
    # ------------------------------------------------------------------

    def populate_context_post_run(self, context: AgentContext) -> None:
        # Token counts aren't available from the installed agent's stdout.
        # Harbor calls this after run() completes; a no-op is valid.
        pass

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        # Parse Harbor's "provider/model" string into aar's config format
        if self.model_name and "/" in self.model_name:
            provider_name, model_id = self.model_name.split("/", 1)
        else:
            provider_name = "anthropic"
            model_id = self.model_name or "claude-sonnet-4-6"

        provider_cfg: dict = {"name": provider_name, "model": model_id}

        # For Ollama the default base_url (localhost:11434) points at the container
        # itself, not the Windows host.  Read OLLAMA_BASE_URL from the host env and
        # embed it in the config so aar uses the correct address inside the container.
        if provider_name == "ollama":
            provider_cfg["base_url"] = os.environ.get(
                "OLLAMA_BASE_URL", "http://localhost:11434"
            )

        config = {
            "provider": provider_cfg,
            "max_steps": 50,
            "tools": {
                "enabled_builtins": [
                    "bash",
                    "read_file",
                    "write_file",
                    "edit_file",
                    "list_directory",
                ]
            },
            # Disable interactive approval gates — aar defaults to True for both,
            # which causes console.input() to hang in a headless container.
            "safety": {
                "require_approval_for_writes": False,
                "require_approval_for_execute": False,
            },
        }

        # Write config into the container before invoking aar
        escaped_config = shlex.quote(json.dumps(config))
        await environment.exec(
            f"mkdir -p ~/.aar && printf %s {escaped_config} > ~/.aar/config.json"
        )

        # Forward API keys from the host into the container
        env = {k: v for k in _API_KEY_VARS if (v := os.environ.get(k))}

        # --no-require-approval as a belt-and-suspenders CLI override on top of
        # the config setting, in case the config file is not picked up for any reason.
        result = await environment.exec(
            f"aar run --no-require-approval {shlex.quote(instruction)}",
            env=env,
            timeout_sec=600,
        )

        context.metadata = {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "return_code": result.return_code,
        }
