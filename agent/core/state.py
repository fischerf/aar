"""Agent runtime state."""

from enum import Enum


class AgentState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_FOR_TOOL = "waiting_for_tool"
    WAITING_FOR_INPUT = "waiting_for_input"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    MAX_STEPS = "max_steps"
    BUDGET_EXCEEDED = "budget_exceeded"
