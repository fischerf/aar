"""Tests for LoopGuardrails — bash→acp_terminal pivot hint and premature end recovery."""

from __future__ import annotations

from agent.core.events import ToolResult
from agent.core.guardrails import (
    _ACP_TERMINAL_HINT,
    _PREMATURE_END_FOLLOWUP,
    GuardrailsConfig,
    LoopGuardrails,
)
from agent.core.session import Session


def _bash_error(output: str = "bash: foo: command not found") -> ToolResult:
    return ToolResult(
        tool_call_id="tc1",
        tool_name="bash",
        output=output,
        is_error=True,
    )


def _bash_success(output: str = "OK") -> ToolResult:
    return ToolResult(
        tool_call_id="tc2",
        tool_name="bash",
        output=output,
        is_error=False,
    )


class TestBashPivotHint:
    """Tests for observe_tool_results bash→acp_terminal pivot detection."""

    def test_no_hint_without_acp_terminal(self) -> None:
        """Bash failures don't trigger hint when acp_terminal isn't registered."""
        guardrails = LoopGuardrails(GuardrailsConfig(bash_failure_threshold=2))
        session = Session()
        registry_names: set[str] = {"bash", "read_file"}

        results = [_bash_error()]
        assert guardrails.observe_tool_results(session, results, registry_names) is None

        # Even after exceeding threshold
        assert guardrails.observe_tool_results(session, results, registry_names) is None
        assert guardrails.observe_tool_results(session, results, registry_names) is None

    def test_hint_after_threshold(self) -> None:
        """Two consecutive bash 'command not found' failures trigger hint."""
        guardrails = LoopGuardrails(GuardrailsConfig(bash_failure_threshold=2))
        session = Session()
        registry_names: set[str] = {"bash", "acp_terminal", "read_file"}

        results = [_bash_error()]

        # First failure — no hint yet
        assert guardrails.observe_tool_results(session, results, registry_names) is None

        # Second failure — threshold reached
        hint = guardrails.observe_tool_results(session, results, registry_names)
        assert hint == _ACP_TERMINAL_HINT

    def test_hint_only_once(self) -> None:
        """Hint is only returned once per session."""
        guardrails = LoopGuardrails(GuardrailsConfig(bash_failure_threshold=1))
        session = Session()
        registry_names: set[str] = {"bash", "acp_terminal"}

        results = [_bash_error()]

        # First call triggers hint (threshold=1)
        hint = guardrails.observe_tool_results(session, results, registry_names)
        assert hint == _ACP_TERMINAL_HINT

        # Subsequent calls return None even with more failures
        assert guardrails.observe_tool_results(session, results, registry_names) is None
        assert guardrails.observe_tool_results(session, results, registry_names) is None

    def test_success_resets_counter(self) -> None:
        """A successful bash result resets the failure counter."""
        guardrails = LoopGuardrails(GuardrailsConfig(bash_failure_threshold=2))
        session = Session()
        registry_names: set[str] = {"bash", "acp_terminal"}

        error_results = [_bash_error()]
        success_results = [_bash_success()]

        # First failure
        assert guardrails.observe_tool_results(session, error_results, registry_names) is None

        # Success resets counter
        assert guardrails.observe_tool_results(session, success_results, registry_names) is None

        # Need two more failures now to trigger hint
        assert guardrails.observe_tool_results(session, error_results, registry_names) is None
        hint = guardrails.observe_tool_results(session, error_results, registry_names)
        assert hint == _ACP_TERMINAL_HINT

    def test_various_failure_patterns(self) -> None:
        """Different failure patterns all count toward the threshold."""
        guardrails = LoopGuardrails(GuardrailsConfig(bash_failure_threshold=2))
        session = Session()
        registry_names: set[str] = {"bash", "acp_terminal"}

        # First failure with "No such file or directory"
        results1 = [_bash_error("ls: /foo: No such file or directory")]
        assert guardrails.observe_tool_results(session, results1, registry_names) is None

        # Second failure with "ModuleNotFoundError"
        results2 = [_bash_error("ModuleNotFoundError: No module named 'foo'")]
        hint = guardrails.observe_tool_results(session, results2, registry_names)
        assert hint == _ACP_TERMINAL_HINT

    def test_non_matching_error_does_not_count(self) -> None:
        """Bash errors without matching patterns don't increment the counter."""
        guardrails = LoopGuardrails(GuardrailsConfig(bash_failure_threshold=2))
        session = Session()
        registry_names: set[str] = {"bash", "acp_terminal"}

        # Error without a matching pattern
        results = [_bash_error("permission denied")]
        assert guardrails.observe_tool_results(session, results, registry_names) is None
        assert guardrails.observe_tool_results(session, results, registry_names) is None
        # Still no hint — pattern didn't match
        assert guardrails.observe_tool_results(session, results, registry_names) is None


