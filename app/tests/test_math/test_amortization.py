import pytest

from app.services.math.amortization import (
    amortization_schedule,
    monthly_payment,
    total_interest,
)


def test_monthly_payment_30yr_fixed():
    # $300,000 at 7% over 30 years should be ~$1,995.91
    p = monthly_payment(300_000, 0.07, 360)
    assert abs(p - 1995.91) < 0.01


def test_monthly_payment_zero_rate_returns_principal_div_term():
    assert monthly_payment(120_000, 0.0, 60) == 2000.0


def test_monthly_payment_zero_principal_returns_zero():
    assert monthly_payment(0, 0.07, 360) == 0.0


def test_monthly_payment_zero_term_returns_zero():
    assert monthly_payment(300_000, 0.07, 0) == 0.0


def test_amortization_schedule_length_matches_term():
    rows = amortization_schedule(100_000, 0.06, 360)
    assert len(rows) == 360


def test_amortization_first_row_interest_is_principal_times_monthly_rate():
    rows = amortization_schedule(100_000, 0.06, 360)
    expected_interest = round(100_000 * (0.06 / 12), 2)
    assert rows[0].interest == expected_interest


def test_amortization_final_balance_is_zero():
    rows = amortization_schedule(100_000, 0.06, 360)
    assert rows[-1].balance == 0.0


def test_amortization_principal_plus_interest_equals_payment():
    rows = amortization_schedule(250_000, 0.075, 360)
    for row in rows[:5]:
        assert abs(row.payment - (row.principal + row.interest)) < 0.01


def test_total_interest_is_positive_for_normal_loan():
    ti = total_interest(300_000, 0.07, 360)
    assert ti > 0


@pytest.mark.parametrize(
    "principal,rate,term,expected_pmt",
    [
        (100_000, 0.05, 360, 536.82),
        (500_000, 0.065, 360, 3160.34),
        (250_000, 0.08, 360, 1834.41),
    ],
)
def test_monthly_payment_known_values(principal, rate, term, expected_pmt):
    assert abs(monthly_payment(principal, rate, term) - expected_pmt) < 0.5
