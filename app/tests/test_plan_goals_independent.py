"""Independent synthetic checks of annual NOI goals and saved-loan cash uses."""

from copy import deepcopy
from uuid import uuid4

import pytest

from app.schemas.earnings_improvement import validate_body
from app.services import earnings_improvement as engine
from app.services import operating_plan_tracking as tracking
from app.services import released_cash_flow
from app.tests.test_earnings_improvement import NOW, action, body, expense
from app.tests.test_released_cash_flow import cockpit

GOAL = {"status": "set", "metric": "annual_noi", "amount": 250000, "currency": "USD", "revision": 1}


def objective_evaluation(current=-15000, proposed=-3000, reviewed=None, currency="USD"):
    return {
        "baseline": {"noi": current, "noi_currency": currency, "currency": "USD", "noi_year": 2025},
        "projection": {"proposed": {"noi": proposed}, "approved": {"noi": reviewed}},
    }


def saved_loan():
    current = cockpit()
    current["debt"]["service"].update(monthly=10000, annual=120000)
    current["plan"]["evaluation"].update(
        debt_service_after_regular_monthly=6000,
        annual_new_operating_expenses=6000,
        annual_new_financing_fees=1200,
    )
    return current


def assignment(amount, purpose="reserve", **links):
    return {
        "id": str(uuid4()),
        "label": "Synthetic monthly use",
        "amount_monthly": amount,
        "purpose": purpose,
        **links,
    }


def assigned(raw, current, rows):
    result = deepcopy(raw)
    result["released_cash_flow"] = {
        "source_fingerprint": released_cash_flow.source(current)["fingerprint"],
        "allocations": rows,
    }
    return validate_body(result)


def test_signed_annual_goal_does_not_turn_a_source_loss_into_zero_or_add_debt_savings():
    evaluation = objective_evaluation(reviewed=260000)
    original = deepcopy(evaluation)
    result = tracking.goal_progress(GOAL, evaluation)
    assert result["status"] == "calculated" and result["metric"] == "annual_noi"
    assert (
        result["current_noi"],
        result["zero"],
        result["proposed_noi"],
        result["reviewed_noi"],
        result["target"],
    ) == (-15000, 0, -3000, 260000, 250000)
    assert (result["current_gap"], result["proposed_gap"], result["reviewed_gap"]) == (
        265000,
        253000,
        0,
    )
    assert result["current_met"] is False and result["reviewed_met"] is True
    assert evaluation == original


@pytest.mark.parametrize(
    "current,expected,status",
    [(0, 250000, "calculated"), (250000, 0, "calculated"), (None, None, "source_unavailable")],
)
def test_zero_exact_target_and_unknown_are_distinct(current, expected, status):
    result = tracking.goal_progress(GOAL, objective_evaluation(current=current))
    assert (
        result["current_noi"] == current
        and result["current_gap"] == expected
        and result["status"] == status
    )


@pytest.mark.parametrize(
    "currency,status",
    [("CAD", "currency_mismatch"), (None, "source_unavailable"), ("", "source_unavailable")],
)
def test_unconfirmed_or_different_noi_currency_cannot_borrow_the_general_usd_label(
    currency, status
):
    result = tracking.goal_progress(GOAL, objective_evaluation(currency=currency))
    assert result["status"] == status
    assert all(
        result[key] is None
        for key in (
            "current_noi",
            "proposed_noi",
            "reviewed_noi",
            "current_gap",
            "proposed_gap",
            "reviewed_gap",
        )
    )
    assert result["target"] == 250000


def test_absent_staff_goal_is_unconfigured_not_a_250k_default():
    result = tracking.goal_progress(None, objective_evaluation())
    assert result["status"] == "awaiting_staff" and result["target"] is None
    assert result["current_gap"] is None and result["current_met"] is None


