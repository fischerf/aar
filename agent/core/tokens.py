"""Token usage tracking, model pricing, and cost calculation utilities.

Provides typed Pydantic models for token usage and pricing, JSON-driven pricing
loading (from the built-in ``agent/core/pricing.json`` and an optional user
override at ``~/.aar/pricing.json``), and helper functions for cost calculation
and display formatting.  Designed as a drop-in replacement for the raw
``dict[str, int]`` token maps used elsewhere in the codebase.

Pricing table loading
---------------------
On first access, :func:`get_pricing` loads the pricing table from two sources
(later entries win):

1. ``<package>/agent/core/pricing.json`` — built-in baseline, shipped with Aar.
2. ``~/.aar/pricing.json``              — user overrides, loaded if the file exists.

Both files share the same flat JSON schema::

    {
      "_comment": "ignored — keys starting with _ are skipped",
      "claude-sonnet-4": {
          "input_per_million": 3.0,
          "output_per_million": 15.0,
          "cache_read_per_million": 0.30,
          "cache_write_per_million": 3.75
      }
    }

Call :func:`reload_pricing_table` to invalidate the cache (useful in tests or
after the user edits ``~/.aar/pricing.json`` while the process is running).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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


# ---------------------------------------------------------------------------
# Pricing table — loaded lazily from JSON, never hardcoded here
# ---------------------------------------------------------------------------

# Internal cache: (table, sorted_keys) tuple, or None when not yet loaded.
_cache: tuple[dict[str, ModelPricing], list[str]] | None = None

# Path to the built-in pricing file bundled alongside this module.
_BUILTIN_PRICING_PATH: Path = Path(__file__).parent / "pricing.json"

# Path to the optional user-level override.
_USER_PRICING_PATH: Path = Path.home() / ".aar" / "pricing.json"


def get_builtin_pricing_path() -> Path:
    """Return the path to the built-in ``pricing.json`` shipped with Aar.

    This is the JSON file that provides the baseline pricing table before any
    user overrides are applied.  Useful for ``aar init`` when generating the
    ``pricing.template.json`` reference file.
    """
    return _BUILTIN_PRICING_PATH


def _parse_pricing_file(path: Path) -> dict[str, ModelPricing]:
    """Parse a pricing JSON file and return a ``{prefix: ModelPricing}`` dict.

    Keys that start with ``_`` are silently ignored — they are treated as
    in-JSON comments (e.g. ``"_comment": "…"``).  Missing or unreadable files
    return an empty dict without raising.
    """
    if not path.is_file():
        return {}
    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    result: dict[str, ModelPricing] = {}
    for key, value in raw.items():
        if key.startswith("_") or not isinstance(value, dict):
            continue
        try:
            result[key] = ModelPricing(**value)
        except Exception:
            # Skip malformed entries rather than crashing at startup.
            pass
    return result


def _build_pricing_table() -> tuple[dict[str, ModelPricing], list[str]]:
    """Load and merge pricing from built-in + user sources.

    Priority: user ``~/.aar/pricing.json`` > built-in ``pricing.json``.
    Returns ``(table, sorted_keys)`` where *sorted_keys* is sorted longest-first
    for prefix matching.
    """
    table: dict[str, ModelPricing] = {}
    # 1. Built-in baseline
    table.update(_parse_pricing_file(_BUILTIN_PRICING_PATH))
    # 2. User override (merged on top)
    table.update(_parse_pricing_file(_USER_PRICING_PATH))

    sorted_keys = sorted(table, key=len, reverse=True)
    return table, sorted_keys


def _get_cache() -> tuple[dict[str, ModelPricing], list[str]]:
    """Return the cached pricing table, loading it on first access."""
    global _cache
    if _cache is None:
        _cache = _build_pricing_table()
    return _cache


def reload_pricing_table() -> None:
    """Invalidate the pricing table cache so it is reloaded on next access.

    Call this after editing ``~/.aar/pricing.json`` while the process is running,
    or in tests that need to inject a custom pricing table.
    """
    global _cache
    _cache = None


# ---------------------------------------------------------------------------
# Lookup / calculation helpers
# ---------------------------------------------------------------------------


def get_pricing(model: str) -> ModelPricing | None:
    """Look up pricing for *model*, using prefix matching.

    For example, ``"claude-sonnet-4-20250514"`` matches the
    ``"claude-sonnet-4"`` key.  Returns ``None`` when no prefix matches.

    Pricing data is loaded lazily from ``agent/core/pricing.json`` (built-in)
    and ``~/.aar/pricing.json`` (user override) on first call.
    """
    table, sorted_keys = _get_cache()
    for key in sorted_keys:
        if model.startswith(key):
            return table[key]
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
