"""Live integration test — verify the agent can complete a full coding plan.

This test runs the agent against a real LLM provider (Ollama by default)
and checks that it produces correct, working code from a task plan.

Run with:
    pytest tests/live/test_plan_execution.py -m live --live -v -s

Requires a running Ollama instance with a model pulled, e.g.:
    ollama pull gemma3:12b
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from agent.core.agent import Agent
from agent.core.config import AgentConfig, ProviderConfig, SafetyConfig, ToolConfig
from agent.core.events import AssistantMessage, ErrorEvent
from agent.core.state import AgentState

# ---------------------------------------------------------------------------
# Plan prompt (the task the agent must complete)
# ---------------------------------------------------------------------------

# ... this could be added to break local models!
#
# 7. Introduce a deliberate bug:
#   - Change one test to expect the wrong value.
#   - Detect the inconsistency and fix the test.

PLAN_PROMPT = textwrap.dedent("""\
    You are given a small project. Your goal is to FULLY complete the task.

    IMPORTANT:
    - Do not stop early.
    - Do not explain what you would do — DO it.
    - Only finish when everything works and all requirements are satisfied.

    ---

    PROJECT SETUP:

    There is a folder with these files:

    1. main.py
    2. utils.py

    Contents:

    --- main.py ---
    from utils import process_numbers

    def main():
        nums = [1, 2, 3, 4]
        result = process_numbers(nums)
        print(result)

    if __name__ == "__main__":
        main()

    --- utils.py ---
    def process_numbers(nums):
        # TODO: implement
        pass

    ---

    TASK:

    1. Implement `process_numbers(nums)` so that:
       - It returns a list where:
         - even numbers are doubled
         - odd numbers are squared

       Example:
       Input: [1,2,3]
       Output: [1,4,9]

    2. Create a new file `test_utils.py` that:
       - tests at least 3 cases
       - exits with a non-zero code if a test fails

    3. Modify `main.py` so that:
       - it prints "DONE" after printing the result

    4. Run the program and tests using bash to verify everything works.

    5. If anything fails:
       - fix it
       - re-run until it passes

    6. When everything works:
       - print EXACTLY: ALL_TASKS_COMPLETED

    ---

    CONSTRAINTS:

    - You MUST use the available tools (read_file, write_file, edit_file, bash).
    - Do NOT assume correctness — VERIFY by running code.
    - Do NOT stop after writing code — you must run and confirm.
    - Do NOT stop if something fails — fix it.

    ---

    SUCCESS CONDITION:

    The task is only complete if:
    - implementation is correct
    - tests pass
    - program runs
    - "DONE" is printed
    - AND you output: ALL_TASKS_COMPLETED
