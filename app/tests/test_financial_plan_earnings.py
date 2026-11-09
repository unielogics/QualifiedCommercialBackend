from copy import deepcopy

import pytest

from app.services.financial_cockpit import plan_brief, plan_summary
from app.services.financial_plan_earnings import scenario_ebitda
from app.tests.test_financial_metric_review import build, correction, file, pnl, tax


def planned(sources=None, **plan):
    return build(sources, plan=plan)


def projection(cockpit):
    return cockpit["plan"]["evaluation"]["ebitda"]


def addback(kind="owner_salary", **extra):
    return {"id": "owner-adjustment", "kind": kind, "label": "Proposed officer pay adjustment",
            "amount_monthly": 1000, "note": "Staff hypothesis: discretionary expense after the proposed transaction",
            "source": "desk", **extra}


def test_refinance_reduces_debt_service_but_never_increases_ebitda():
    c = planned(retire=["row:debt"], new_payment_monthly=500)
    result = projection(c)
    assert (result["before"], result["after"], result["change"]) == (26000, 26000, 0)
    assert (result["annual_before"], result["annual_after"], result["annual_change"]) == (52000, 52000, 0)
    assert result["status"] == "projected_unchanged"
    assert result["debt_service_savings_monthly"] == 500
    assert result["adjusted_annual"]["before"] == result["adjusted_annual"]["after"] == 52000
    assert c["plan"]["evaluation"]["headline_after"] > c["dscr"]["headline"]["value"]


def test_only_documented_operating_hypotheses_change_adjusted_annual_ebitda():
    sources = [pnl()]
    original = deepcopy(sources)
    c = planned(sources, retire=["row:debt"], new_payment_monthly=500,
                add_backs=[addback(), addback("one_time_expense", id="one-time", amount_monthly=500),
                           addback("rent_to_owner", id="rent", amount_monthly=250)])
    result = projection(c)
    assert sources == original and result["before"] == result["after"] == 26000
    assert result["adjusted_annual"]["before"] == 52000
    assert result["adjusted_annual"]["after"] == 73000
    assert result["adjusted_annual"]["change"] == 21000
    assert result["adjusted_annual"]["status"] == "hypothetical_adjusted"
    assert not result["adjusted_annual"]["verified"]
    assert all(item["status"] == "hypothetical_unverified" for item in result["adjusted_annual"]["adjustments"])
    assert c["ebitda"]["headline"]["value"] == 26000


@pytest.mark.parametrize("kind", ["interest_refinanced", "depreciation", "other"])
def test_financing_and_generic_dscr_addbacks_do_not_enter_ebitda(kind):
    c = planned(add_backs=[addback(kind)], overrides={"income_annual": 999999})
    result = projection(c)
    assert result["annual_after"] == 52000 and result["adjusted_annual"]["after"] == 52000
    assert not result["adjusted_annual"]["adjustments"]
    assert result["adjusted_annual"]["excluded_adjustments"][0]["kind"] == kind
    assert c["plan"]["evaluation"]["income_after_annual"] == 999999


def test_missing_operating_reason_and_duplicate_identity_are_not_counted():
    c = planned(add_backs=[addback(note=None), addback(id="included"), addback(id="included")])
    result = projection(c)["adjusted_annual"]
    assert result["change"] == 12000
    assert len(result["adjustments"]) == 1 and len(result["excluded_adjustments"]) == 2
    assert "not counted twice" in result["excluded_adjustments"][1]["reason"]


@pytest.mark.parametrize("overrides", [dict(currency="EUR"), dict(currency=None), dict(basis=None), dict(business_name="Other LLC"), dict(interest=None), dict(ebitda=50000)])
def test_unsupported_annual_basis_does_not_create_adjusted_ebitda(overrides):
    c = planned([pnl(**overrides)], add_backs=[addback()])
    result = projection(c)
    assert result["adjusted_annual"]["status"] == "unavailable"
    assert result["adjusted_annual"]["before"] is None and result["adjusted_annual"]["after"] is None
    assert not result["adjusted_annual"]["adjustments"]


def test_short_period_keeps_period_earnings_known_without_annual_guess():
    c = planned([pnl(period_end="2026-04-30")], add_backs=[addback()])
    result = projection(c)
    assert result["before"] == result["after"] == 26000
    assert result["months_covered"] == 4 and result["annual_before"] is None and result["annual_after"] is None
    assert result["adjusted_annual"]["after"] is None
    assert any("without annualization" in note for note in result["notes"])