def test_real_operating_projection_and_saved_loan_cash_are_separate_and_do_not_mutate_the_plan():
    current = saved_loan()
    raw = body(
        action(current_amount=2000, proposed_amount=1000), expenses=[expense(current_amount=2000)]
    )
    raw = assigned(raw, current, [assignment(3500)])
    original_body, original_context = deepcopy(raw), deepcopy(current)
    evaluation = engine.evaluate(raw, current, now=NOW)
    result = tracking.cash_bridge(raw, current, evaluation)
    assert evaluation["baseline"]["noi"] == -15000
    assert evaluation["totals"]["proposed"]["annual_impact"] == 12000
    assert evaluation["projection"]["proposed"]["noi"] == -3000
    assert (
        result["before_debt_service_monthly"] == 10000
        and result["after_debt_service_monthly"] == 6000
    )
    assert (
        result["new_operating_charges_monthly"] == 500
        and result["new_financing_fees_monthly"] == 100
    )
    assert (
        result["gross_loan_relief_monthly"] == 4000
        and result["net_loan_cash_change_monthly"] == 3500
    )
    assert (
        result["recurring_cash_released_monthly"] == 3500
        and result["modeled_operations_monthly"] == 1000
    )
    assert result["combined_incremental_monthly"] == 4500 and result["unallocated_monthly"] == 0
    assert result["complete"] and result["status"] == "complete"
    assert raw == original_body and current == original_context
    unassigned = deepcopy(raw)
    unassigned["released_cash_flow"] = {"source_fingerprint": None, "allocations": []}
    without = engine.evaluate(unassigned, current, now=NOW)
    assert all(evaluation[key] == without[key] for key in ("baseline", "projection", "totals"))


@pytest.mark.parametrize(
    "missing",
    ["before", "after", "operating_charges", "financing_charges", "unsaved", "unapplied_budget"],
)
def test_unknown_or_unsaved_loan_capacity_never_becomes_zero_available_cash(missing):
    current = saved_loan()
    if missing == "before":
        current["debt"]["service"]["monthly"] = None
    elif missing == "after":
        current["plan"]["evaluation"]["debt_service_after_regular_monthly"] = None
    elif missing == "operating_charges":
        current["plan"]["evaluation"]["annual_new_operating_expenses"] = None
    elif missing == "financing_charges":
        current["plan"]["evaluation"]["annual_new_financing_fees"] = None
    elif missing == "unsaved":
        current["plan"]["record"].pop("updated_at")
    else:
        current["plan"]["evaluation"]["principal_solution"] = {
            "status": "calculated",
            "requires_apply": True,
        }
    raw = assigned(body(adjustments=[]), current, [assignment(3500)])
    result = tracking.cash_bridge(raw, current)
    assert not result["available"] and not result["complete"]
    assert all(
        result[key] is None
        for key in (
            "recurring_cash_released_monthly",
            "net_loan_cash_change_monthly",
            "combined_incremental_monthly",
            "unallocated_monthly",
        )
    )
    assert result["missing"]


def test_changed_saved_source_requires_new_ack_and_does_not_erase_allocations():
    current = saved_loan()
    raw = assigned(body(adjustments=[]), current, [assignment(3500)])
    current["plan"]["record"]["updated_at"] = "2026-11-11T16:00:00Z"
    before = deepcopy(raw)
    result = tracking.cash_bridge(raw, current)
    assert result["status"] == "stale" and result["stale"] and not result["complete"]
    assert "released_cash_flow.source_fingerprint.stale" in result["missing"]
    assert result["allocated_monthly"] == 3500 and raw == before


@pytest.mark.parametrize("left,right,exceeded", [(1000, 1000, False), (1000, 1000.01, True)])
def test_existing_cost_capacity_is_shared_by_all_links_to_the_same_actual_cost(
    left, right, exceeded
):
    current = saved_loan()
    cost = expense(current_amount=2000)
    raw = assigned(
        body(expenses=[cost], adjustments=[]),
        current,
        [
            assignment(left, "existing_cost", expense_row_id=cost["id"]),
            assignment(right, "existing_cost", expense_row_id=cost["id"]),
            assignment(3500 - left - right),
        ],
    )
    result = tracking.cash_bridge(raw, current)
    links = result["linked_allocations"][:2]
    assert [row["capacity_monthly"] for row in links] == [2000, 2000]
    assert all(
        any("linked_capacity_exceeded" in value for value in row["issues"]) == exceeded
        for row in links
    )
    assert result["complete"] is not exceeded
    assert len(raw["expenses"]) == 1


