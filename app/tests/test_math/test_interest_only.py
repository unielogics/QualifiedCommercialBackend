from app.services.math.interest_only import interest_only_payment, interest_only_total


def test_io_payment_simple():
    # $500,000 at 9% IO = $3,750/mo
    assert interest_only_payment(500_000, 0.09) == 3750.0


def test_io_payment_zero_principal():
    assert interest_only_payment(0, 0.09) == 0.0


def test_io_payment_zero_rate():
    assert interest_only_payment(500_000, 0.0) == 0.0


def test_io_total_over_12_months():
    pmt = interest_only_payment(500_000, 0.09)
    total = interest_only_total(500_000, 0.09, 12)
    assert total == round(pmt * 12, 2)


def test_io_balance_unchanged_implicitly():
    # IO = no principal reduction. Total interest = monthly_payment * months exactly.
    monthly = interest_only_payment(1_000_000, 0.10)
    assert interest_only_total(1_000_000, 0.10, 18) == round(monthly * 18, 2)
