"""Per-model token pricing (USD per 1M tokens) + cost computation.

EDITABLE — update these to match your actual Anthropic contract. The
defaults mirror public Sonnet / Haiku tier list pricing; unknown model
names fall back by tier keyword, then to the Sonnet tier (conservative).

Anthropic `usage` reports four buckets that are priced differently:
  - input_tokens          : fresh (non-cached) input        → "input"
  - cache_creation_*       : tokens written into the cache    → "cache_write"
  - cache_read_*           : tokens served from the cache     → "cache_read"
  - output_tokens          : generated output                 → "output"
`input_tokens` already EXCLUDES the cached buckets, so we price each
separately and sum.
"""

from __future__ import annotations

# USD per 1,000,000 tokens.
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {
        "input": 3.00,
        "cache_write": 3.75,
        "cache_read": 0.30,
        "output": 15.00,
    },
    "claude-haiku-4-5": {
        "input": 0.80,
        "cache_write": 1.00,
        "cache_read": 0.08,
        "output": 4.00,
    },
}

_SONNET = MODEL_PRICING["claude-sonnet-4-6"]
_HAIKU = MODEL_PRICING["claude-haiku-4-5"]


def _rates(model: str) -> dict[str, float]:
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    low = (model or "").lower()
    if "haiku" in low:
        return _HAIKU
    # sonnet / opus / anything else → Sonnet tier (conservative).
    return _SONNET


def compute_cost(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """USD cost for one call, rounded to 6 dp."""
    r = _rates(model)
    total = (
        input_tokens * r["input"]
        + cache_creation_tokens * r["cache_write"]
        + cache_read_tokens * r["cache_read"]
        + output_tokens * r["output"]
    ) / 1_000_000.0
    return round(total, 6)
