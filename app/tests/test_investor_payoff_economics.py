"""Independent payoff, fee applicability and setup examples; synthetic only."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.enums import Role
from app.schemas.deal_budget import DealBudgetPatch, DealBudgetRead, DealCostRow, PayoffTerms
from app.services.deal_budget import calculate, solve_principal
from app.services.investor_payoff import cash_interest
from app.tests.test_deal_budget_independent_math import budget, cost, terms

CONFIRMED_PAYOFF = {"interest_policy": "accrued_only", "charge_amount": 0}


def comparison(rows=(), loan=None, **settings):
    return calculate(budget(list(rows), payoff_terms=CONFIRMED_PAYOFF, **settings), loan or terms())["investor_scenarios"]


def test_month_six_collects_remaining_principal_and_only_six_months_interest_after_line_fees():
    scenarios = comparison([cost(label="Reporting", category="reporting", method="percent", basis="interest", value=10, timing="monthly"),
                            cost(label="Investor closing", value=250)])
    early, full = scenarios["six_month_payoff"], scenarios["full_term"]
    assert early["principal_returned"] == full["principal_returned"] == 100000
    assert early["gross_interest"] == early["earned_interest"] == 6000
    assert early["total_fees"] == 850 and early["net_income"] == 5150
    assert early["initial_outlay"] == 100250
    assert early["net_roi_pct"] == round(5150 / 100250 * 100, 2)
    assert full["gross_interest"] == 12000 and full["net_income"] == 10550
    assert sum(flow["net_cash_flow"] for flow in early["cash_flows"]) == pytest.approx(5150)


@pytest.mark.parametrize("ppy", [1, 4, 12])
def test_actual_payment_cadence_and_stub_interest_keep_six_month_fee_and_cash_amounts(ppy):
    early = comparison([cost(category="monitoring", method="percent", value=1, basis="outstanding_principal", timing="annual")],
                       terms(payments_per_year=ppy))["six_month_payoff"]
    assert early["principal_returned"] == 100000
    assert early["gross_interest"] == 6000
    # Each actual collection interval is rounded to cents, as in the budget.
    expected_fee = 499.98 if ppy == 12 else 500
    assert early["total_fees"] == expected_fee
    assert early["net_income"] == round(6000 - expected_fee, 2)
    assert early["payoff_month"] == 6


def test_amortizing_payoff_uses_declining_principal_and_returns_balance_once():
    early = comparison(loan=terms(term_months=36, structure="amortizing"))["six_month_payoff"]
    principal, rate, months = Decimal(100000), Decimal(".01"), 36
    payment = (principal * rate / (1 - (1 + rate) ** -months)).quantize(Decimal(".01"))
    interest = Decimal(0)
    for _ in range(6):
        due = (principal * rate).quantize(Decimal(".01"))
        interest += due
        principal -= payment - due
    assert early["gross_interest"] == float(interest)
    assert early["principal_returned"] == 100000
    assert sum(row["gross_receipts"] for row in early["cash_flows"]) == pytest.approx(100000 + float(interest))
    assert early["net_income"] == float(interest)


def test_short_note_caps_comparison_at_maturity_without_requiring_prepayment_assumptions():
    result = calculate(budget([]), terms(term_months=3))
    early = result["investor_scenarios"]["six_month_payoff"]
    assert early["capped_at_maturity"] is True
    assert early["horizon_months"] == early["payoff_month"] == 3
    assert early["gross_interest"] == early["net_income"] == 3000
    assert early["missing_assumptions"] == []


def test_unknown_payoff_terms_preserve_known_subtotals_but_never_infer_zero_charges():
    result = calculate(budget([cost(value=100, timing="monthly")]), terms())
    early, full = result["investor_scenarios"].values()
    assert early["gross_interest"] == 6000 and early["known_fee_subtotal"] == 600
    assert early["scheduled_net_income"] == 5400
    assert early["net_income"] is early["net_roi_pct"] is None
    assert early["missing_assumptions"] == ["interest_policy", "charge_amount"]
    assert full["net_income"] == result["investor_net_income"] == 10800


def test_documented_minimum_interest_is_a_separate_collected_payoff_charge():
    result = calculate(budget([], payoff_terms={"interest_policy": "minimum_months", "minimum_interest_months": 9, "charge_amount": 0}), terms())
    early = result["investor_scenarios"]["six_month_payoff"]
    assert early["earned_interest"] == 6000
    assert early["minimum_interest_charge"] == 3000
    assert early["gross_interest"] == early["net_income"] == 9000
    assert sum(flow["net_cash_flow"] for flow in early["cash_flows"]) == pytest.approx(9000)


@pytest.mark.parametrize(("recipient", "expected"), [("investor", 6500), ("company", 6000), ("third_party", 6000)])
def test_payoff_charge_is_investor_income_only_when_explicitly_payable_to_investor(recipient, expected):
    early = calculate(budget([], payoff_terms={"interest_policy": "accrued_only", "charge_amount": 500, "charge_recipient": recipient}), terms())["investor_scenarios"]["six_month_payoff"]
    assert early["net_income"] == expected
    assert early["gross_interest"] == 6000
    assert early["prepayment_charge"] == 500


def test_partial_investor_payoff_income_uses_unrounded_participation_ratio():
    early = calculate(budget([], investor_funding_amount=33333.33,
        payoff_terms={"interest_policy": "accrued_only", "charge_amount": 9000, "charge_recipient": "investor"}), terms())["investor_scenarios"]["six_month_payoff"]
    assert early["payoff_charge_income"] == 3000
    assert early["principal_returned"] == 33333.33


@pytest.mark.parametrize(("policy", "released", "held", "ready"), [("at_payoff", 1000, 0, True), ("contract_maturity", 0, 1000, True), ("unknown", 0, 1000, False)])
def test_reserve_assets_are_not_fees_or_profit_and_release_is_never_assumed(policy, released, held, ready):
    early = calculate(budget([cost(category="escrow_reserve", value=1000, reserve_release="maturity")],
        payoff_terms={**CONFIRMED_PAYOFF, "reserve_release": policy}), terms())["investor_scenarios"]["six_month_payoff"]
    assert early["reserve_released"] == released and early["reserve_held"] == held
    assert early["total_fees"] == 0
    assert (early["net_income"] == 6000) if ready else (early["net_income"] is None)
    assert early["initial_outlay"] == 101000
    if ready:
        assert early["net_cash_profit"] == 6000 - held


@pytest.mark.parametrize(("mode", "applies", "fee", "net"), [("company_managed", True, 600, 5400), ("transferred", False, 0, 6000), ("not_set", None, None, None)])
def test_company_fee_scope_is_explicit_and_unknown_is_not_zero(mode, applies, fee, net):
    result = calculate(budget([cost(category="maintenance", value=100, timing="monthly", applicability="company_managed")],
                              servicing_mode=mode, payoff_terms=CONFIRMED_PAYOFF), terms())
    early = result["investor_scenarios"]["six_month_payoff"]
    assert early["rows"][0]["applies"] is applies
    assert early["total_fees"] == fee and early["net_income"] == net


def test_applicability_also_governs_financed_principal_and_unknown_scope_cannot_solve():
    row = cost(category="software", payer="borrower", value=1000, timing="closing", settlement="financed", applicability="company_managed")
    base = budget([row], loan_amount_mode="base_loan", base_loan_amount=100000)
    assert solve_principal({**base, "servicing_mode": "company_managed"}, terms())["gross_loan_amount"] == 101000
    assert solve_principal({**base, "servicing_mode": "transferred"}, terms())["gross_loan_amount"] == 100000
    assert solve_principal(base, terms())["status"] == "unknown"


def test_software_setup_is_separate_required_tracking_not_a_verified_integration_or_fee():
    result = calculate(budget([]), terms())
    assert result["setup_readiness"]["status"] == "needs_setup"
    assert result["investor_net_income"] == 12000
    configured = calculate(budget([], software_monitoring={"software_status": "configured", "monitoring_status": "configured"}), terms())
    assert configured["setup_readiness"]["status"] == "staff_recorded_configured"
    assert configured["setup_readiness"]["verification"] == "staff_recorded_only"
    with pytest.raises(ValidationError):
        DealBudgetPatch(expected_revision=0, software_monitoring={"software_status": "not_required"})


def test_legacy_schema_defaults_preserve_all_fees_and_expose_unconfigured_status_without_write():
    row = DealCostRow.model_validate(cost()).model_dump()
    assert row["applicability"] == "all"
    read = DealBudgetRead(profile_id=uuid4())
    assert read.servicing_mode == "not_set" and read.software_monitoring.software_status == "not_set"
    assert read.payoff_terms.charge_amount is None
    with pytest.raises(ValidationError):
        PayoffTerms(charge_amount=False)


def test_legacy_full_write_preserves_omitted_new_scope_and_setup_but_explicit_edits_win():
    from app.services.deal_budget import _budget_payload

    saved_row = cost(applicability="company_managed")
    saved = budget([saved_row], servicing_mode="transferred",
        software_monitoring={"software_status": "configured", "monitoring_status": "in_progress", "provider": "Synthetic vendor"},
        payoff_terms={"interest_policy": "accrued_only", "charge_amount": "0.00"})
    old_row = {key: value for key, value in saved_row.items() if key != "applicability"}
    old = DealBudgetPatch(expected_revision=2, rows=[DealCostRow.model_validate(old_row)])
    preserved = _budget_payload(old, saved)
    for key in ("servicing_mode", "software_monitoring", "payoff_terms"):
        if isinstance(saved[key], dict):
            assert {field: preserved[key][field] for field in saved[key]} == saved[key]
        else:
            assert preserved[key] == saved[key]
    assert preserved["rows"][0]["applicability"] == "company_managed"
    explicit = DealBudgetPatch(expected_revision=2, servicing_mode="company_managed",
        software_monitoring={"software_status": "planned", "provider": None}, payoff_terms={"charge_amount": None},
        rows=[DealCostRow.model_validate({**saved_row, "applicability": "all"})])
    changed = _budget_payload(explicit, saved)
    assert changed["servicing_mode"] == "company_managed"
    assert changed["software_monitoring"]["software_status"] == "planned"
    assert changed["software_monitoring"]["monitoring_status"] == "in_progress"
    assert changed["software_monitoring"]["provider"] is None
    assert changed["payoff_terms"]["charge_amount"] is None
    assert changed["payoff_terms"]["interest_policy"] == "accrued_only"
    assert changed["rows"][0]["applicability"] == "all"


def test_unpaid_interest_accrual_is_not_counted_as_collected_income():
    assert cash_interest({"cash_payment": 0, "interest": 1000, "total_principal": 0}) == 0
    assert cash_interest({"cash_payment": 2100, "interest": 1000, "total_principal": 2000}) == 100


def test_closing_percentage_on_contract_interest_is_not_refunded_after_early_payoff():
    scenarios = comparison([cost(method="percent", value=5, basis="interest", timing="closing")])
    assert scenarios["six_month_payoff"]["total_fees"] == scenarios["full_term"]["total_fees"] == 600
    assert scenarios["six_month_payoff"]["net_income"] == 5400


def test_requested_short_horizon_does_not_accidentally_collect_next_cadence_payment():
    # Five requested months map to a six-month quarterly payment maturity;
    # payoff at month five needs one stub, not the later six-month interest.
    early = comparison(loan=terms(term_months=5, payments_per_year=4))["six_month_payoff"]
    assert early["horizon_months"] == early["payoff_month"] == 5
    assert early["gross_interest"] == 5000
    assert max(flow["month"] for flow in early["cash_flows"]) == 5


def test_default_structure_uses_same_flat_interest_stub_as_explicit_simple_interest():
    automatic = comparison(loan=terms(structure=None, payments_per_year=51.96))["six_month_payoff"]
    explicit = comparison(loan=terms(structure="simple_interest", payments_per_year=51.96))["six_month_payoff"]
    assert automatic["gross_interest"] == explicit["gross_interest"]
    assert automatic["net_income"] == explicit["net_income"]
    assert automatic["principal_returned"] == explicit["principal_returned"] == 100000


@pytest.mark.asyncio
async def test_guarded_budget_write_round_trips_explicit_setup_scope_and_payoff_assumptions():
    from app.services import application_profiles, deal_budget, financial_cockpit

    profile = SimpleNamespace(id=uuid4(), deal_budget={"revision": 2, "rows": []}, financial_plan={"new_loan_amount": 100000})
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: profile)), flush=AsyncMock())
    actor = SimpleNamespace(id=uuid4(), role=Role.SUPER_ADMIN)
    payload = DealBudgetPatch(expected_revision=2, costs_confirmed=True, servicing_mode="transferred",
        software_monitoring={"software_status": "planned", "monitoring_status": "in_progress", "provider": "Synthetic vendor", "notes": "Staff setup record"},
        payoff_terms=CONFIRMED_PAYOFF,
        rows=[DealCostRow.model_validate(cost(category="reporting", value=100, timing="monthly", applicability="company_managed"))])
    with patch.object(financial_cockpit, "load_cockpit", AsyncMock(return_value={"proposed_terms": terms(requested_amount=100000)})), \
            patch.object(application_profiles, "log_profile_action", AsyncMock()) as audit:
        result = await deal_budget.update(db, profile, payload, actor)
    assert result.revision == 3 and result.servicing_mode == "transferred"
    assert result.software_monitoring.provider == "Synthetic vendor"
    assert result.payoff_terms.charge_amount == 0
    assert result.rows[0].applicability == "company_managed"
    assert result.calculation["investor_scenarios"]["six_month_payoff"]["net_income"] == 6000
    assert result.calculation["rows"][0]["status"] == "excluded"
    audit.assert_awaited_once()
