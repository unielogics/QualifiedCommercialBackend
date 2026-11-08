from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.enums import Role
from app.routers.financial_metrics import FinancialMetricComposition, review_composition
from app.services import financial_metric_composition as composition
from app.services import financial_metric_review as reviews
from app.tests.test_financial_metric_review import ACTOR, build, correction, file, pnl, target


def calculation(cockpit, key="dscr.profit_and_loss"):
    return next(row for row in cockpit["metric_review"]["calculations"] if row["key"] == key)


def payload(
    cockpit, *, key="dscr.profit_and_loss", excluded=(), adjustments=None, revision=0, **extra
):
    recipe = calculation(cockpit, key)["recipe"]
    return {
        "calculation_key": key,
        "action": "compose",
        "components": [
            {
                "input_key": row["input_key"],
                "included": row["included"] and row["input_key"] not in excluded,
            }
            for row in recipe["components"]
        ],
        "adjustments": adjustments or [],
        "reason": "Reviewed source and underwriting treatment",
        "expected_revision": revision,
        "source_stamp": recipe["source_stamp"],
        "idempotency_key": "recipe-save-1",
        **extra,
    }


def test_dscr_recipe_preserves_source_operands_and_ebitda_while_adopting_signed_reviewed_math():
    analyses = [pnl()]
    original = deepcopy(analyses)
    current = build(analyses)
    command = payload(
        current,
        excluded={"profit_and_loss.interest"},
        adjustments=[{"id": "deduction", "label": "Reviewed operating deduction", "amount": -500}],
    )
    state, changed = composition.change({}, current["metric_review"], actor=ACTOR, **command)
    assert changed and state["revision"] == 1
    updated = build(analyses, state)
    # (20,000 + 2,000 + 3,000 - 500) × 2 / 12,000.
    assert updated["dscr"]["headline"]["numerator"] == 49000
    assert updated["dscr"]["headline"]["value"] == 4.08
    assert updated["plan"]["evaluation"]["headline_before"] == 4.08
    assert updated["dscr"]["headline"]["staff_adjusted"]
    assert calculation(updated)["recipe"]["reviewed_subtotal"] == 24500
    assert calculation(updated)["source_value"] == 4.33
    assert target(updated, "profit_and_loss.interest")["value"] == 1000
    assert updated["ebitda"]["headline"]["value"] == 26000
    assert updated["net_profit"]["conclusion"]["value"] == 40000
    assert analyses == original


def test_period_ebitda_recipe_is_separately_labelled_and_never_replaces_the_source_fact():
    current = build()
    command = payload(
        current,
        key="profit_and_loss",
        adjustments=[
            {"id": "one-off", "label": "Reviewed one-time operating expense", "amount": 3000}
        ],
    )
    state, _ = composition.change({}, current["metric_review"], actor=ACTOR, **command)
    updated = build(state=state)
    result = calculation(updated, "profit_and_loss")
    assert result["value"] == 26000 and result["reviewed_value"] == 29000
    assert result["reviewed_label"] == "Staff-adjusted EBITDA"
    assert updated["ebitda"]["headline"]["value"] == 26000
    assert updated["dscr"]["headline"]["numerator"] == 52000


def test_bank_recipe_distinguishes_monthly_payment_addback_from_annual_denominator():
    analyses = [
        file(
            "bank_statement",
            {
                "bank": "Bank",
                "account_last4": "1234",
                "currency": "USD",
                "statement_period": f"2026-0{month}",
                "total_deposits_and_credits": 10000,
                "total_withdrawals_and_debits": 8000,
            },
            key=f"bank-{month}",
        )
        for month in (1, 2, 3)
    ]
    current = build(analyses)
    command = payload(
        current,
        key="dscr.bank_cash_flow",
        excluded={"debt.monthly"},
        adjustments=[{"id": "monthly", "label": "Monthly reviewed expense", "amount": -250}],
    )
    state, _ = composition.change({}, current["metric_review"], actor=ACTOR, **command)
    updated = build(analyses, state)
    assert updated["dscr"]["headline"]["numerator"] == 21000
    assert updated["dscr"]["headline"]["value"] == 1.75
    assert updated["debt"]["service"]["monthly"] == 1000
    assert updated["debt"]["service"]["annual"] == 12000
    assert calculation(updated, "dscr.bank_cash_flow")["recipe"]["denominator"]["editable"] is False


