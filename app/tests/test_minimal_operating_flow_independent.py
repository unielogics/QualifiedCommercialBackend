"""Independent synthetic oracles for assignment capacity, not operating profit."""
from copy import deepcopy
from typing import get_args

import pytest

from app.schemas.earnings_improvement import ExpenseCategory, validate_body
from app.services import earnings_improvement as engine
from app.services import released_cash_flow
from app.tests.test_earnings_improvement import NOW, action, body, expense
from app.tests.test_refinance_reinvestment_independent import (
    accounting_change,
    saved_loan,
    source_context,
)

RENT = "71000000-0000-4000-8000-000000000001"
SALARY = "71000000-0000-4000-8000-000000000002"
SECOND = "71000000-0000-4000-8000-000000000003"
ALLOCATION = "71000000-0000-4000-8000-000000000004"
RENT_KEY = "source:synthetic:rent"


def scenario(*, loan=True, reduction=100, setup=0, setup_cadence="one_time"):
    current = saved_loan() if loan else source_context()
    guidance = current["operating_evidence"]["guidance"]
    reference = deepcopy(guidance["expense_groups"][0])
    for key, category, label, amount in [
        (RENT_KEY, "rent", "Premises", 12000),
        ("source:synthetic:salary", "salaries", "Employee costs", 78000),
    ]:
        guidance["expense_groups"].append({**deepcopy(reference), "key": key, "category": category,
            "label": label, "baseline_annual_amount": amount})
    da = accounting_change()["expenses"][0]
    rows = [da, expense(id=RENT, category="rent", label="Premises", current_amount=1000,
        cadence="monthly", source_group_key=RENT_KEY),
        expense(id=SALARY, category="salaries", label="Employee costs", current_amount=78000,
        cadence="annual", source_group_key="source:synthetic:salary")]
    planned = action(expense_row_id=RENT, label="Renegotiate premises",
        current_amount=1000, proposed_amount=None if reduction is None else 1000-reduction,
        implementation_cost=setup, implementation_cost_cadence=setup_cadence,
        source_note="", source_file_ids=[],
        command={"target":"total_cost", "mode":"amount", "direction":"increase" if reduction is not None and reduction < 0 else "decrease",
            "value":abs(reduction) if reduction is not None else None, "allocation":"none"})
    raw = body(planned, expenses=rows)
    raw["category_confirmations"] = {key: "confirmed" if key in {"rent", "salaries", "depreciation_amortization"}
        else "not_applicable" for key in get_args(ExpenseCategory)}
    raw["released_cash_flow"] = {"source_fingerprint": released_cash_flow.source(current)["fingerprint"], "allocations":[]}
    return validate_body(raw), current


def evaluate(raw, current):
    return engine.evaluate(raw, current, now=NOW, include_expense_changes=True)


def acknowledge_funding(raw, result):
    raw = deepcopy(raw)
    raw["released_cash_flow"]["source_fingerprint"] = result["cash_bridge"]["funding_source_fingerprint"]
    return raw


@pytest.mark.parametrize("loan,expected", [(False,100), (True,3600)])
def test_recurring_cash_cost_saving_joins_loan_pool_once(loan, expected):
    raw, current = scenario(loan=loan)
    raw["released_cash_flow"]={"source_fingerprint":None,"allocations":[]}
    result = evaluate(raw,current)
    cash = result["cash_bridge"]
    assert cash["operating_savings_monthly"] == 100
    assert cash["operating_savings_annual"] == 1200
    assert cash["funding_capacity_monthly"] == expected
    assert cash["funding_capacity_annual"] == expected * 12
    assert cash["funding_available"] is True
    assert cash["funding_complete"] is True
    assert cash["funding_retained_monthly"] == expected
    assert cash["funding_retained_annual"] == expected * 12
    assert cash["funding_reinvested_monthly"] == 0
    assert raw["released_cash_flow"] == {"source_fingerprint":None,"allocations":[]}
    assert result["draft_feedback"]["summary"]["working"]["noi_delta"] == 1200
    assert result["baseline"]["noi"] == 18000


@pytest.mark.parametrize("setup,cadence,monthly", [(12,"monthly",88), (144,"annual",88), (144,"one_time",100)])
def test_only_recurring_setup_reduces_recurring_savings_capacity(setup,cadence,monthly):
    raw,current=scenario(setup=setup,setup_cadence=cadence)
    result=evaluate(raw,current)
    assert result["cash_bridge"]["operating_savings_monthly"] == monthly
    assert result["cash_bridge"]["funding_capacity_monthly"] == 3500+monthly
    assert result["draft_feedback"]["summary"]["working"]["first_year_cash_delta"] == 1056


def test_unknown_setup_is_not_assumed_free_while_trusted_loan_remains_known():
    raw,current=scenario(setup=None)
    cash=evaluate(raw,current)["cash_bridge"]
    assert cash["net_loan_cash_change_monthly"] == 3500
    assert cash["funding_capacity_monthly"] is None
    assert cash["funding_known_capacity_monthly"] == 3500
    assert cash["funding_available"] is False
    assert cash["operating_savings_monthly"] is None