""")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_project(work_dir: Path) -> None:
    """Write the initial project files into the work directory."""
    (work_dir / "main.py").write_text(
        textwrap.dedent("""\
            from utils import process_numbers

            def main():
                nums = [1, 2, 3, 4]
                result = process_numbers(nums)
                print(result)

            if __name__ == "__main__":
                main()
        """),
        encoding="utf-8",
    )
    (work_dir / "utils.py").write_text(
        textwrap.dedent("""\
            def process_numbers(nums):
                # TODO: implement
                pass
        """),
        encoding="utf-8",
    )


def _run_python(work_dir: Path, script: str, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    """Run a Python script in the work directory and return the result."""
    return subprocess.run(
        ["python", script],
        cwd=work_dir,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.live
class TestPlanExecution:
    """End-to-end test: agent completes a coding plan from scratch."""

    @pytest.fixture()
    def work_dir(self, tmp_path: Path) -> Path:
        """Create an isolated working directory with the starter project."""
        project = tmp_path / "plan_project"
        project.mkdir()
        _setup_project(project)
        return project

    @pytest.fixture()
    def agent_config(self, work_dir: Path) -> AgentConfig:
        """Agent config pointed at Ollama with full tool access, no approval."""
        model = os.environ.get("AAR_LIVE_MODEL", "gemma4:e4b")
        provider = os.environ.get("AAR_LIVE_PROVIDER", "ollama")
        return AgentConfig(
            provider=ProviderConfig(
                name=provider,
                model=model,
                max_tokens=8192,
            ),
            tools=ToolConfig(
                enabled_builtins=[
                    "read_file",
                    "write_file",
                    "edit_file",
                    "list_directory",
                    "bash",
                ],
                command_timeout=30,
                allowed_paths=[str(work_dir)],
            ),
            safety=SafetyConfig(
                read_only=False,
                require_approval_for_writes=False,
                require_approval_for_execute=False,
                allowed_paths=[str(work_dir)],
            ),
            max_steps=30,
            timeout=300.0,
            system_prompt=(
                f"You are a helpful coding assistant. "
                f"Working directory: {work_dir}\n"
                f"All file paths must be relative to or inside {work_dir}.\n"
                f"Use bash to run Python scripts."
            ),
        )

    @pytest.mark.asyncio
    async def test_agent_completes_coding_plan(
        self,
        work_dir: Path,
        agent_config: AgentConfig,
    ) -> None:
        """Run the agent on the plan and verify all deliverables."""
        agent = Agent(config=agent_config)
        session = await agent.run(PLAN_PROMPT)

        # ── Basic session checks ──────────────────────────────────────
        assert session.state in {
            AgentState.COMPLETED,
            AgentState.MAX_STEPS,
        }, f"Agent ended in unexpected state: {session.state}"

        # ── Collect all assistant text ────────────────────────────────
        assistant_text = "\n".join(
            e.content for e in session.events if isinstance(e, AssistantMessage) and e.content
        )

        # ── Check the agent declared completion ───────────────────────
        assert "ALL_TASKS_COMPLETED" in assistant_text, "Agent did not output ALL_TASKS_COMPLETED"

        # ── Verify utils.py was implemented ───────────────────────────
        utils_path = work_dir / "utils.py"
        assert utils_path.exists(), "utils.py missing"
        utils_src = utils_path.read_text(encoding="utf-8")
        assert "def process_numbers" in utils_src, "process_numbers not defined"

        # Make sure the stub `pass` was replaced with a real body
        func_body = utils_src.split("def process_numbers")[1]
        code_lines = [
            ln.split("#")[0].strip()
            for ln in func_body.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        assert not all(ln == "pass" for ln in code_lines if ln), (
            "process_numbers still has only a bare pass"
        )

        # ── Verify the implementation is correct ──────────────────────
        spec = importlib.util.spec_from_file_location("utils", utils_path)
        assert spec is not None and spec.loader is not None
        utils_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(utils_mod)

        assert utils_mod.process_numbers([1, 2, 3, 4]) == [1, 4, 9, 8], (
            f"process_numbers([1,2,3,4]) returned {utils_mod.process_numbers([1, 2, 3, 4])}"
        )
        assert utils_mod.process_numbers([]) == [], "Empty list case failed"
        assert utils_mod.process_numbers([2]) == [4], "Single even number case failed"
        assert utils_mod.process_numbers([3]) == [9], "Single odd number case failed"

        # ── Verify test_utils.py exists ───────────────────────────────
        test_path = work_dir / "test_utils.py"
        assert test_path.exists(), "test_utils.py was not created"

        # ── Verify tests pass when run ────────────────────────────────
        result = _run_python(work_dir, "test_utils.py")
        assert result.returncode == 0, (
            f"test_utils.py failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

        # ── Verify main.py prints DONE ────────────────────────────────
        main_path = work_dir / "main.py"
        assert main_path.exists(), "main.py missing"
        result = _run_python(work_dir, "main.py")
        assert result.returncode == 0, (
            f"main.py failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "DONE" in result.stdout, f"main.py output does not contain DONE:\n{result.stdout}"

        # ── Check no unrecoverable errors in the session ──────────────
        errors = [e for e in session.events if isinstance(e, ErrorEvent) and not e.recoverable]
        assert len(errors) == 0, f"Agent had unrecoverable errors: {[e.message for e in errors]}"

        # ── Summary ───────────────────────────────────────────────────
        print("\n✅ Plan execution test passed!")
        print(f"   Steps: {session.step_count}")
        print(f"   Total tokens: {session.total_tokens}")
        print(f"   Total cost: ${session.total_cost:.4f}")