def test_reported_and_computed_ebitda_cannot_be_double_counted():
    current = build([pnl(ebitda=50000)])
    command = payload(current)
    for component in command["components"]:
        component["included"] = True
    with pytest.raises(HTTPException, match="Choose reported EBITDA"):
        composition.change({}, current["metric_review"], actor=ACTOR, **command)
    for component in command["components"]:
        component["included"] = component["input_key"] != "profit_and_loss.reported_ebitda"
    state, _ = composition.change({}, current["metric_review"], actor=ACTOR, **command)
    updated = build([pnl(ebitda=50000)], state)
    assert updated["dscr"]["headline"]["numerator"] == 52000
    assert updated["ebitda"]["headline"]["value"] == 50000


@pytest.mark.parametrize("source_change", ["analysis", "amount", "period", "account", "correction"])
def test_source_change_or_effective_operand_change_stales_recipe_instead_of_reusing_it(
    source_change,
):
    source = pnl()
    current = build([source])
    state, _ = composition.change(
        {},
        current["metric_review"],
        actor=ACTOR,
        **payload(current, excluded={"profit_and_loss.interest"}),
    )
    if source_change == "analysis":
        source["analysis_id"] = "replacement-analysis"
    elif source_change == "amount":
        source["key_facts"]["net_income"] = 10000
    elif source_change == "period":
        source["key_facts"]["period_end"] = "2026-07-31"
    elif source_change == "account":
        # The published denominator is part of a DSCR receipt, not a hidden constant.
        updated = build(
            [source],
            state,
            accepted_schedule={
                "body": {
                    "debts": [
                        {"id": "debt", "lender": "Lender", "balance": 5000, "monthly_payment": 500}
                    ]
                },
                "version_id": "new-debt",
            },
        )
    else:
        state = correction(build([source], state), value=30000, state=state)
    if source_change != "account":
        updated = build([source], state)
    assert not updated["dscr"]["headline"].get("staff_adjusted")
    assert len(updated["metric_review"]["stale_compositions"]) == 1


def test_replay_and_restore_are_atomic_audited_and_restore_source_calculation():
    current = build()
    command = payload(
        current,
        adjustments=[{"id": "deduct", "label": "Deduct annual-period expense", "amount": -100000}],
    )
    state, _ = composition.change({}, current["metric_review"], actor=ACTOR, **command)
    replay, changed = composition.change(state, {"calculations": []}, actor=ACTOR, **command)
    assert not changed and replay == state
    reviewed = build(state=state)
    assert reviewed["dscr"]["headline"]["value"] == -12.33
    restored, _ = composition.change(
        state,
        reviewed["metric_review"],
        actor=ACTOR,
        **payload(
            reviewed, action="restore", components=[], revision=1, idempotency_key="restore-recipe"
        ),
    )
    result = build(state=restored)
    assert result["dscr"]["headline"]["value"] == 4.33
    assert len(result["metric_review"]["composition_history"]) == 2
    assert result["metric_review"]["history"] == []
    assert restored["events"][0]["actor_id"] == str(ACTOR.id)


