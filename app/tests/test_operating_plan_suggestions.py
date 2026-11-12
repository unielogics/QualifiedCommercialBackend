"""Suggested numbers are supplied by sources and the existing calculator."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.services import operating_plan_suggestions as suggestions
from app.tests.test_earnings_improvement import (
    QUOTE,
    SOURCE,
    body,
    context,
    expense,
    processing_body,
    processing_context,
)


def goal(**changes):
    return {"status": "set", "metric": "annual_noi", "currency": "USD", "amount": 250000, "revision": 1, **changes}


def quoted():
    raw = body(expenses=[expense()], adjustments=[])
    cockpit = context()
    cockpit["operating_evidence"]["cost_quotes"] = [{"id": "comparable-renewal", "expense_row_id": raw["expenses"][0]["id"],
        "file_id": QUOTE, "status": "accepted", "currency": "USD", "cadence": "monthly", "amount": 900, "implementation_cost": 0}]
    return raw, cockpit


def ready(raw, cockpit):
    result = suggestions.build(raw, cockpit, goal(), raw["expenses"][0]["id"])
    return result, next(item for item in result["suggestions"] if item["status"] == "ready")


def test_documented_renewal_suggestion_is_1200_per_year_then_proposed_only():
    raw, cockpit = quoted()
    before = deepcopy((raw, cockpit))
    result, choice = ready(raw, cockpit)
    assert choice["calculation"]["annual_effect_before_setup"] == 1200
    assert choice["calculation"]["net_annual_effect"] == 1200
    assert {item["file_id"] for item in choice["source_refs"]} == {SOURCE, QUOTE}
    applied = suggestions.apply(raw, cockpit, goal(), raw["expenses"][0]["id"], choice["id"], result["context_stamp"])
    assert applied["status"] == applied["body"]["adjustments"][0]["status"] == "proposed"
    assert applied["body"]["expenses"] == raw["expenses"]
    assert applied["body"]["adjustments"][0]["reviewed_at"] is None
    assert applied["evaluation"]["projection"]["proposed"]["noi"] == -13800
    assert (raw, cockpit) == before


def test_unknown_setup_is_not_zero_or_a_completed_net_projection():
    raw, cockpit = quoted()
    cockpit["operating_evidence"]["cost_quotes"][0].pop("implementation_cost")
    result, choice = ready(raw, cockpit)
    assert choice["calculation"]["annual_effect_before_setup"] == 1200
    assert choice["calculation"]["net_annual_effect"] is None
    assert "implementation cost" in choice["missing"]
    proposed = suggestions.apply(raw, cockpit, goal(), raw["expenses"][0]["id"], choice["id"], result["context_stamp"])
    assert proposed["body"]["adjustments"][0]["implementation_cost"] is None
    assert proposed["evaluation"]["projection"]["proposed"]["noi"] is None


def test_documented_unit_quote_preserves_decimal_command_intent():
    raw, cockpit = quoted()
    row = raw["expenses"][0]
    row.update(cost_basis="per_unit", unit_label="consultation", current_amount=800,
        current_units_per_period=100, current_unit_cost=8, current_fixed_amount=0)
    cockpit["operating_evidence"]["cost_quotes"][0].update(unit_cost=7.2, unit_label="consultation")
    _, choice = ready(raw, cockpit)
    assert choice["command"]["value"] == 0.8
    assert choice["calculation"]["annual_effect_before_setup"] == 960


@pytest.mark.parametrize("fault", ["association", "currency", "cadence", "other_item", "withdrawn", "boolean"])
def test_noncomparable_or_unaccepted_quote_cannot_be_applied(fault):
    raw, cockpit = quoted()
    quote = cockpit["operating_evidence"]["cost_quotes"][0]
    if fault == "association":
        cockpit["operating_evidence"]["files"][1]["eligible_for_review"] = False
    elif fault == "currency":
        quote["currency"] = "CAD"
    elif fault == "cadence":
        quote["cadence"] = "annual"
    elif fault == "other_item":
        quote["expense_row_id"] = QUOTE
    elif fault == "withdrawn":
        quote["status"] = "withdrawn"
    else:
        quote["amount"] = True
    result = suggestions.build(raw, cockpit, goal(), raw["expenses"][0]["id"])
    assert not any(item["status"] == "ready" for item in result["suggestions"])
    assert all(item["calculation"]["annual_effect_before_setup"] is None for item in result["suggestions"])


@pytest.mark.parametrize("change", ["working_amount", "goal", "source", "quote"])
def test_changed_source_goal_or_intent_requires_refresh(change):
    raw, cockpit = quoted()
    result, choice = ready(raw, cockpit)
    objective = goal()
    if change == "working_amount":
        raw["expenses"][0]["current_amount"] += 1
    elif change == "goal":
        objective["revision"] += 1
    elif change == "source":
        cockpit["operating_evidence"]["files"][0]["content_hash"] = "changed-source"
    else:
        cockpit["operating_evidence"]["cost_quotes"][0]["amount"] = 800
    with pytest.raises(HTTPException) as caught:
        suggestions.apply(raw, cockpit, objective, raw["expenses"][0]["id"], choice["id"], result["context_stamp"])
    assert caught.value.status_code == 409 and caught.value.detail["code"] == "operating_suggestion_stale"


def test_applying_twice_or_reusing_rejected_history_never_duplicates_or_revives():
    raw, cockpit = quoted()
    result, choice = ready(raw, cockpit)
    applied = suggestions.apply(raw, cockpit, goal(), raw["expenses"][0]["id"], choice["id"], result["context_stamp"])
    with pytest.raises(HTTPException):
        suggestions.apply(applied["body"], cockpit, goal(), raw["expenses"][0]["id"], choice["id"], result["context_stamp"])
    history = deepcopy(applied["body"])
    history["adjustments"][0].update(status="rejected", excluded=True)
    refreshed = suggestions.build(history, cockpit, goal(), raw["expenses"][0]["id"])
    with pytest.raises(HTTPException) as caught:
        suggestions.apply(history, cockpit, goal(), raw["expenses"][0]["id"], choice["id"], refreshed["context_stamp"])
    assert caught.value.detail["code"] == "operating_suggestion_already_used"
    assert history["adjustments"][0]["status"] == "rejected"


def test_context_is_stable_under_file_query_order_and_private_read_timestamps():
    raw, cockpit = quoted()
    first = suggestions.build(raw, cockpit, goal(), raw["expenses"][0]["id"])
    cockpit["operating_evidence"]["files"].reverse()
    cockpit["noi"]["checked_at"] = "different-read-time"
    assert suggestions.build(raw, cockpit, goal(), raw["expenses"][0]["id"])["context_stamp"] == first["context_stamp"]


def test_missing_evidence_is_a_question_not_fabricated_250k_income_or_a_new_row():
    raw, cockpit = quoted()
    cockpit["operating_evidence"].pop("cost_quotes")
    result = suggestions.build(raw, cockpit, goal(), raw["expenses"][0]["id"])
    assert len(result["suggestions"]) == 1
    choice = result["suggestions"][0]
    assert choice["status"] == "needs_inputs" and choice["missing"]
    assert choice["calculation"]["annual_noi_goal_gap"] == 265000
    assert choice["calculation"]["annual_effect_before_setup"] is None
    assert choice["proposed_fields"] == {} and choice["command"] is None
    with pytest.raises(HTTPException):
        suggestions.apply(raw, cockpit, goal(), raw["expenses"][0]["id"], choice["id"], result["context_stamp"])


@pytest.mark.parametrize("objective", [goal(status="awaiting_staff", amount=None), goal(amount=0), goal(currency="CAD")])
def test_staff_goal_and_currency_prerequisites_cannot_be_bypassed(objective):
    raw, cockpit = quoted()
    with pytest.raises(HTTPException) as caught:
        suggestions.build(raw, cockpit, objective, raw["expenses"][0]["id"])
    assert caught.value.status_code == 409


def test_merchant_offer_matches_existing_calculator_before_explicit_implementation_cost():
    raw, cockpit = processing_body(), processing_context()
    raw["adjustments"] = []
    cockpit["operating_evidence"]["merchant_offers"][0]["implementation_cost"] = 1000
    result, choice = ready(raw, cockpit)
    assert choice["action_kind"] == "processing_cost_change"
    assert choice["calculation"]["annual_effect_before_setup"] == 9600
    assert choice["calculation"]["net_annual_effect"] == 9600
    assert choice["proposed_fields"]["implementation_cost"] == 1000
    applied = suggestions.apply(raw, cockpit, goal(), raw["expenses"][0]["id"], choice["id"], result["context_stamp"])
    assert applied["evaluation"]["totals"]["proposed"]["first_year_impact"] == 8600


@pytest.mark.parametrize("reconciled", [True, False])
def test_growth_threshold_requires_same_period_source_revenue_and_does_not_invent_demand(reconciled):
    from app.tests.test_operating_unit_economics import unit_plan

    raw, cockpit = unit_plan(), context()
    raw["adjustments"] = []
    if not reconciled:
        raw["historical_revenue_annual"] += 1
    result = suggestions.build(raw, cockpit, goal(), raw["expenses"][0]["id"])
    thresholds = [item for item in result["suggestions"] if item["key"] == "growth-threshold"]
    assert bool(thresholds) is reconciled
    if thresholds:
        choice = thresholds[0]
        assert choice["calculation"]["current_contribution_per_unit"] == 58
        assert choice["calculation"]["additional_units_needed"] == 4569
        assert choice["status"] == "needs_inputs" and choice["command"] is None
        assert choice["calculation"]["annual_effect_before_setup"] is None


@pytest.mark.asyncio
async def test_ai_ranks_ids_but_cannot_rewrite_any_financial_field():
    raw, cockpit = quoted()
    baseline = suggestions.build(raw, cockpit, goal(), raw["expenses"][0]["id"])
    order = [item["id"] for item in reversed(baseline["suggestions"])]
    with patch.object(suggestions, "get_settings", return_value=SimpleNamespace(bedrock_enabled=True)), patch.object(suggestions, "_rank", AsyncMock(return_value=order)):
        result = await suggestions.prepare(None, body=raw, cockpit=cockpit, goal=goal(), expense_row_id=raw["expenses"][0]["id"])
    assert result["generation_status"] == "ai_ranked"
    assert {item["id"]: item for item in result["suggestions"]} == {item["id"]: item for item in baseline["suggestions"]}


@pytest.mark.asyncio
async def test_ai_unavailability_keeps_honest_source_based_choices():
    raw, cockpit = quoted()
    with patch.object(suggestions, "get_settings", return_value=SimpleNamespace(bedrock_enabled=True)), patch.object(suggestions, "_rank", AsyncMock(side_effect=ValueError("unsupported output"))):
        result = await suggestions.prepare(None, body=raw, cockpit=cockpit, goal=goal(), expense_row_id=raw["expenses"][0]["id"])
    assert result["generation_status"] == "source_based" and result["notes"]
    assert next(item for item in result["suggestions"] if item["status"] == "ready")["calculation"]["annual_effect_before_setup"] == 1200
