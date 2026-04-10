"""Tests for token usage tracking, cost calculation, and budget enforcement."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.core.config import AgentConfig, ProviderConfig, SafetyConfig, ToolConfig
from agent.core.events import ErrorEvent
from agent.core.loop import run_loop
from agent.core.session import Session
from agent.core.state import AgentState
from agent.core.tokens import (
    ModelPricing,
    TokenUsage,
    calculate_cost,
    format_cost,
    format_tokens,
    get_pricing,
)
from agent.tools.execution import ToolExecutor
from agent.transports.tui_utils.formatting import format_token_display, is_over_warning_threshold
from tests.conftest import MockProvider

# ---------------------------------------------------------------------------
# TestTokenUsage
# ---------------------------------------------------------------------------


class TestTokenUsage:
    """Unit tests for the TokenUsage Pydantic model."""

    def test_from_dict_standard_keys(self) -> None:
        usage = TokenUsage.from_dict({"input_tokens": 100, "output_tokens": 50})
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50
        assert usage.cache_read_tokens == 0
        assert usage.cache_write_tokens == 0

    def test_from_dict_openai_keys(self) -> None:
        usage = TokenUsage.from_dict({"prompt_tokens": 100, "completion_tokens": 50})
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50

    def test_from_dict_cache_tokens(self) -> None:
        usage = TokenUsage.from_dict(
            {
                "input_tokens": 200,
                "output_tokens": 80,
                "cache_read_tokens": 50,
                "cache_write_tokens": 30,
            }
        )
        assert usage.input_tokens == 200
        assert usage.output_tokens == 80
        assert usage.cache_read_tokens == 50
        assert usage.cache_write_tokens == 30

    def test_from_dict_cache_tokens_anthropic_style(self) -> None:
        """Anthropic uses cache_read_input_tokens / cache_creation_input_tokens."""
        usage = TokenUsage.from_dict(
            {
                "input_tokens": 150,
                "output_tokens": 60,
                "cache_read_input_tokens": 40,
                "cache_creation_input_tokens": 20,
            }
        )
        assert usage.cache_read_tokens == 40
        assert usage.cache_write_tokens == 20

    def test_from_dict_empty(self) -> None:
        usage = TokenUsage.from_dict({})
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.cache_read_tokens == 0
        assert usage.cache_write_tokens == 0

    def test_total_property(self) -> None:
        usage = TokenUsage(input_tokens=120, output_tokens=80)
        assert usage.total == 200

    def test_total_excludes_cache(self) -> None:
        usage = TokenUsage(
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=30,
            cache_write_tokens=20,
        )
        assert usage.total == 150  # only input + output

    def test_to_dict_roundtrip(self) -> None:
        original = {"input_tokens": 100, "output_tokens": 50}
        usage = TokenUsage.from_dict(original)
        d = usage.to_dict()
        assert d["input_tokens"] == 100
        assert d["output_tokens"] == 50
        assert d["cache_read_tokens"] == 0
        assert d["cache_write_tokens"] == 0
        # Round-trip: from_dict on to_dict output should produce the same model
        roundtripped = TokenUsage.from_dict(d)
        assert roundtripped == usage


# ---------------------------------------------------------------------------
# TestModelPricing
# ---------------------------------------------------------------------------


class TestModelPricing:
    """Tests for pricing table lookup via get_pricing()."""

    def test_get_pricing_exact_match(self) -> None:
        pricing = get_pricing("gpt-5.4")
        assert pricing is not None
        assert pricing.input_per_million == 2.50
        assert pricing.output_per_million == 15.0

    def test_get_pricing_prefix_match(self) -> None:
        pricing = get_pricing("claude-sonnet-4-20250514")
        assert pricing is not None
        assert pricing.input_per_million == 3.0
        assert pricing.output_per_million == 15.0

    def test_get_pricing_no_match(self) -> None:
        pricing = get_pricing("unknown-model")
        assert pricing is None

    def test_get_pricing_prefers_longer_prefix(self) -> None:
        """More-specific prefix should win over shorter one."""
        pricing_mini = get_pricing("gpt-5.4-mini")
        pricing_base = get_pricing("gpt-5.4")
        assert pricing_mini is not None
        assert pricing_base is not None
        # gpt-5.4-mini is cheaper than gpt-5.4
        assert pricing_mini.input_per_million < pricing_base.input_per_million


# ---------------------------------------------------------------------------
# TestCostCalculation
# ---------------------------------------------------------------------------


class TestCostCalculation:
    """Tests for calculate_cost()."""

    def test_calculate_cost_basic(self) -> None:
        usage = TokenUsage(input_tokens=1000, output_tokens=0)
        pricing = ModelPricing(input_per_million=3.0, output_per_million=15.0)
        cost = calculate_cost(usage, pricing)
        assert cost == pytest.approx(0.003)

    def test_calculate_cost_output_tokens(self) -> None:
        usage = TokenUsage(input_tokens=0, output_tokens=1000)
        pricing = ModelPricing(input_per_million=3.0, output_per_million=15.0)
        cost = calculate_cost(usage, pricing)
        assert cost == pytest.approx(0.015)

    def test_calculate_cost_with_cache(self) -> None:
        usage = TokenUsage(
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=200,
            cache_write_tokens=100,
        )
        pricing = ModelPricing(
            input_per_million=3.0,
            output_per_million=15.0,
            cache_read_per_million=0.30,
            cache_write_per_million=3.75,
        )
        expected = (1000 * 3.0 + 500 * 15.0 + 200 * 0.30 + 100 * 3.75) / 1_000_000
        cost = calculate_cost(usage, pricing)
        assert cost == pytest.approx(expected)

    def test_calculate_cost_zero_usage(self) -> None:
        usage = TokenUsage()
        pricing = ModelPricing(input_per_million=3.0, output_per_million=15.0)
        cost = calculate_cost(usage, pricing)
        assert cost == 0.0


# ---------------------------------------------------------------------------
# TestFormatting
# ---------------------------------------------------------------------------


class TestFormatting:
    """Tests for format_tokens(), format_cost(), and TUI formatting helpers."""

    def test_format_tokens(self) -> None:
        result = format_tokens(150, 80)
        assert result == "150in / 80out"

    def test_format_tokens_zero(self) -> None:
        result = format_tokens(0, 0)
        assert result == "0in / 0out"

    def test_format_cost_small(self) -> None:
        result = format_cost(0.0032)
        assert result == "$0.0032"

    def test_format_cost_large(self) -> None:
        result = format_cost(1.23)
        assert result == "$1.23"

    def test_format_cost_exactly_one_cent(self) -> None:
        result = format_cost(0.01)
        assert result == "$0.01"

    def test_format_cost_zero(self) -> None:
        result = format_cost(0.0)
        assert result == "$0.0000"

    def test_format_token_display(self) -> None:
        result = format_token_display(150, 80, cost=0.0032, show_cost=True)
        assert "150in / 80out" in result
        assert "$0.0032" in result

    def test_format_token_display_large_cost(self) -> None:
        result = format_token_display(10000, 5000, cost=1.50, show_cost=True)
        assert "10000in / 5000out" in result
        assert "$1.50" in result

    def test_format_token_display_no_cost(self) -> None:
        result = format_token_display(150, 80, cost=0.0, show_cost=True)
        assert result == "150in / 80out"
        assert "$" not in result

    def test_format_token_display_show_cost_false(self) -> None:
        result = format_token_display(150, 80, cost=0.50, show_cost=False)
        assert result == "150in / 80out"
        assert "$" not in result


# ---------------------------------------------------------------------------
# TestWarningThreshold
# ---------------------------------------------------------------------------


class TestWarningThreshold:
    """Tests for is_over_warning_threshold()."""

    def test_over_threshold(self) -> None:
        assert is_over_warning_threshold(80, 100, 0.8) is True

    def test_exactly_at_threshold(self) -> None:
        assert is_over_warning_threshold(80, 100, 0.8) is True

    def test_under_threshold(self) -> None:
        assert is_over_warning_threshold(70, 100, 0.8) is False

    def test_zero_limit(self) -> None:
        """Limit=0 means unlimited — never warn."""
        assert is_over_warning_threshold(999, 0, 0.8) is False

    def test_negative_limit(self) -> None:
        assert is_over_warning_threshold(999, -1, 0.8) is False

    def test_custom_threshold(self) -> None:
        assert is_over_warning_threshold(50, 100, 0.5) is True
        assert is_over_warning_threshold(49, 100, 0.5) is False

    def test_threshold_one(self) -> None:
        """Threshold of 1.0 means warn only at/above the full limit."""
        assert is_over_warning_threshold(99, 100, 1.0) is False
        assert is_over_warning_threshold(100, 100, 1.0) is True


# ---------------------------------------------------------------------------
# TestBudgetEnforcement — async loop-level tests
# ---------------------------------------------------------------------------


class TestBudgetEnforcement:
    """Integration tests for token budget and cost limit enforcement in run_loop."""

    @pytest.mark.asyncio
    async def test_token_budget_stops_loop(
        self, mock_provider: MockProvider, tool_registry
    ) -> None:
        """Loop should stop with BUDGET_EXCEEDED when total tokens >= token_budget.

        Tool-call response: {input_tokens: 10, output_tokens: 15} = 25 tokens.
        Text response:      {input_tokens: 10, output_tokens: 5}  = 15 tokens.
        With token_budget=30, step 1 (tool call, 25 total) is fine, but step 2
        (text, 40 total >= 30) triggers BUDGET_EXCEEDED.
        """
        mock_provider.enqueue_tool_call("echo", {"message": "ping"}, "tc_1")
        mock_provider.enqueue_text("Done")
        mock_provider.enqueue_text("Should not be reached")

        session = Session()
        session.add_user_message("Go")

        config = AgentConfig(
            provider=ProviderConfig(name="mock", model="mock-1"),
            max_steps=10,
            timeout=30.0,
            token_budget=30,
        )
        executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
        result = await run_loop(session, mock_provider, executor, config)

        assert result.state == AgentState.BUDGET_EXCEEDED
        assert result.total_tokens >= 30
        # Step 1: tool call (25 tokens), step 2: text (15 tokens) = 40 total
        assert len(mock_provider.call_history) == 2
        # An error event about budget should be present
        errors = [e for e in result.events if isinstance(e, ErrorEvent)]
        assert any("budget" in e.message.lower() for e in errors)

    @pytest.mark.asyncio
    async def test_cost_limit_stops_loop(self, mock_provider: MockProvider, tool_registry) -> None:
        """Loop should stop with BUDGET_EXCEEDED when total cost >= cost_limit.

        We patch get_pricing to return a known pricing for "mock-1" so cost
        calculation works.  Tool-call step costs
          (10*3 + 15*15) / 1_000_000 = $0.000255.
        Text step costs (10*3 + 5*15) / 1_000_000 = $0.000105.
        Set cost_limit to $0.0003 so step 2 (total $0.000360) exceeds it.
        """
        mock_pricing = ModelPricing(input_per_million=3.0, output_per_million=15.0)

        mock_provider.enqueue_tool_call("echo", {"message": "hi"}, "tc_1")
        mock_provider.enqueue_text("Done")
        mock_provider.enqueue_text("Should not be reached")

        session = Session()
        session.add_user_message("Go")

        config = AgentConfig(
            provider=ProviderConfig(name="mock", model="mock-1"),
            max_steps=10,
            timeout=30.0,
            cost_limit=0.0003,
        )
        executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())

        with patch("agent.core.tokens.get_pricing", return_value=mock_pricing):
            result = await run_loop(session, mock_provider, executor, config)

        assert result.state == AgentState.BUDGET_EXCEEDED
        assert result.total_cost >= 0.0003
        errors = [e for e in result.events if isinstance(e, ErrorEvent)]
        assert any("cost" in e.message.lower() for e in errors)

    @pytest.mark.asyncio
    async def test_no_budget_no_limit(
        self, mock_provider: MockProvider, tool_registry, default_config: AgentConfig
    ) -> None:
        """With defaults (budget=0, cost_limit=0.0), loop runs to completion."""
        mock_provider.enqueue_text("Hello!")

        session = Session()
        session.add_user_message("Hi")

        executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
        result = await run_loop(session, mock_provider, executor, default_config)

        assert result.state == AgentState.COMPLETED
        assert result.step_count == 1
        # Tokens should still be accumulated even without a budget
        assert result.total_input_tokens == 10
        assert result.total_output_tokens == 5

    @pytest.mark.asyncio
    async def test_token_accumulation_across_steps(
        self, mock_provider: MockProvider, tool_registry
    ) -> None:
        """Tokens from both tool-call and text steps should accumulate.

        Tool-call response: {input_tokens: 10, output_tokens: 15} = 25
        Text response:      {input_tokens: 10, output_tokens: 5}  = 15
        Total: 40 tokens
        """
        mock_provider.enqueue_tool_call("echo", {"message": "ping"}, "tc_1")
        mock_provider.enqueue_text("Done: echo: ping")

        session = Session()
        session.add_user_message("Call echo")

        config = AgentConfig(
            provider=ProviderConfig(name="mock", model="mock-1"),
            max_steps=10,
            timeout=30.0,
        )
        executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
        result = await run_loop(session, mock_provider, executor, config)

        assert result.state == AgentState.COMPLETED
        # Tool call step: 10 in + 15 out; text step: 10 in + 5 out
        assert result.total_input_tokens == 20
        assert result.total_output_tokens == 20
        assert result.total_tokens == 40

    @pytest.mark.asyncio
    async def test_session_tracks_cost(self, mock_provider: MockProvider, tool_registry) -> None:
        """Session.total_cost should be positive when pricing is available."""
        mock_pricing = ModelPricing(
            input_per_million=3.0,
            output_per_million=15.0,
        )
        mock_provider.enqueue_text("Response")

        session = Session()
        session.add_user_message("Hi")

        config = AgentConfig(
            provider=ProviderConfig(name="mock", model="mock-1"),
            max_steps=10,
            timeout=30.0,
        )
        executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())

        with patch("agent.core.tokens.get_pricing", return_value=mock_pricing):
            result = await run_loop(session, mock_provider, executor, config)

        assert result.state == AgentState.COMPLETED
        # (10 * 3.0 + 5 * 15.0) / 1_000_000 = 0.000105
        expected_cost = (10 * 3.0 + 5 * 15.0) / 1_000_000
        assert result.total_cost == pytest.approx(expected_cost)

    @pytest.mark.asyncio
    async def test_session_cost_zero_without_pricing(
        self, mock_provider: MockProvider, tool_registry
    ) -> None:
        """When get_pricing returns None, session.total_cost stays 0."""
        mock_provider.enqueue_text("Hello")

        session = Session()
        session.add_user_message("Hi")

        config = AgentConfig(
            provider=ProviderConfig(name="mock", model="mock-1"),
            max_steps=10,
            timeout=30.0,
        )
        executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())

        # "mock-1" has no entry in PRICING_TABLE, so get_pricing returns None
        result = await run_loop(session, mock_provider, executor, config)

        assert result.state == AgentState.COMPLETED
        assert result.total_cost == 0.0
        # Tokens should still be tracked
        assert result.total_tokens == 15

    @pytest.mark.asyncio
    async def test_budget_exceeded_emits_stop_reason(
        self, mock_provider: MockProvider, tool_registry
    ) -> None:
        """The error event emitted on budget exceeded should not be recoverable."""
        # Tool-call keeps the loop running; text step triggers budget check
        mock_provider.enqueue_tool_call("echo", {"message": "hi"}, "tc_1")
        mock_provider.enqueue_text("Done")

        session = Session()
        session.add_user_message("Go")

        config = AgentConfig(
            provider=ProviderConfig(name="mock", model="mock-1"),
            max_steps=10,
            timeout=30.0,
            token_budget=30,  # tool call (25) OK, text (25+15=40) >= 30 → exceeded
        )
        executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
        result = await run_loop(session, mock_provider, executor, config)

        assert result.state == AgentState.BUDGET_EXCEEDED
        errors = [e for e in result.events if isinstance(e, ErrorEvent)]
        budget_errors = [e for e in errors if "budget" in e.message.lower()]
        assert len(budget_errors) == 1
        assert budget_errors[0].recoverable is False

    @pytest.mark.asyncio
    async def test_token_budget_exact_boundary(
        self, mock_provider: MockProvider, tool_registry
    ) -> None:
        """Budget check uses >= so hitting the limit exactly also triggers."""
        # Each text response = 15 tokens.  Budget = 15 → first call hits it.
        mock_provider.enqueue_text("One")
        mock_provider.enqueue_text("Two")  # should not be reached

        session = Session()
        session.add_user_message("Go")

        config = AgentConfig(
            provider=ProviderConfig(name="mock", model="mock-1"),
            max_steps=10,
            timeout=30.0,
            token_budget=15,
        )
        executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
        result = await run_loop(session, mock_provider, executor, config)

        assert result.state == AgentState.BUDGET_EXCEEDED
        assert result.total_tokens == 15
        assert len(mock_provider.call_history) == 1


# ---------------------------------------------------------------------------
# TestConfigDefaults — ensure new config fields have sensible defaults
# ---------------------------------------------------------------------------


class TestConfigDefaults:
    """Verify that the new token/cost config fields default to disabled."""

    def test_default_token_budget(self) -> None:
        config = AgentConfig(provider=ProviderConfig(name="mock", model="mock-1"))
        assert config.token_budget == 0

    def test_default_cost_limit(self) -> None:
        config = AgentConfig(provider=ProviderConfig(name="mock", model="mock-1"))
        assert config.cost_limit == 0.0

    def test_default_warning_thresholds(self) -> None:
        config = AgentConfig(provider=ProviderConfig(name="mock", model="mock-1"))
        assert config.token_warning_threshold == 0.8
        assert config.cost_warning_threshold == 0.8


# ---------------------------------------------------------------------------
# TestSessionTokenFields — ensure Session fields initialise correctly
# ---------------------------------------------------------------------------


class TestSessionTokenFields:
    """Verify Session token tracking fields."""

    def test_session_defaults(self) -> None:
        session = Session()
        assert session.total_input_tokens == 0
        assert session.total_output_tokens == 0
        assert session.total_cost == 0.0
        assert session.total_tokens == 0

    def test_session_total_tokens_property(self) -> None:
        session = Session()
        session.total_input_tokens = 100
        session.total_output_tokens = 50
        assert session.total_tokens == 150


# ---------------------------------------------------------------------------
# TestStreamingTokenTracking
# ---------------------------------------------------------------------------


class TestStreamingTokenTracking:
    """Tests that token usage flows through streaming providers correctly.

    The root cause of tokens not displaying was that _consume_stream() in
    loop.py returned ProviderResponse(meta=None) — the StreamDelta had no
    way to carry usage data.  These tests verify the fix end-to-end.
    """

    @pytest.mark.asyncio
    async def test_streaming_tokens_accumulated_on_session(self, tool_registry) -> None:
        """Streaming run should accumulate tokens on the session."""
        from agent.core.events import ProviderMeta
        from agent.providers.base import StreamDelta
        from tests.conftest import StreamingMockProvider

        provider = StreamingMockProvider()
        meta = ProviderMeta(
            provider="mock_stream",
            model="mock-stream-1",
            usage={"input_tokens": 42, "output_tokens": 18},
        )
        provider.enqueue_stream(
            [
                StreamDelta(text="Hello "),
                StreamDelta(text="world!"),
                StreamDelta(done=True, meta=meta),
            ]
        )

        config = AgentConfig(
            provider=ProviderConfig(name="mock_stream", model="mock-stream-1"),
            max_steps=10,
            timeout=30.0,
            streaming=True,
        )
        session = Session()
        session.add_user_message("Hi")

        executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
        result = await run_loop(session, provider, executor, config)

        assert result.state == AgentState.COMPLETED
        assert result.total_input_tokens == 42
        assert result.total_output_tokens == 18
        assert result.total_tokens == 60

    @pytest.mark.asyncio
    async def test_streaming_emits_provider_meta_event(self, tool_registry) -> None:
        """Streaming run should emit a ProviderMeta event with usage data."""
        from agent.core.events import ProviderMeta
        from agent.providers.base import StreamDelta
        from tests.conftest import StreamingMockProvider

        provider = StreamingMockProvider()
        meta = ProviderMeta(
            provider="mock_stream",
            model="mock-stream-1",
            usage={"input_tokens": 100, "output_tokens": 50},
        )
        provider.enqueue_stream(
            [
                StreamDelta(text="response"),
                StreamDelta(done=True, meta=meta),
            ]
        )

        config = AgentConfig(
            provider=ProviderConfig(name="mock_stream", model="mock-stream-1"),
            max_steps=10,
            timeout=30.0,
            streaming=True,
        )
        session = Session()
        session.add_user_message("Hi")

        events_received: list = []

        def capture(event):
            events_received.append(event)

        executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
        await run_loop(session, provider, executor, config, on_event=capture)

        meta_events = [e for e in events_received if isinstance(e, ProviderMeta)]
        assert len(meta_events) == 1
        assert meta_events[0].usage["input_tokens"] == 100
        assert meta_events[0].usage["output_tokens"] == 50

    @pytest.mark.asyncio
    async def test_streaming_without_meta_still_works(self, tool_registry) -> None:
        """Streaming run without meta on the final delta should still complete."""
        from agent.providers.base import StreamDelta
        from tests.conftest import StreamingMockProvider

        provider = StreamingMockProvider()
        provider.enqueue_stream(
            [
                StreamDelta(text="no meta here"),
                StreamDelta(done=True),  # no meta
            ]
        )

        config = AgentConfig(
            provider=ProviderConfig(name="mock_stream", model="mock-stream-1"),
            max_steps=10,
            timeout=30.0,
            streaming=True,
        )
        session = Session()
        session.add_user_message("Hi")

        executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
        result = await run_loop(session, provider, executor, config)

        assert result.state == AgentState.COMPLETED
        assert result.total_input_tokens == 0
        assert result.total_output_tokens == 0

    @pytest.mark.asyncio
    async def test_streaming_token_budget_enforced(self, tool_registry) -> None:
        """Token budget should be enforced during streaming runs."""
        from agent.core.events import ProviderMeta
        from agent.providers.base import StreamDelta
        from tests.conftest import StreamingMockProvider

        provider = StreamingMockProvider()
        # First call: 25 tokens (exceeds budget of 20)
        meta1 = ProviderMeta(
            provider="mock_stream",
            model="mock-stream-1",
            usage={"input_tokens": 15, "output_tokens": 10},
        )
        provider.enqueue_stream(
            [
                StreamDelta(text="first"),
                StreamDelta(done=True, meta=meta1),
            ]
        )
        # Second call should never happen
        provider.enqueue_stream(
            [
                StreamDelta(text="second"),
                StreamDelta(done=True, meta=meta1),
            ]
        )

        config = AgentConfig(
            provider=ProviderConfig(name="mock_stream", model="mock-stream-1"),
            max_steps=10,
            timeout=30.0,
            streaming=True,
            token_budget=20,
        )
        session = Session()
        session.add_user_message("Hi")

        executor = ToolExecutor(tool_registry, ToolConfig(), SafetyConfig())
        result = await run_loop(session, provider, executor, config)

        assert result.state == AgentState.BUDGET_EXCEEDED
        assert result.total_tokens == 25
        assert result.step_count == 1
