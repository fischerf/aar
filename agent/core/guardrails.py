"""Lean runtime guardrails — mechanical safety nets for the agent loop."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from agent.core.events import ToolCall, ToolResult
from agent.core.session import Session

_STATE_KEY = "guardrails"

_MAX_TOKENS_FOLLOWUP = (
    "Your previous response was truncated by the token limit. "
    "Continue from exactly where you left off. Do not restart or repeat content."
)

_PREMATURE_END_FOLLOWUP = (
    "You stopped without completing the task. The most recent tool outputs show "
    "errors or test failures that still need to be resolved. "
    "Re-read the failing output carefully, identify what's actually wrong "
    "(it may be different from what you assumed), and try a different approach. "
    "Do NOT repeat the same fix — try something materially different."
)

_BASH_FAILURE_PATTERNS = (
    "command not found",
    "not found",
    "No such file or directory",
    "ImportError",
    "ModuleNotFoundError",
    "SyntaxError",  # common from cmd.exe quoting issues
)

_ACP_TERMINAL_HINT = (
    "[System hint: The bash tool runs inside WSL which has a different environment "
    "from the Windows host. The acp_terminal tool is available and runs commands in "
    "the native Windows host environment with the user's PATH, Python, and installed "
    "tools. Try using acp_terminal instead.]"
)

_READ_ONLY_NUDGE = (
    "[System] You have spent many steps reading without taking action. "
    "Summarize what you've learned so far, formulate a concrete plan, and begin "
    "implementation. Do not read more files unless absolutely necessary for the next step."
)

_READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "list_directory",
        "grep",
        "find_files",
        "find_projects",
        "read_issue",
        "whoami",
        "list_my_issues",
    }
)


class GuardrailsConfig(BaseModel):
    """Tuning knobs for the mechanical guardrails."""

    max_tokens_recoveries: int = 2
    max_repeated_tool_steps: int = 3
    max_premature_end_recoveries: int = 2
    reserve_tokens: int = 512
    reserve_cost_fraction: float = 0.1
    bash_failure_threshold: int = 2
    read_only_loop_threshold: int = 8


_STATE_DEFAULTS: dict[str, Any] = {
    "max_tokens_recovery_count": 0,
    "premature_end_recovery_count": 0,
    "last_tool_signature": None,
    "repeated_tool_steps": 0,
    "near_budget_warned": False,
    "consecutive_bash_failures": 0,
    "bash_pivot_hinted": False,
    "consecutive_read_only_steps": 0,
    "read_only_nudge_given": False,
}


def _get_state(session: Session) -> dict[str, Any]:
    """Return (and lazily initialise) the guardrails sub-dict in session metadata.

    Sessions persisted before a guardrail field was introduced load with the
    old shape, so every key is filled in via ``setdefault`` on each call to
    keep legacy sessions from raising ``KeyError`` mid-loop. (#3)
    """
    state = session.metadata.setdefault(_STATE_KEY, {})
    for key, default in _STATE_DEFAULTS.items():
        state.setdefault(key, default)
    return state


class LoopGuardrails:
    """Stateless helper that reads/writes guardrail counters on a :class:`Session`.

    All mutable state lives in ``session.metadata["guardrails"]`` so that
    it is automatically persisted and restored with the session.
    """

    def __init__(self, config: GuardrailsConfig | None = None) -> None:
        self.config = config or GuardrailsConfig()

    # ------------------------------------------------------------------
    # Max-tokens recovery
    # ------------------------------------------------------------------

    def should_continue_after_max_tokens(self, session: Session) -> bool:
        """Return *True* if the loop may retry after a ``max_tokens`` stop.

        Each call increments the recovery counter.  Once the configured
        limit (default 2) is reached, returns *False*.
        """
        state = _get_state(session)
        if state["max_tokens_recovery_count"] >= self.config.max_tokens_recoveries:
            return False
        state["max_tokens_recovery_count"] += 1
        return True

    def max_tokens_followup(self) -> str:
        """Return the continuation prompt injected after a truncation."""
        return _MAX_TOKENS_FOLLOWUP

    # ------------------------------------------------------------------
    # Premature end_turn recovery
    # ------------------------------------------------------------------

    def should_continue_after_premature_end(self, session: Session, response_content: str) -> bool:
        """Return *True* if the loop should retry after an empty/minimal end_turn.

        Detects when the model stops with empty or very short content while
        recent tool results contain errors or test failures.  This prevents
        the agent from silently giving up mid-task.

        Each call increments the recovery counter.  Once the configured
        limit (default 2) is reached, returns *False*.
        """
        state = _get_state(session)
        if state["premature_end_recovery_count"] >= self.config.max_premature_end_recoveries:
            return False

        # Only trigger when the response is empty or trivially short (model gave up)
        if len(response_content.strip()) > 0:
            return False

        # Check if recent tool results contain failure indicators
        if not self._has_recent_failures(session):
            return False

        state["premature_end_recovery_count"] += 1
        return True

    def premature_end_followup(self) -> str:
        """Return the continuation prompt injected after a premature end_turn."""
        return _PREMATURE_END_FOLLOWUP

    @staticmethod
    def _has_recent_failures(session: Session) -> bool:
        """Check if the last few tool results contain errors or test failures."""
        # Look at the last 10 events for tool results with failure indicators
        failure_patterns = (
            "FAIL:",
            "Error",
            "error:",
            "Traceback",
            "AssertionError",
            "FAILED",
            "Exit code:",
        )
        recent_events = session.events[-10:] if len(session.events) > 10 else session.events
        for event in reversed(recent_events):
            if isinstance(event, ToolResult):
                if event.is_error:
                    return True
                if any(pat in event.output for pat in failure_patterns):
                    return True
        return False

    # ------------------------------------------------------------------
    # Repetition detection
    # ------------------------------------------------------------------

    def observe_tool_calls(self, session: Session, tool_calls: list[ToolCall]) -> None:
        """Record the current tool-call signature and update the repetition counter."""
        state = _get_state(session)
        signature = _tool_signature(tool_calls)
        if signature == state["last_tool_signature"]:
            state["repeated_tool_steps"] += 1
        else:
            state["repeated_tool_steps"] = 0
            state["last_tool_signature"] = signature

        # Track consecutive read-only steps
        all_read_only = all(tc.tool_name in _READ_ONLY_TOOLS for tc in tool_calls)
        if all_read_only:
            state["consecutive_read_only_steps"] += 1
        else:
            state["consecutive_read_only_steps"] = 0

    def is_stuck(self, session: Session) -> bool:
        """Return *True* when the same tool-call pattern has repeated too many times."""
        state = _get_state(session)
        return state["repeated_tool_steps"] >= self.config.max_repeated_tool_steps

    # ------------------------------------------------------------------
    # Read-only loop detection
    # ------------------------------------------------------------------

    def get_read_only_nudge(self, session: Session) -> str | None:
        """Return a nudge message if the agent is stuck in a read-only loop.

        Fires at most once per session.  Returns *None* if the threshold has
        not been reached or the nudge was already given.
        """
        state = _get_state(session)
        if state["read_only_nudge_given"]:
            return None
        if state["consecutive_read_only_steps"] >= self.config.read_only_loop_threshold:
            state["read_only_nudge_given"] = True
            return _READ_ONLY_NUDGE
        return None

    # ------------------------------------------------------------------
    # Budget proximity
    # ------------------------------------------------------------------

    def check_near_budget(
        self,
        session: Session,
        token_budget: int,
        cost_limit: float,
    ) -> bool:
        """Return *True* exactly once — the first step that enters budget proximity.

        Subsequent calls return *False* so only one warning is emitted per session.
        """
        state = _get_state(session)
        if state["near_budget_warned"]:
            return False
        if self.near_budget(session, token_budget, cost_limit):
            state["near_budget_warned"] = True
            return True
        return False

    def near_budget(
        self,
        session: Session,
        token_budget: int,
        cost_limit: float,
    ) -> bool:
        """Return *True* when remaining tokens or cost is within the reserve margin.

        Returns *False* immediately when the corresponding limit is zero
        (unlimited), so callers don't need to gate on that themselves.
        """
        if token_budget > 0:
            remaining_tokens = token_budget - session.total_tokens
            if remaining_tokens <= self.config.reserve_tokens:
                return True
        if cost_limit > 0:
            remaining_cost = cost_limit - session.total_cost
            if remaining_cost <= cost_limit * self.config.reserve_cost_fraction:
                return True
        return False

    # ------------------------------------------------------------------
    # Bash → acp_terminal pivot hint
    # ------------------------------------------------------------------

    def observe_tool_results(
        self, session: Session, results: list[ToolResult], registry_names: set[str]
    ) -> str | None:
        """Check tool results for patterns suggesting the agent should switch tools.

        Returns a hint string to inject as an internal user message, or None.
        Currently detects repeated bash failures when acp_terminal is available.
        """
        state = _get_state(session)

        # Only relevant when acp_terminal is registered
        if "acp_terminal" not in registry_names:
            return None

        # Already hinted once this session — don't nag
        if state["bash_pivot_hinted"]:
            return None

        # Count bash failures in this batch
        bash_failed = any(
            r.tool_name == "bash"
            and r.is_error
            and any(pat in r.output for pat in _BASH_FAILURE_PATTERNS)
            for r in results
        )

        if bash_failed:
            state["consecutive_bash_failures"] += 1
        else:
            # Reset on any successful bash call or non-bash results
            has_bash = any(r.tool_name == "bash" for r in results)
            if has_bash:
                state["consecutive_bash_failures"] = 0

        if state["consecutive_bash_failures"] >= self.config.bash_failure_threshold:
            state["bash_pivot_hinted"] = True
            return _ACP_TERMINAL_HINT

        return None


def _tool_signature(tool_calls: list[ToolCall]) -> str:
    """Derive a deterministic string from tool names and argument key-value pairs.

    Includes argument *values* so that calling the same tool with different
    arguments (e.g. reading different files) is not treated as a repetition.
    Values are truncated to keep the signature compact.
    """
    parts: list[str] = []
    for tc in tool_calls:
        items = sorted(tc.arguments.items())
        args = ",".join(f"{k}={_compact(v)}" for k, v in items)
        parts.append(f"{tc.tool_name}({args})")
    return ";".join(sorted(parts))


def _compact(value: object) -> str:
    """Truncate a value for signature comparison."""
    s = str(value)
    return s[:200] if len(s) > 200 else s
