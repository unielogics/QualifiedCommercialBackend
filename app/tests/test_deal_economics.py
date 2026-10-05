from decimal import Decimal

from app.services.deal_economics import calculate_deal_earnings


def test_earnings_use_accepted_amount_not_approved_or_requested_amount() -> None:
    result = calculate_deal_earnings(
        accepted_amount=Decimal("325000"),
        origination_points=Decimal("2.5"),
        consulting_fee=Decimal("1500"),
    )

    assert result.accepted_amount == Decimal("325000.00")
    assert result.origination_earnings == Decimal("8125.00")
    assert result.consulting_fee == Decimal("1500.00")
    assert result.total == Decimal("9625.00")


def test_fixed_consulting_fee_is_forecastable_before_amount_is_accepted() -> None:
    result = calculate_deal_earnings(
        accepted_amount=None,
        origination_points=2,
        consulting_fee=750,
    )

    assert result.origination_earnings is None
    assert result.total == Decimal("750.00")


def test_no_calculable_component_remains_unknown_but_explicit_zero_is_zero() -> None:
    unknown = calculate_deal_earnings(
        accepted_amount=100_000,
        origination_points=None,
        consulting_fee=None,
    )
    zero = calculate_deal_earnings(
        accepted_amount=100_000,
        origination_points=0,
        consulting_fee=None,
    )

    assert unknown.total is None
    assert zero.total == Decimal("0.00")
