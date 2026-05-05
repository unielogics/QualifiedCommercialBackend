import pytest

from app.services.math.pricing import (
    broker_compensation,
    final_rate_after_buydown,
    pricing_quote,
)


def test_buydown_reduces_rate_by_25bps_per_point():
    # 7.00% base, 1 point buy down → 6.75%
    assert final_rate_after_buydown(0.07, 1.0) == 0.0675


def test_buydown_two_points():
    # 7.00% base, 2 points → 6.50%
    assert final_rate_after_buydown(0.07, 2.0) == 0.065


def test_buydown_floors_at_zero():
    assert final_rate_after_buydown(0.005, 5.0) == 0.0


def test_zero_buydown_preserves_base_rate():
    assert final_rate_after_buydown(0.075, 0) == 0.075


def test_broker_compensation_default_origination():
    # $500k loan, 1.5% origination + 1 point discount
    orig, disc, total_pct = broker_compensation(500_000, 1.0)
    assert orig == 7500.0     # 1.5% × 500,000
    assert disc == 5000.0     # 1% × 500,000
    assert total_pct == 2.5   # 1.5 + 1.0


def test_pricing_quote_full():
    q = pricing_quote(base_rate=0.07, loan_amount=500_000, discount_points=2.0)
    assert q.final_rate == 0.065
    assert q.broker_origination_dollars == 7500.0
    assert q.discount_dollars == 10000.0
    assert q.total_broker_points == 3.5
    assert q.cash_to_close_pricing == 17500.0


def test_pricing_quote_rejects_excessive_buydown():
    with pytest.raises(ValueError):
        pricing_quote(base_rate=0.07, loan_amount=500_000, discount_points=5.0)


def test_pricing_quote_rejects_negative_points():
    with pytest.raises(ValueError):
        pricing_quote(base_rate=0.07, loan_amount=500_000, discount_points=-1.0)


def test_pricing_quote_zero_points_baseline():
    q = pricing_quote(base_rate=0.07, loan_amount=500_000, discount_points=0)
    assert q.final_rate == 0.07
    assert q.discount_dollars == 0.0
