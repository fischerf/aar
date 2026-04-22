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

        failures: list[str] = []

        def ok(label: str) -> None:
            print(f"  \u2705 {label}")

        def fail(label: str, detail: str = "") -> None:
            msg = f"{label}: {detail}" if detail else label
            failures.append(msg)
            print(f"  \u274c {msg}")

        print(
            f"\n\u2500\u2500 Deliverable checks (state={session.state}, steps={session.step_count}) \u2500\u2500"
        )

        # \u2500\u2500 Basic session checks
        if session.state not in {AgentState.COMPLETED, AgentState.MAX_STEPS}:
            fail("session state", f"unexpected state {session.state!r}")
        else:
            ok(f"session state ({session.state})")

        # \u2500\u2500 Collect all assistant text
        assistant_text = "\n".join(
            e.content for e in session.events if isinstance(e, AssistantMessage) and e.content
        )

        # \u2500\u2500 Verify utils.py was implemented
        utils_path = work_dir / "utils.py"
        utils_mod = None
        if not utils_path.exists():
            fail("utils.py exists")
        else:
            utils_src = utils_path.read_text(encoding="utf-8")
            if "def process_numbers" not in utils_src:
                fail("process_numbers defined", "function not found in utils.py")
            else:
                func_body = utils_src.split("def process_numbers")[1]
                code_lines = [
                    ln.split("#")[0].strip()
                    for ln in func_body.splitlines()
                    if ln.strip() and not ln.strip().startswith("#")
                ]
                if all(ln == "pass" for ln in code_lines if ln):
                    fail("process_numbers implemented", "still only contains bare pass")
                else:
                    ok("process_numbers implemented")

                # Try to import and run the function
                try:
                    spec = importlib.util.spec_from_file_location("utils_live", utils_path)
                    assert spec is not None and spec.loader is not None
                    utils_mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(utils_mod)  # type: ignore[arg-type]

                    cases = [
                        ([1, 2, 3, 4], [1, 4, 9, 8]),
                        ([], []),
                        ([2], [4]),
                        ([3], [9]),
                    ]
                    for inp, expected in cases:
                        got = utils_mod.process_numbers(inp)
                        if got != expected:
                            fail(
                                f"process_numbers({inp!r})",
                                f"expected {expected!r}, got {got!r}",
                            )
                        else:
                            ok(f"process_numbers({inp!r}) == {expected!r}")
                except Exception as exc:
                    fail("process_numbers import/run", str(exc))

        # \u2500\u2500 Verify test_utils.py exists and passes
        test_path = work_dir / "test_utils.py"
        if not test_path.exists():
            fail("test_utils.py created")
        else:
            ok("test_utils.py created")
            result = _run_python(work_dir, "test_utils.py")
            if result.returncode != 0:
                fail(
                    "test_utils.py passes",
                    f"stdout: {result.stdout!r}  stderr: {result.stderr!r}",
                )
            else:
                ok("test_utils.py passes")

        # \u2500\u2500 Verify main.py prints DONE
        main_path = work_dir / "main.py"
        if not main_path.exists():
            fail("main.py exists")
        else:
            result = _run_python(work_dir, "main.py")
            if result.returncode != 0:
                fail(
                    "main.py runs",
                    f"stdout: {result.stdout!r}  stderr: {result.stderr!r}",
                )
            elif "DONE" not in result.stdout:
                fail("main.py prints DONE", f"stdout: {result.stdout!r}")
            else:
                ok("main.py runs and prints DONE")

        # \u2500\u2500 Check no unrecoverable errors in the session
        errors = [e for e in session.events if isinstance(e, ErrorEvent) and not e.recoverable]
        if errors:
            fail("no unrecoverable errors", str([e.message for e in errors]))
        else:
            ok("no unrecoverable errors")

        # \u2500\u2500 Check the agent declared completion (checked last)
        if "ALL_TASKS_COMPLETED" not in assistant_text:
            fail("agent output ALL_TASKS_COMPLETED")
        else:
            ok("agent output ALL_TASKS_COMPLETED")

        # \u2500\u2500 Summary
        print(
            "\n\u2500\u2500 Session stats \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
        )
        print(f"   Steps: {session.step_count}")
        print(f"   Total tokens: {session.total_tokens}")
        print(f"   Total cost: ${session.total_cost:.4f}")

        if failures:
            print(f"\n\u274c {len(failures)} check(s) failed:")
            for f in failures:
                print(f"   \u2022 {f}")
            pytest.fail("\n".join(failures))
        else:
            print("\n\u2705 Plan execution test passed!")