def test_planned_increment_uses_only_extra_cost_not_entire_proposed_spend():
    current = saved_loan()
    cost = expense(current_amount=1000)
    change = action(kind="expense_increase", current_amount=1000, proposed_amount=1300)
    raw = assigned(
        body(change, expenses=[cost]),
        current,
        [
            assignment(300, "planned_increment", expense_row_id=cost["id"], action_id=change["id"]),
            assignment(3200),
        ],
    )
    evaluation = engine.evaluate(raw, current, now=NOW)
    assert evaluation["totals"]["proposed"]["annual_impact"] == -3600
    result = tracking.cash_bridge(raw, current, evaluation)
    assert result["linked_allocations"][0]["capacity_monthly"] == 300 and result["complete"]
    raw["released_cash_flow"]["allocations"][0]["amount_monthly"] = 301
    result = tracking.cash_bridge(raw, current, evaluation)
    assert (
        any("linked_capacity_exceeded" in value for value in result["missing"])
        and not result["complete"]
    )


def test_marketing_allocation_is_incremental_spend_not_net_profit_or_full_spend():
    current = saved_loan()
    cost = expense(category="marketing", current_amount=100)
    change = action(
        kind="marketing_investment",
        label="Additional advertising",
        current_amount=100,
        proposed_amount=600,
        revenue_return_multiple=5,
        contribution_margin_pct=50,
        driver_key="advertising",
        product_name="Consultations",
        explanation="Proposed consultation advertising",
        source_note="Synthetic advertising quote",
    )
    raw = assigned(
        body(change, expenses=[cost]),
        current,
        [
            assignment(500, "planned_increment", expense_row_id=cost["id"], action_id=change["id"]),
            assignment(3000),
        ],
    )
    evaluation = engine.evaluate(raw, current, now=NOW)
    assert evaluation["totals"]["proposed"]["annual_impact"] == 9000
    result = tracking.cash_bridge(raw, current, evaluation)
    assert result["linked_allocations"][0]["capacity_monthly"] == 500
    assert result["complete"] and len(raw["expenses"]) == 1


@pytest.mark.parametrize(
    "problem", ["savings", "unknown_cost", "unclassified", "invalid_action", "outside_operations"]
)
def test_invalid_linked_uses_fail_truthfully_instead_of_claiming_cash_was_assigned(problem):
    current = saved_loan()
    cost = expense(current_amount=None if problem == "unknown_cost" else 1000)
    change = action(proposed_amount=800)
    purpose = (
        None
        if problem == "unclassified"
        else "existing_cost"
        if problem in {"unknown_cost", "outside_operations"}
        else "planned_increment"
    )
    if problem == "outside_operations":
        cost["category"] = "loan_payments"
    if problem == "invalid_action":
        change["implementation_cost"] = None
    raw = assigned(
        body(change, expenses=[cost]),
        current,
        [
            assignment(
                100,
                purpose,
                expense_row_id=cost["id"],
                **({"action_id": change["id"]} if purpose == "planned_increment" else {}),
            ),
            assignment(3400),
        ],
    )
    evaluation = engine.evaluate(raw, current, now=NOW)
    result = tracking.cash_bridge(raw, current, evaluation)
    assert not result["complete"] and result["linked_allocations"][0]["status"] == "incomplete"
    assert result["linked_allocations"][0]["issues"]
    if problem in {"unknown_cost", "invalid_action"}:
        assert result["linked_allocations"][0]["capacity_monthly"] is None
