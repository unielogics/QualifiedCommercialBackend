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


def test_loan_header_strips_pricing_internals_for_client():
    """The loan header is the single biggest leak surface — it gets
    rendered for every chat turn. For client audience it must never
    leak base_rate, discount_points, lender_fees, risk_score, or the
    operator-only loan-level mechanics (vacancy_pct, expense_ratio,
    reserves_required, etc.)."""
    from types import SimpleNamespace
    from uuid import UUID
    from app.services.ai.context import _loan_header

    loan = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        deal_id="L-9999",
        client_id=UUID("00000000-0000-0000-0000-000000000002"),
        address="1 Test St",
        city="Austin",
        state="TX",
        property_type="single_family",
        sqft=1500,
        beds=3,
        baths=2,
        year_built=2010,
        unit_count=1,
        annual_taxes=4500.0,
        annual_insurance=1200.0,
        monthly_hoa=0,
        zoning="R-1",
        parcel_id="ABC-123",
        listing_status="off_market",
        type="dscr",
        purpose="purchase",
        side="buyer",
        stage="collecting_docs",
        amount=500_000.0,
        ltv=0.75,
        ltc=None,
        arv=None,
        base_rate=7.500,            # MUST NOT APPEAR for client
        discount_points=1.250,      # MUST NOT APPEAR for client
        final_rate=8.250,           # OK for client
        origination_pct=0.015,      # MUST NOT APPEAR for client
        term_months=360,
        monthly_rent=4000.0,
        dscr=1.30,
        risk_score=72,              # MUST NOT APPEAR for client
        amortization_style="fully_amortizing",
        prepay_penalty="5_4_3_2_1", # MUST NOT APPEAR for client
        vacancy_pct=0.05,           # MUST NOT APPEAR for client
        expense_ratio_pct=0.25,     # MUST NOT APPEAR for client
        reserves_required=12000,    # MUST NOT APPEAR for client
        lender_fees=2500,           # MUST NOT APPEAR for client
        entity_type="llc",          # MUST NOT APPEAR for client
        experience_tier="3_5_flips",
        construction_holdback_pct=None,
        draw_count=None,
        exit_strategy=None,
        cash_to_borrower=None,
        seasoning_months=None,
        property_count=None,
        close_date=None,
        deal_health="on_track",     # MUST NOT APPEAR for client
        status_summary=None,
    )

    client_view = _loan_header(loan, audience="client")
    # Things the client SHOULD see.
    assert "L-9999" in client_view
    assert "1 Test St" in client_view
    assert "Austin, TX" in client_view
    assert "500,000" in client_view
    assert "8.25" in client_view or "8.250" in client_view  # final rate
    assert "LTV" in client_view
    # Things the client must NEVER see.
    forbidden_for_client = [
        "Base rate", "7.5", "Discount points", "1.250", "Origination",
        "Lender fees", "risk", "deal health", "vacancy", "expense ratio",
        "reserves", "entity", "prepay",
    ]
    for f in forbidden_for_client:
        assert f.lower() not in client_view.lower(), f"Leaked to client view: {f!r}\n{client_view}"

    # Broker view should include those internal fields.
    broker_view = _loan_header(loan, audience="broker")
    assert "Base rate" in broker_view
    assert "Discount points" in broker_view
    assert "vacancy" in broker_view
    assert "expense ratio" in broker_view
    assert "deal health" in broker_view
