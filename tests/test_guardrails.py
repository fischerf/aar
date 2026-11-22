"""Tests for LoopGuardrails — shell→acp_terminal pivot hint and premature end recovery."""

from __future__ import annotations

from agent.core.events import ToolCall, ToolResult
from agent.core.guardrails import (
    _ACP_TERMINAL_HINT,
    _ACP_TERMINAL_HINT_POWERSHELL,
    _ACP_TERMINAL_HINT_SHELLS,
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


# What the shell tools return for a failed command: output, then "Exit code: N".
_PS_NOT_FOUND = (
    "STDERR:\nfoo : The term 'foo' is not recognized as the name of a cmdlet, function,"
    " script file, or operable program.\n    + CategoryInfo          : ObjectNotFound:"
    " (foo:String) [], CommandNotFoundException\n\nExit code: 1"
)


def _result(tool: str, output: str, is_error: bool = False) -> ToolResult:
    return ToolResult(tool_call_id="tc", tool_name=tool, output=output, is_error=is_error)


class TestPowerShellPivotHint:
    """powershell failures count toward the acp_terminal hint exactly like bash."""

    names = frozenset({"powershell", "acp_terminal"})

    def _guardrails(self, threshold: int = 2) -> LoopGuardrails:
        return LoopGuardrails(GuardrailsConfig(bash_failure_threshold=threshold))

    def test_powershell_failures_trigger_powershell_hint(self) -> None:
        g, session = self._guardrails(), Session()
        assert (
            g.observe_tool_results(session, [_result("powershell", _PS_NOT_FOUND)], self.names)
            is None
        )
        hint = g.observe_tool_results(session, [_result("powershell", _PS_NOT_FOUND)], self.names)
        assert hint == _ACP_TERMINAL_HINT_POWERSHELL
        assert "powershell" in hint and "WSL" not in hint and "bash" not in hint

    def test_powershell_error_result_counts(self) -> None:
        g, session = self._guardrails(1), Session()
        err = _result("powershell", "Error [exception]: ModuleNotFoundError: x", is_error=True)
        assert g.observe_tool_results(session, [err], self.names) == _ACP_TERMINAL_HINT_POWERSHELL

    def test_no_hint_without_acp_terminal(self) -> None:
        g, session = self._guardrails(1), Session()
        assert (
            g.observe_tool_results(session, [_result("powershell", _PS_NOT_FOUND)], {"powershell"})
            is None
        )

    def test_powershell_success_resets_counter(self) -> None:
        g, session = self._guardrails(), Session()
        fail, ok = _result("powershell", _PS_NOT_FOUND), _result("powershell", "ok\n")
        assert g.observe_tool_results(session, [fail], self.names) is None
        assert g.observe_tool_results(session, [ok], self.names) is None
        assert g.observe_tool_results(session, [fail], self.names) is None
        assert g.observe_tool_results(session, [fail], self.names) == _ACP_TERMINAL_HINT_POWERSHELL

    def test_other_tools_do_not_reset(self) -> None:
        g, session = self._guardrails(), Session()
        fail = _result("powershell", _PS_NOT_FOUND)
        assert g.observe_tool_results(session, [fail], self.names) is None
        assert g.observe_tool_results(session, [_result("read_file", "x")], self.names) is None
        assert g.observe_tool_results(session, [fail], self.names) == _ACP_TERMINAL_HINT_POWERSHELL

    def test_pattern_without_failure_does_not_count(self) -> None:
        """Exit code 0 output that merely mentions "not found" is not a failure."""
        g, session = self._guardrails(1), Session()
        out = _result("powershell", "Cannot find path: 3 entries not found\n")
        assert g.observe_tool_results(session, [out], self.names) is None
        zero = _result("bash", "grep: not found\n\nExit code: 0")
        assert g.observe_tool_results(session, [zero], {"bash", "acp_terminal"}) is None

    def test_failure_without_pattern_does_not_count(self) -> None:
        g, session = self._guardrails(1), Session()
        out = _result("powershell", "STDERR:\nAccess denied\n\nExit code: 5")
        assert g.observe_tool_results(session, [out], self.names) is None

    def test_bash_nonzero_exit_counts_without_is_error(self) -> None:
        """The bash tool reports a failed command as text + exit code, not is_error."""
        g, session = self._guardrails(), Session()
        out = _result("bash", "STDERR:\nbash: foo: command not found\n\nExit code: 127")
        names = {"bash", "acp_terminal"}
        assert g.observe_tool_results(session, [out], names) is None
        assert g.observe_tool_results(session, [out], names) == _ACP_TERMINAL_HINT

    def test_mixed_bash_and_powershell_share_the_counter(self) -> None:
        g, session = self._guardrails(), Session()
        names = {"bash", "powershell", "acp_terminal"}
        assert g.observe_tool_results(session, [_bash_error()], names) is None
        hint = g.observe_tool_results(session, [_result("powershell", _PS_NOT_FOUND)], names)
        assert hint == _ACP_TERMINAL_HINT_SHELLS
        assert "bash" in hint and "powershell" in hint
        assert g.observe_tool_results(session, [_bash_error()], names) is None  # once only

    def test_legacy_state_without_failed_tools_key(self) -> None:
        g, session = self._guardrails(1), Session()
        session.metadata["guardrails"] = {"consecutive_bash_failures": 0}
        hint = g.observe_tool_results(session, [_result("powershell", _PS_NOT_FOUND)], self.names)
        assert hint == _ACP_TERMINAL_HINT_POWERSHELL

    def test_state_lists_are_not_shared_between_sessions(self) -> None:
        g = self._guardrails(3)
        first, second = Session(), Session()
        g.observe_tool_results(first, [_result("powershell", _PS_NOT_FOUND)], self.names)
        g.observe_tool_results(second, [_bash_error()], {"bash", "acp_terminal"})
        assert first.metadata["guardrails"]["shell_failure_tools"] == ["powershell"]
        assert second.metadata["guardrails"]["shell_failure_tools"] == ["bash"]


# ---------------------------------------------------------------------------
# Legacy session metadata backfill (#3)
# ---------------------------------------------------------------------------


class TestLegacyMetadataState:
    """#3 — _get_state must tolerate sessions persisted before new keys existed."""

    def test_empty_legacy_state_does_not_raise(self) -> None:
        guardrails = LoopGuardrails()
        session = Session()
        # Simulate a session saved before new guardrail fields were added.
        session.metadata["guardrails"] = {}

        tc = ToolCall(tool_call_id="tc1", tool_name="read_file", arguments={"path": "a"})
        # Without #3 this would raise KeyError on the missing
        # 'consecutive_read_only_steps' / 'last_tool_signature' keys.
        guardrails.observe_tool_calls(session, [tc])

        state = session.metadata["guardrails"]
        assert state["consecutive_read_only_steps"] == 1
        assert "read_only_nudge_given" in state
        assert "bash_pivot_hinted" in state
        assert "max_tokens_recovery_count" in state

    def test_partial_legacy_state_keeps_existing_values(self) -> None:
        """Existing counters survive backfill — only missing keys get defaults."""
        guardrails = LoopGuardrails()
        session = Session()
        session.metadata["guardrails"] = {
            # Pre-existing field with non-default value
            "max_tokens_recovery_count": 2,
        }

        # should_continue_after_max_tokens reads max_tokens_recovery_count.
        # If backfill clobbered it, this would return True (counter back to 0).
        result = guardrails.should_continue_after_max_tokens(session)
        assert result is False  # already at the default limit (2)

    def test_pivot_hint_works_on_legacy_state(self) -> None:
        """bash_pivot_hinted / consecutive_bash_failures backfill cleanly."""
        guardrails = LoopGuardrails(GuardrailsConfig(bash_failure_threshold=1))
        session = Session()
        session.metadata["guardrails"] = {}

        hint = guardrails.observe_tool_results(session, [_bash_error()], {"bash", "acp_terminal"})
        assert hint == _ACP_TERMINAL_HINT


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