def test_source_reconciliation_warning_remains_visible_and_provisional():
    result = projection(planned([pnl(ebitda=50000)]))
    assert result["before"] == result["after"] == 50000 and result["provisional"]
    assert any("differs" in note for note in result["notes"])


def test_savings_before_missing_replacement_and_manual_debt_basis_are_identified():
    missing = projection(planned(retire=["row:debt"]))
    assert missing["debt_service_savings_monthly"] == 1000
    assert missing["replacement_payment_missing"] and missing["debt_service_savings_status"] == "before_replacement_payment"
    assert any("replacement loan terms" in note for note in missing["notes"])
    override = projection(planned(new_payment_monthly=500, overrides={"debt_service_monthly": 3000}))
    assert override["debt_service_savings_monthly"] == -2500
    assert override["debt_service_override_applied"] and override["debt_service_savings_status"] == "hypothetical_override"
    assert any("hypothetical DSCR debt-service override" in note for note in override["notes"])


def test_adjustment_eligibility_cannot_borrow_another_candidates_annual_period():
    c = planned()
    sources = deepcopy(c["ebitda"])
    sources["headline"].update(period="A newer four-month period", months_covered=4, annualized_value=None, annualization_factor=None)
    result = scenario_ebitda(sources, {"add_backs": [addback()]}, c["plan"]["evaluation"], c["dscr"], c["debt"]["service"])
    assert result["before"] == result["after"] == 26000
    assert result["annual_before"] is None and result["period"] == "A newer four-month period"
    assert result["adjusted_annual"]["before"] is None and result["adjusted_annual"]["after"] is None
    assert result["adjusted_annual"]["excluded_adjustments"][0]["reason"]


def test_correction_effective_current_source_is_preserved_as_factual_baseline():
    initial = build()
    corrected = build(state=correction(initial), plan={"retire": ["row:debt"], "new_payment_monthly": 500, "add_backs": [addback()]})
    result = projection(corrected)
    assert result["before"] == result["after"] == 36000
    assert result["annual_before"] == 72000 and result["adjusted_annual"]["after"] == 84000
    assert result["source_refs"][0]["analysis_id"] == "analysis-1"


def test_tax_only_and_bank_only_do_not_become_scenario_ebitda():
    tax_result = projection(planned([tax()], add_backs=[addback()]))
    assert tax_result["status"] == "tax_estimate_only" and tax_result["before"] is None and tax_result["after"] is None
    assert tax_result["tax_estimate"]["kind"] == "tax_estimate"
    assert tax_result["adjusted_annual"]["after"] is None
    banks = [file("bank_statement", {"bank": "Bank", "account_last4": "1234", "currency": "USD", "statement_period": f"2026-0{month}",
                                     "total_deposits_and_credits": 10000, "total_withdrawals_and_debits": 8000}, key=f"bank-{month}") for month in (1, 2, 3)]
    bank_result = projection(planned(banks, retire=["row:debt"], new_payment_monthly=500, add_backs=[addback()]))
    assert bank_result["status"] == "unavailable" and bank_result["before"] is None
    assert bank_result["debt_service_savings_monthly"] == bank_result["cash_flow_change_monthly"] == 500


def test_latest_pl_supplies_both_coverage_and_earnings_with_prior_tax_comparative():
    c = planned([tax(), pnl()], retire=["row:debt"], new_payment_monthly=500)
    assert c["dscr"]["headline"]["key"] == "profit_and_loss"
    assert projection(c)["source_kind"] == "computed_ebitda"
    assert projection(c)["before"] == projection(c)["after"] == 26000
    assert c["plan"]["evaluation"]["income_before_annual"] == 52000
    assert c["dscr"]["comparative_candidates"][0]["numerator"] == 115000


def test_zero_and_loss_baselines_remain_known_and_signed():
    c = planned([pnl(net_income=-6000, interest=1000, income_taxes=2000, depreciation_and_amortization=3000)])
    assert projection(c)["before"] == projection(c)["after"] == 0
    assert projection(c)["annual_before"] == projection(c)["adjusted_annual"]["before"] == 0
    loss = projection(planned([pnl(net_income=-10000)], add_backs=[addback()]))
    assert loss["before"] == loss["after"] == -4000
    assert loss["adjusted_annual"]["before"] == -8000 and loss["adjusted_annual"]["after"] == 4000


def test_impact_summary_and_model_brief_use_the_same_hypothetical_result():
    c = planned(retire=["row:debt"], new_payment_monthly=500, add_backs=[addback()])
    expected = projection(c)
    assert c["plan"]["impact"]["ebitda"] == expected
    assert plan_summary(c)["impact"]["ebitda"] == expected
    assert plan_brief(c)["ebitda"] == expected
