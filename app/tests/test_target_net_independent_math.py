"""Independent cash identities and an exhaustive cent oracle for grossed-up loans."""
from decimal import ROUND_HALF_UP, Decimal
from random import Random
from uuid import uuid4

import pytest

from app.services.deal_budget import calculate, solve_principal

CENT = Decimal(".01")


def row(value, *, method="percent", settlement="financed", **changes):
    return {"id": str(uuid4()), "label": "Synthetic cost", "category": "closing_cost", "payer": "borrower",
        "method": method, "value": value, "basis": "loan_amount", "timing": "closing", "settlement": settlement,
        "enabled": True, "reserve_release": "unknown", "recipient_role": "third_party", **changes}


def budget(rows, **changes):
    return {"funding_mode": "single_investor", "investor_funding_amount": None,
        "minimum_investor_contribution": None, "currency": "USD", "costs_confirmed": True,
        "loan_amount_mode": "target_net", "target_net_amount": "100000.00", "base_loan_amount": None,
        "rows": rows, **changes}


def loan(amount="100000.00"):
    return {"amount": amount, "rate_pct": 12, "term_months": 12, "structure": "interest_only",
        "payments_per_year": 12, "active_proposal": True}


def d(value):
    return Decimal(str(value))


def cents_cost(gross, cost):
    return (d(cost["value"]) if cost["method"] == "fixed" else gross * d(cost["value"]) / 100).quantize(CENT, rounding=ROUND_HALF_UP)


def test_requested_example_finances_points_and_withholds_escrow_without_shorting_client():
    costs = [row(4, category="origination"), row(10, category="escrow_reserve", settlement="withheld", recipient_role="escrow")]
    result = solve_principal(budget(costs, target_net_amount="172488.00"), loan("172488.00"))
    assert result["status"] == "calculated"
    assert d(result["gross_loan_amount"]) == d("200567.44")
    assert d(result["financed_closing_costs"]) == d("8022.70")
    assert d(result["withheld_closing_costs"]) == d("20056.74")
    assert d(result["net_client_cash"]) == d("172488.00")
    assert result["requires_apply"] is True


def test_base_amount_only_finances_selected_rows_and_keeps_withheld_deduction_visible():
    costs = [row(4), row(10, settlement="withheld", category="escrow_reserve")]
    result = solve_principal(budget(costs, loan_amount_mode="base_loan", base_loan_amount="100000.00", target_net_amount=None), loan())
    assert d(result["gross_loan_amount"]) == d("104166.67")
    assert d(result["financed_closing_costs"]) == d("4166.67")
    assert d(result["withheld_closing_costs"]) == d("10416.67")
    assert d(result["net_client_cash"]) == d("89583.33")


def test_fixed_charges_and_points_gross_up_once_while_separate_costs_stay_separate():
    costs = [row(4), row(10, settlement="withheld", category="escrow_reserve"),
        row(1250, method="fixed"), row(5000, method="fixed", settlement="separate")]
    settings = budget(costs)
    solved = solve_principal(settings, loan())
    assert d(solved["gross_loan_amount"]) == d("117732.56")
    result = calculate(settings, loan(solved["gross_loan_amount"]))
    assert d(result["net_client_cash"]) == d("100000.00")
    assert d(result["borrower_cash_after_separate_costs"]) == d("95000.00")
    assert d(result["investor_funding_amount"]) == d("117732.56")
    assert d(result["escrow_reserves"]) == d("11773.26")
    again = solve_principal(settings, loan(solved["gross_loan_amount"]))
    assert d(again["gross_loan_amount"]) == d(solved["gross_loan_amount"])
    assert again["requires_apply"] is False


def test_fixed_investor_capital_basis_does_not_create_a_false_circular_percentage():
    result = solve_principal(budget([row(2, basis="investor_funding")], investor_funding_amount=50000), loan())
    assert d(result["gross_loan_amount"]) == d("101000.00")
    assert d(result["financed_closing_costs"]) == d("1000.00")
    assert d(result["net_client_cash"]) == d("100000.00")