# ---------------------------------------------------------------------------
# Premature end_turn recovery tests
# ---------------------------------------------------------------------------


def _make_session_with_failure() -> Session:
    """Create a session with a recent tool result that shows test failures."""
    session = Session()
    session.append(
        ToolResult(
            tool_call_id="tc_test",
            tool_name="acp_terminal",
            output=(
                "...F.\n"
                "======================================================================\n"
                "FAIL: test_regex_invalid_formats (__main__.TestParser)\n"
                "----------------------------------------------------------------------\n"
                "Traceback (most recent call last):\n"
                '  File "test.py", line 26, in test_regex_invalid_formats\n'
                "AssertionError: Lists differ: ['TKN-ABC-1234'] != []\n"
            ),
            is_error=False,
        )
    )
    return session


def _make_session_clean() -> Session:
    """Create a session with only successful tool results."""
    session = Session()
    session.append(
        ToolResult(
            tool_call_id="tc_ok",
            tool_name="acp_terminal",
            output=".....\nOK (5 tests)",
            is_error=False,
        )
    )
    return session


class TestPrematureEndRecovery:
    """Tests for should_continue_after_premature_end detection."""

    def test_triggers_on_empty_content_with_failures(self) -> None:
        """Empty end_turn with recent test failures triggers recovery."""
        guardrails = LoopGuardrails(GuardrailsConfig(max_premature_end_recoveries=2))
        session = _make_session_with_failure()

        assert guardrails.should_continue_after_premature_end(session, "") is True

    def test_does_not_trigger_without_failures(self) -> None:
        """Empty end_turn without recent failures does NOT trigger recovery."""
        guardrails = LoopGuardrails(GuardrailsConfig(max_premature_end_recoveries=2))
        session = _make_session_clean()

        assert guardrails.should_continue_after_premature_end(session, "") is False

    def test_does_not_trigger_with_any_content(self) -> None:
        """End_turn with ANY content does NOT trigger even with failures."""
        guardrails = LoopGuardrails(GuardrailsConfig(max_premature_end_recoveries=2))
        session = _make_session_with_failure()

        # Even short content means the model deliberately ended
        assert guardrails.should_continue_after_premature_end(session, "I'm stuck.") is False

    def test_respects_max_recoveries(self) -> None:
        """Only triggers up to max_premature_end_recoveries times."""
        guardrails = LoopGuardrails(GuardrailsConfig(max_premature_end_recoveries=2))
        session = _make_session_with_failure()

        assert guardrails.should_continue_after_premature_end(session, "") is True
        assert guardrails.should_continue_after_premature_end(session, "") is True
        assert guardrails.should_continue_after_premature_end(session, "") is False

    def test_triggers_on_error_tool_result(self) -> None:
        """Triggers when tool result has is_error=True."""
        guardrails = LoopGuardrails(GuardrailsConfig(max_premature_end_recoveries=2))
        session = Session()
        session.append(
            ToolResult(
                tool_call_id="tc_err",
                tool_name="write_file",
                output="Error [blocked]: blocked by safety policy",
                is_error=True,
            )
        )

        assert guardrails.should_continue_after_premature_end(session, "") is True

    def test_followup_message(self) -> None:
        """The followup message is actionable and encourages a different approach."""
        guardrails = LoopGuardrails()
        msg = guardrails.premature_end_followup()
        assert msg == _PREMATURE_END_FOLLOWUP
        assert "different approach" in msg