@pytest.mark.parametrize("change", [0, -100])
def test_zero_or_cost_increase_does_not_create_a_new_funding_source(change):
    raw,current=scenario(reduction=change)
    if change<0:
        raw["adjustments"][0]["kind"]="expense_increase"
        raw["adjustments"][0]["command"].update(direction="increase",value=100)
    cash=evaluate(raw,current)["cash_bridge"]
    assert cash["operating_savings_monthly"] == 0
    assert cash["funding_capacity_monthly"] == 3500


def test_d_and_a_profit_change_is_not_assignable_cash():
    raw,current=scenario()
    raw["adjustments"]=accounting_change(6000)["adjustments"]
    result=evaluate(raw,current)
    assert result["draft_feedback"]["summary"]["working"]["noi_delta"] == 6000
    assert result["draft_feedback"]["summary"]["working"]["cash_delta_annual"] == 0
    assert result["cash_bridge"]["operating_savings_monthly"] == 0
    assert result["cash_bridge"]["funding_capacity_monthly"] == 3500


def test_unknown_other_action_does_not_erase_valid_savings_or_known_loan():
    raw,current=scenario()
    raw["adjustments"].append(action(id=SECOND,expense_row_id=SALARY,driver_key="salary",
        label="Unpriced employee proposal",current_amount=78000,proposed_amount=None,cadence="annual",
        implementation_cost=None,command={"target":"total_cost","mode":"amount","direction":"decrease","value":None,"allocation":"none"}))
    result=evaluate(raw,current)
    assert result["draft_feedback"]["summary"]["working"]["noi"] is None
    assert result["cash_bridge"]["operating_savings_monthly"] is None
    assert result["cash_bridge"]["operating_known_savings_monthly"] == 100
    assert result["cash_bridge"]["funding_capacity_monthly"] is None
    assert result["cash_bridge"]["funding_known_capacity_monthly"] == 3600


@pytest.mark.parametrize("fault", ["source_amount","source_stamp","currency"])
def test_untrusted_cost_saving_does_not_become_spendable_funds(fault):
    raw,current=scenario()
    if fault=="source_amount":
        raw["expenses"][1]["current_amount"]=1100
    elif fault=="source_stamp":
        current["ebitda"]["source_selection"]["stale"]=True
        current["operating_evidence"]["files"][0]["eligible_for_review"]=False
    else:
        raw["currency"]="EUR"
    cash=evaluate(raw,current)["cash_bridge"]
    assert cash["net_loan_cash_change_monthly"] == 3500
    assert cash["funding_capacity_monthly"] is None
    assert cash["funding_known_capacity_monthly"] == 3500
    assert cash["funding_available"] is False
    assert cash["operating_savings_monthly"] is None


def test_saved_but_blocked_loan_is_not_replaced_with_zero_plus_savings():
    raw,current=scenario()
    current["plan"]["evaluation"]["annual_new_operating_expenses"]=None
    cash=evaluate(raw,current)["cash_bridge"]
    assert cash["operating_savings_monthly"] == 100
    assert cash["net_loan_cash_change_monthly"] is None
    assert cash["funding_capacity_monthly"] is None
    assert cash["funding_available"] is False


def test_assignment_changes_neither_finance_nor_expense_results():
    raw,current=scenario()
    before=evaluate(raw,current)
    assigned=acknowledge_funding(raw,before)
    assigned["released_cash_flow"]["allocations"]=[{"id":ALLOCATION,"label":"Retain operating and loan cash",
        "purpose":"reserve","amount_monthly":3600}]
    after=evaluate(assigned,current)
    assert after["cash_bridge"]["funding_allocated_monthly"] == 3600
    assert after["cash_bridge"]["funding_unallocated_monthly"] == 0
    assert after["cash_bridge"]["funding_complete"] is True
    assert after["baseline"] == before["baseline"]
    assert after["projection"] == before["projection"]
    assert after["draft_feedback"] == before["draft_feedback"]
    assert assigned["expenses"] == raw["expenses"]
    assert len(assigned["adjustments"]) == 1


def test_savings_change_requires_new_composite_ack_without_removing_assignments():
    raw,current=growth_scenario()
    first=evaluate(raw,current)
    raw=acknowledge_funding(raw,first)
    raw["released_cash_flow"]["allocations"]=[{"id":ALLOCATION,"label":"Fund campaign","purpose":"planned_increment","amount_monthly":1000,"expense_row_id":SECOND,"action_id":raw["adjustments"][1]["id"]}]
    assert evaluate(raw,current)["cash_bridge"]["funding_complete"] is True
    raw["adjustments"][0]["command"]["value"]=200
    raw["adjustments"][0]["proposed_amount"]=800
    after=evaluate(raw,current)["cash_bridge"]
    assert after["funding_capacity_monthly"] == 3700
    assert after["funding_stale"] is True
    assert after["funding_complete"] is False
    assert raw["released_cash_flow"]["allocations"][0]["amount_monthly"] == 1000
    assert after["funding_source_fingerprint"] != first["cash_bridge"]["funding_source_fingerprint"]