@pytest.mark.parametrize("mode", ["base_loan", "target_net"])
@pytest.mark.parametrize("seed", range(12))
def test_rounded_multi_row_solution_matches_exhaustive_cent_oracle(mode, seed):
    rng = Random(seed)
    target = Decimal(rng.randrange(50, 400)) / 100
    costs = [row(rng.randrange(1, 150000) / 10000) for _ in range(3)]
    costs += [row(str(Decimal(rng.randrange(0, 30)) / 100), method="fixed", settlement="withheld"),
        row(99, enabled=False), row(40, settlement="separate")]
    settings = budget(costs, loan_amount_mode=mode, base_loan_amount=str(target) if mode == "base_loan" else None,
        target_net_amount=str(target) if mode == "target_net" else None)
    def delivered(gross):
        applicable = [cost for cost in costs if cost["enabled"] and (cost["settlement"] == "financed"
            or mode == "target_net" and cost["settlement"] == "withheld")]
        return gross - sum((cents_cost(gross, cost) for cost in applicable), Decimal(0))
    expected = next(Decimal(cents) / 100 for cents in range(int(target * 100), 2000)
        if delivered(Decimal(cents) / 100) >= target)
    result = solve_principal(settings, loan(str(target)))
    assert result["status"] == "calculated"
    assert d(result["gross_loan_amount"]) == expected


def test_unknown_and_impossible_grossups_are_not_published_as_real_zero_costs():
    unknown = solve_principal(budget([row(None)]), loan())
    assert unknown["status"] == "unknown" and unknown["gross_loan_amount"] is None
    impossible = solve_principal(budget([row(60), row(40, settlement="withheld")]), loan())
    assert impossible["status"] == "impossible" and impossible["gross_loan_amount"] is None
    interest = solve_principal(budget([row(4, basis="interest")]), loan())
    assert interest["status"] == "unknown" and interest["gross_loan_amount"] is None


def test_legacy_fixed_gross_budget_preserves_the_explicit_note_amount():
    result = calculate(budget([row(4, settlement="withheld")], loan_amount_mode="gross_loan", target_net_amount=None), loan())
    assert result["gross_loan_amount"] == 100000
    assert result["net_client_cash"] == 96000


def evaluate_costs(costs):
    from app.services.financial_plan import evaluate_plan, normalize_plan

    basis = {"key": "profit_and_loss", "label": "Synthetic annual operating earnings", "period": "2025",
        "numerator": 120000, "value": 10, "annual_debt_service": 12000, "currency_comparable": True}
    return evaluate_plan(normalize_plan({"new_loan_amount": 100000, "new_loan_rate_pct": 12,
        "new_loan_term_months": 12, "new_loan_structure": "interest_only"}), obligations_list=[],
        recurring={"payees": [], "months_read": ["2026-01"]},
        months=[{"month": "2026-01", "label": "Jan 2026", "deposits": 10000, "withdrawals": 8000,
            "net": 2000, "ending_balance": 5000, "low_balance": 4000}],
        dscr={"headline": basis, "candidates": [basis]}, service={"monthly": 1000, "annual": 12000},
        proposed=None, requested_amount=100000,
        ebitda={"headline": {"kind": "computed_ebitda", "value": 120000, "annualized_value": 120000,
            "months_covered": 12, "annualization_factor": 1, "currency": "USD", "period": "2025"}},
        financing_budget=budget(costs, loan_amount_mode="gross_loan", target_net_amount=None))


def test_new_operating_and_financing_charges_affect_different_parts_of_coverage_once():
    result = evaluate_costs([
        row(100, method="fixed", settlement="separate", timing="monthly"),
        row(200, method="fixed", settlement="separate", timing="monthly", earnings_treatment="operating_expense"),
        row(300, method="fixed", settlement="separate", timing="monthly", coverage_treatment="already_in_baseline"),
        row(500, method="fixed", settlement="separate", timing="monthly", payer="investor"),
    ])
    assert result["income_before_annual"] == 120000
    assert result["income_after_annual"] == 117600
    assert result["annual_new_financing_fees"] == 1200
    assert result["annual_new_operating_expenses"] == 2400
    assert result["debt_service_after_annual"] == 25200
    assert result["headline_after"] == round(117600 / 25200, 2)
    assert result["net_after_avg"] == 700
    assert result["ebitda"]["annual_before"] == 120000
    assert result["ebitda"]["annual_after"] == 117600


def test_closing_points_and_held_reserves_do_not_become_recurring_operating_expenses():
    result = evaluate_costs([row(4), row(10, settlement="withheld", category="escrow_reserve")])
    assert result["debt_service_after_annual"] == 24000
    assert result["headline_after"] == 5
    assert result["annual_new_operating_expenses"] == 0
    assert result["annual_new_financing_fees"] == 0
    assert result["ebitda"]["annual_before"] == result["ebitda"]["annual_after"] == 120000
