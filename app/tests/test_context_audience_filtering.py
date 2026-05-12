"""Unit tests for the audience-filtering safety net in
app/services/ai/context.py.

The contract: when audience='client', NO context block we generate
should leak basis points, spreads, base-rate-vs-final-rate breakdowns,
or other markup mechanics. These tests don't hit a DB — they exercise
the pure helpers and the constant.
"""

from __future__ import annotations

from app.services.ai.context import (
    _PRICING_CONDUCT_BLOCK,
    _market_pulse_block,
    _TONE_PREAMBLES,
)


def test_pricing_conduct_block_present_and_complete():
    """The mandatory client-only pricing-conduct block must exist and
    contain the user-approved phrasing + every hard rule."""
    body = _PRICING_CONDUCT_BLOCK
    assert "Pricing conduct" in body
    # The user-approved script anchor.
    assert "still putting a loan together for you" in body
    # The variable-driven hedge — must cite the things that will change.
    assert "credit score" in body
    assert "property final value" in body
    # Hard rules.
    assert "Do NOT quote rates from general knowledge" in body
    assert "Do NOT mention basis points" in body
    assert "Do NOT compare the borrower's rate to public benchmarks" in body
    assert "Do NOT promise approval" in body


def test_tone_preamble_for_client_does_not_authorize_pricing():
    """Sanity: client tone preamble doesn't say 'discuss rates freely'."""
    body = _TONE_PREAMBLES["client"]
    assert "Don't expose internal" in body
    # And the broker preamble explicitly OPENS the pricing detail door —
    # confirms the system permits it for internal audiences.
    broker = _TONE_PREAMBLES["broker"]
    assert "Internal pricing detail is fine" in broker


async def test_market_pulse_returns_empty_for_client_audience():
    """The market-pulse block returns nothing when the prompt is being
    assembled for a client — regardless of whether a pulse exists. We
    don't even fetch the data."""
    # Pass `db=None` and `loan=None` — for client, the function should
    # short-circuit before touching either.
    result = await _market_pulse_block(None, None, audience="client")  # type: ignore[arg-type]
    assert result == ""