def test_no_savings_preserves_existing_loan_only_ack():
    raw,current=scenario(reduction=0)
    raw["released_cash_flow"]["allocations"]=[{"id":ALLOCATION,"label":"Retain loan cash","purpose":"reserve","amount_monthly":3500}]
    cash=evaluate(raw,current)["cash_bridge"]
    assert cash["funding_stale"] is False
    assert cash["funding_complete"] is True


def test_why_is_required_but_evidence_and_owner_are_optional():
    raw,current=scenario()
    result=evaluate(raw,current)
    planned=result["actions"][0]
    assert planned["proposed_math_valid"] is True
    assert not any("source note" in str(item).lower() or "responsible" in str(item).lower() for item in planned["issues"])
    raw["adjustments"][0]["explanation"]=""
    result=evaluate(raw,current)
    assert any("explanation" in str(item) for item in result["actions"][0]["issues"])
    assert result["cash_bridge"]["funding_capacity_monthly"] == 3600



def test_undated_change_retains_annual_run_rate_without_fabricating_first_year():
    raw,current=scenario()
    raw["projection_start"]=None
    raw["adjustments"][0]["effective_date"]=None
    result=evaluate(raw,current)
    assert result["draft_feedback"]["summary"]["working"]["noi_delta"] == 1200
    assert result["draft_feedback"]["summary"]["working"]["first_year_noi_delta"] is None
    assert result["cash_bridge"]["funding_capacity_monthly"] == 3600
    assert result["actions"][0]["proposed_math_valid"] is True
    assert not any("effective_date" in str(item) for item in result["actions"][0].get("issues",[]))
    assert raw["projection_start"] is None
    assert raw["adjustments"][0]["effective_date"] is None


def growth_scenario():
    raw,current=scenario()
    salary_group=next(row for row in current["operating_evidence"]["guidance"]["expense_groups"] if row["category"]=="salaries")
    salary_group["baseline_annual_amount"]=66000
    next(row for row in raw["expenses"] if row["id"]==SALARY)["current_amount"]=66000
    current["operating_evidence"]["guidance"]["expense_groups"].append({**deepcopy(salary_group),
        "key":"source:synthetic:marketing","category":"marketing","label":"Current campaign","baseline_annual_amount":12000})
    raw["expenses"].append(expense(id=SECOND,category="marketing",label="Current campaign",
        current_amount=1000,cadence="monthly",source_group_key="source:synthetic:marketing"))
    raw["adjustments"].append(action(id="71000000-0000-4000-8000-000000000005",
        kind="marketing_investment",label="New service campaign",driver_key="campaign",
        expense_row_id=SECOND,current_amount=1000,proposed_amount=2000,cadence="monthly",
        revenue_return_multiple=2.5,contribution_margin_pct=60,
        product_name="Example service",source_note="",source_file_ids=[],
        explanation="Test an explicit demand and contribution assumption"))
    raw["category_confirmations"]["marketing"]="confirmed"
    return raw,current


def test_speculative_growth_profit_is_not_a_source_for_its_own_funding():
    raw,current=growth_scenario()
    result=evaluate(raw,current)
    assert result["draft_feedback"]["summary"]["working"]["cash_delta_monthly"] == 600
    assert result["cash_bridge"]["funding_capacity_monthly"] == 3600
    assert result["cash_bridge"]["operating_savings_monthly"] == 100
    assert result["cash_bridge"]["combined_incremental_monthly"] == 4100


def test_funding_planned_increment_does_not_spend_or_reduce_earnings_twice():
    raw,current=growth_scenario()
    before=evaluate(raw,current)
    raw=acknowledge_funding(raw,before)
    raw["released_cash_flow"]["allocations"]=[
        {"id":ALLOCATION,"purpose":"planned_increment","label":"Fund campaign","amount_monthly":1000,
            "expense_row_id":SECOND,"action_id":raw["adjustments"][1]["id"]},
    ]
    after=evaluate(raw,current)
    assert after["cash_bridge"]["funding_complete"] is True
    assert after["cash_bridge"]["funding_capacity_monthly"] == 3600
    assert after["cash_bridge"]["funding_unallocated_monthly"] == 2600
    assert after["cash_bridge"]["funding_retained_monthly"] == 2600
    assert after["cash_bridge"]["funding_reinvested_monthly"] == 1000
    assert after["draft_feedback"] == before["draft_feedback"]
    assert after["projection"] == before["projection"]
    assert len(raw["expenses"]) == 4
    assert len(raw["adjustments"]) == 2
