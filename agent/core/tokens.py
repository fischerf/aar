"""Token usage tracking, model pricing, and cost calculation utilities.

Provides typed Pydantic models for token usage and pricing, a built-in pricing
table for common models, and helper functions for cost calculation and display
formatting.  Designed as a drop-in replacement for the raw ``dict[str, int]``
token maps used elsewhere in the codebase.
"""

from __future__ import annotations

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Token usage model
# ---------------------------------------------------------------------------


class TokenUsage(BaseModel):
    """Typed token-usage counters — replaces raw ``dict[str, int]``."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        """Sum of input + output tokens (excludes cache breakdown)."""
        return self.input_tokens + self.output_tokens

    @classmethod
    def from_dict(cls, d: dict[str, int]) -> TokenUsage:
        """Build a ``TokenUsage`` from the legacy dict format.

        Recognises both the internal key names (``input_tokens``, …) and the
        OpenAI-style names (``prompt_tokens``, ``completion_tokens``).
        """
        return cls(
            input_tokens=d.get("input_tokens", d.get("prompt_tokens", 0)),
            output_tokens=d.get("output_tokens", d.get("completion_tokens", 0)),
            cache_read_tokens=d.get("cache_read_tokens", d.get("cache_read_input_tokens", 0)),
            cache_write_tokens=d.get("cache_write_tokens", d.get("cache_creation_input_tokens", 0)),
        )

    def to_dict(self) -> dict[str, int]:
        """Serialise back to the legacy dict format for backward compatibility."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
        }


# ---------------------------------------------------------------------------
# Model pricing
# ---------------------------------------------------------------------------


class ModelPricing(BaseModel):
    """Per-model pricing expressed in USD per 1 million tokens."""

    input_per_million: float = 0.0
    output_per_million: float = 0.0
    cache_read_per_million: float = 0.0
    cache_write_per_million: float = 0.0


# Pricing table — values are approximate and user-overridable.
# Sources: public pricing pages as of mid-2025.
PRICING_TABLE: dict[str, ModelPricing] = {
    # ── Anthropic ──────────────────────────────────────────────────────
    "claude-sonnet-4": ModelPricing(
        input_per_million=3.0,
        output_per_million=15.0,
        cache_read_per_million=0.30,
        cache_write_per_million=3.75,
    ),
    "claude-opus-4": ModelPricing(
        input_per_million=15.0,
        output_per_million=75.0,
        cache_read_per_million=1.50,
        cache_write_per_million=18.75,
    ),
    "claude-3-5-haiku": ModelPricing(
        input_per_million=1.0,
        output_per_million=5.0,
        cache_read_per_million=0.10,
        cache_write_per_million=1.25,
    ),
    "claude-3-5-sonnet": ModelPricing(
        input_per_million=3.0,
        output_per_million=15.0,
        cache_read_per_million=0.30,
        cache_write_per_million=3.75,
    ),
    # ── OpenAI ─────────────────────────────────────────────────────────
    "gpt-4o": ModelPricing(
        input_per_million=2.50,
        output_per_million=10.0,
        cache_read_per_million=1.25,
    ),
    "gpt-4o-mini": ModelPricing(
        input_per_million=0.15,
        output_per_million=0.60,
        cache_read_per_million=0.075,
    ),
    "gpt-4.1": ModelPricing(
        input_per_million=2.0,
        output_per_million=8.0,
        cache_read_per_million=0.50,
    ),
    "gpt-4.1-mini": ModelPricing(
        input_per_million=0.40,
        output_per_million=1.60,
        cache_read_per_million=0.10,
    ),
    "gpt-4.1-nano": ModelPricing(
        input_per_million=0.10,
        output_per_million=0.40,
        cache_read_per_million=0.025,
    ),
    "o3": ModelPricing(
        input_per_million=10.0,
        output_per_million=40.0,
        cache_read_per_million=2.50,
    ),
    "o3-mini": ModelPricing(
        input_per_million=1.10,
        output_per_million=4.40,
        cache_read_per_million=0.55,
    ),
    "o4-mini": ModelPricing(
        input_per_million=1.10,
        output_per_million=4.40,
        cache_read_per_million=0.275,
    ),
}


# ---------------------------------------------------------------------------
# Lookup / calculation helpers
# ---------------------------------------------------------------------------

# Sorted longest-first so that more-specific prefixes match before shorter ones.
_SORTED_KEYS: list[str] = sorted(PRICING_TABLE, key=len, reverse=True)


def get_pricing(model: str) -> ModelPricing | None:
    """Look up pricing for *model*, using prefix matching.

    For example, ``"claude-sonnet-4-20250514"`` matches the
    ``"claude-sonnet-4"`` key.  Returns ``None`` when no prefix matches.
    """
    for key in _SORTED_KEYS:
        if model.startswith(key):
            return PRICING_TABLE[key]
    return None


def calculate_cost(usage: TokenUsage, pricing: ModelPricing) -> float:
    """Return the estimated USD cost for the given *usage* and *pricing*."""
    return (
        usage.input_tokens * pricing.input_per_million
        + usage.output_tokens * pricing.output_per_million
        + usage.cache_read_tokens * pricing.cache_read_per_million
        + usage.cache_write_tokens * pricing.cache_write_per_million
    ) / 1_000_000


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_tokens(input_tokens: int, output_tokens: int) -> str:
    """Human-readable token summary, e.g. ``"150in / 80out"``."""
    return f"{input_tokens}in / {output_tokens}out"


def format_cost(cost: float) -> str:
    """Human-readable USD cost string.

    Uses four decimal places for sub-cent values (``$0.0032``), two for
    values ≥ $0.01 (``$1.23``), and whole-dollar formatting when the
    fractional part is negligible.
    """
    if cost < 0.01:
        return f"${cost:.4f}"
    return f"${cost:.2f}"
