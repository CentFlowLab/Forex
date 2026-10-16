"""Approximate public list prices for cost tracking. Update as vendors change."""

from __future__ import annotations

# USD per 1M tokens: (input, output)
PRICE_TABLE: dict[str, tuple[float, float]] = {
    "grok-4-latest": (3.0, 15.0),
    "grok-3": (3.0, 15.0),
    "grok-3-mini": (0.30, 0.50),
    "grok-2-latest": (2.0, 10.0),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "o4-mini": (1.10, 4.40),
    "claude-sonnet-4-20250514": (3.00, 15.00),
    "claude-3-7-sonnet-latest": (3.00, 15.00),
    "claude-3-5-sonnet-latest": (3.00, 15.00),
    "claude-3-5-haiku-latest": (0.80, 4.00),
}


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    inp, out = PRICE_TABLE.get(model, (2.0, 8.0))
    return (prompt_tokens / 1_000_000.0) * inp + (completion_tokens / 1_000_000.0) * out