@pytest.mark.parametrize(
    "issue",
    [
        "revision",
        "stamp",
        "anchor",
        "duplicate",
        "missing",
        "infinite",
        "role",
        "reason",
        "denominator",
        "currency",
    ],
)
def test_unscoped_or_invalid_recipes_are_rejected_without_mutating_history(issue):
    current = build([pnl(currency="EUR" if issue == "currency" else "USD")])
    state = {"revision": 0, "events": []}
    command = payload(current)
    actor = ACTOR
    if issue == "revision":
        command["expected_revision"] = 2
    elif issue == "stamp":
        command["source_stamp"] = "x" * 64
    elif issue == "anchor":
        command["components"][1]["included"] = False
    elif issue == "duplicate":
        command["components"].append(command["components"][0])
    elif issue == "missing":
        command["components"].pop()
    elif issue == "infinite":
        command["adjustments"] = [{"id": "a", "label": "Bad", "amount": float("inf")}]
    elif issue == "role":
        actor = SimpleNamespace(role=Role.BROKER)
    elif issue == "reason":
        command["reason"] = " "
    elif issue == "denominator":
        command["components"].append({"input_key": "debt.annual", "included": False})
    original = deepcopy(state)
    with pytest.raises(HTTPException):
        composition.change(state, current["metric_review"], actor=actor, **command)
    assert state == original


def test_factual_batch_validates_all_fields_before_persisting_and_replays_one_receipt():
    current = build()
    corrections = [
        {
            "input_key": name,
            "action": "correct",
            "value": value,
            "mark_unknown": False,
            "source_stamp": target(current, name)["source_stamp"],
        }
        for name, value in (
            ("profit_and_loss.net_income", -1000),
            ("profit_and_loss.interest", 500),
        )
    ]
    args = dict(
        corrections=corrections,
        reason="Checked the same source PDF",
        expected_revision=0,
        idempotency_key="atomic-review",
        actor=ACTOR,
    )
    state, changed = reviews.change_many({}, current["metric_review"], **args)
    assert changed and state["revision"] == 2
    replay, changed = reviews.change_many(state, {"inputs": []}, **args)
    assert not changed and replay == state
    updated = build(state=state)
    assert updated["ebitda"]["headline"]["value"] == 4500
    broken = deepcopy(corrections)
    broken[1]["source_stamp"] = "x" * 64
    empty = {"revision": 0, "events": []}
    with pytest.raises(HTTPException):
        reviews.change_many(empty, current["metric_review"], **{**args, "corrections": broken})
    assert empty == {"revision": 0, "events": []}


@pytest.mark.asyncio
async def test_route_blocks_broker_before_profile_or_source_reads():
    command = FinancialMetricComposition(**payload(build()))
    with patch("app.routers.financial_metrics.profiles.load_profile", AsyncMock()) as load:
        with pytest.raises(HTTPException) as caught:
            await review_composition(
                uuid4(), command, SimpleNamespace(role=Role.BROKER), AsyncMock()
            )
    assert caught.value.status_code == 403 and not load.called


def test_transport_rejects_boolean_components_and_nonfinite_adjustments():
    command = payload(build())
    command["adjustments"] = [{"id": "a", "label": "Bad", "amount": float("nan")}]
    with pytest.raises(ValidationError):
        FinancialMetricComposition(**command)
    command["adjustments"] = []
    command["components"][0]["included"] = "yes"
    with pytest.raises(ValidationError):
        FinancialMetricComposition(**command)


def test_recipe_decimal_sum_is_deterministic_and_adjustments_are_normalized_before_arithmetic():
    recipe = {"components": [{"input_key": str(index), "contribution": value} for index, value in enumerate(
        [-51.942, 321.181, 924.319, 530.66, -764.783])], "annualization_factor": 2, "unit": "currency"}
    choices = [{"input_key": row["input_key"], "included": True} for row in recipe["components"]]
    adjustments = [{"amount": value} for value in [-0.31, 85.13, -95.36]]
    assert composition._calculate(recipe, choices, adjustments) == (948.90, 1897.80, 1897.80)
    recipe["components"] = [{"input_key": "a", "contribution": 1}]
    assert composition._calculate(recipe, [{"input_key": "a", "included": True}], [{"amount": 0.005}]) == (1, 2, 2)
