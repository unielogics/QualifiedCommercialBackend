"""Suggested terms for an unfunded listing: the largest amount the income
covers at 1.25x on the program's longest term, never more than the program
maximum or what the client asked for, with the coverage at the requested
amount shown beside it. Honest when there is no room."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.offer_suggestions import (
    capacity_monthly,
    income_figure,
    principal_for_payment,
    program_shape,
    suggest_product,
)


def test_capacity_and_principal_math() -> None:
    assert capacity_monthly(120000, 2000) == 6000.0  # 120k/12/1.25 = 8000, minus 2000
    assert capacity_monthly(12000, 2000) == 0.0
    assert capacity_monthly(None, 2000) is None
    assert principal_for_payment(1434.71, 12, 120) == 100000.04
    assert principal_for_payment(1000, 0, 12) == 12000.0


def test_a_program_with_published_terms_shapes_the_suggestion() -> None:
    terms = SimpleNamespace(pricing_basis="interest_rate", minimum_pricing=14, maximum_pricing=20, minimum_term_months=12, maximum_term_months=120, minimum_amount=50000, maximum_amount=350000)
    shape = program_shape(terms)
    assert (shape["term_months"], shape["rate_pct"], shape["max_amount"], shape["published"]) == (120, 17.0, 350000.0, True)
    product = suggest_product(program_key="mca_refinance", label="MCA Refinance", income_annual=120000, existing_monthly_debt_service=2000, requested_amount=70000, shape=shape, eligible=True)
    # room 6000/mo at 17% over 120 months supports ~345k; the client asked for 70k, so 70k it is
    assert product["amount"] == 70000.0 and product["affordable_amount"] > 300000
    assert product["monthly_payment"] == 1216.58 and product["dscr"] == 3.11 and product["dscr_status"] == "very_strong"
    assert product["requested_dscr"] == 3.11
    assert "70,000 at 17% over 120 months" in product["reading"]


def test_the_program_maximum_and_minimum_bound_the_amount() -> None:
    shape = program_shape(SimpleNamespace(pricing_basis="apr", minimum_pricing=10, maximum_pricing=None, minimum_term_months=None, maximum_term_months=60, minimum_amount=100000, maximum_amount=150000))
    capped = suggest_product(program_key="p", label="Term loan", income_annual=600000, existing_monthly_debt_service=0, requested_amount=None, shape=shape)
    assert capped["amount"] == 150000.0 and capped["term_months"] == 60 and capped["rate_pct"] == 10.0
    below = suggest_product(program_key="p", label="Term loan", income_annual=30000, existing_monthly_debt_service=0, requested_amount=None, shape=shape)
    assert below["amount"] is None and "below the program minimum" in below["reason"]


def test_no_room_and_no_income_are_said_plainly() -> None:
    shape = program_shape(None)
    assert (shape["term_months"], shape["rate_pct"], shape["published"]) == (120, 16.0, False)
    no_room = suggest_product(program_key="p", label="MCA Refinance", income_annual=-29364, existing_monthly_debt_service=0, requested_amount=70000, shape=shape)
    assert no_room["amount"] is None and "leaves no room" in no_room["reason"]
    assert no_room["requested_dscr"] is not None and no_room["requested_dscr_status"] == "concerning"
    no_income = suggest_product(program_key="p", label="MCA Refinance", income_annual=None, existing_monthly_debt_service=None, requested_amount=70000, shape=shape)
    assert no_income["amount"] is None and "No income figure" in no_income["reason"] and no_income["dscr"] is None


def test_factor_rate_programs_price_by_factor() -> None:
    shape = program_shape(SimpleNamespace(pricing_basis="factor_rate", minimum_pricing=1.2, maximum_pricing=1.4, minimum_term_months=None, maximum_term_months=12, minimum_amount=None, maximum_amount=None))
    assert shape["factor_rate"] == 1.3 and shape["rate_pct"] is None
    product = suggest_product(program_key="p", label="Advance", income_annual=120000, existing_monthly_debt_service=0, requested_amount=50000, shape=shape)
    assert product["monthly_payment"] == round(50000 * 1.3 / 12, 2) and product["amount"] == 50000.0


def test_income_figure_follows_the_cockpit_headline() -> None:
    cockpit = {"dscr": {"headline": {"numerator": 70000, "key": "tax_return", "numerator_label": "EBITDA"}, "pro_forma_headline": None, "candidates": []}}
    assert income_figure(cockpit) == (70000, "tax_return", "EBITDA")
    proxy = {"dscr": {"headline": None, "pro_forma_headline": None, "candidates": [{"numerator": -29364, "key": "bank_cash_flow", "numerator_label": "Cash retained"}]}}
    assert income_figure(proxy) == (-29364, "bank_cash_flow", "Cash retained")
    assert income_figure({"dscr": {"headline": None, "pro_forma_headline": None, "candidates": []}}) == (None, None, None)
