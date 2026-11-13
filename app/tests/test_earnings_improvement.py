from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.schemas.earnings_improvement import (
    REVIEW_FIELDS,
    completion,
    empty_body,
    key_facts,
    validate_body,
)
from app.services import earnings_improvement as plan

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ACTOR = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
SOURCE, QUOTE, EXPENSE, ACTION, OFFER = [str(uuid4()) for _ in range(5)]


@pytest.mark.parametrize("blank", ["", " ", "\t"])
def test_cleared_worksheet_amounts_and_optional_links_stay_unknown(blank):
    raw = body()
    raw["expenses"][0].update(current_amount=blank, excluded="false", essential="true")
    raw["adjustments"][0].update(current_amount=blank, proposed_amount=blank,
        implementation_cost=blank, merchant_offer_id=blank, expense_row_id=blank)
    normalized = validate_body(raw)
    assert normalized["expenses"][0]["current_amount"] is None
    assert normalized["expenses"][0]["excluded"] is False
    assert normalized["expenses"][0]["essential"] is True
    for key in ("current_amount", "proposed_amount", "implementation_cost", "merchant_offer_id", "expense_row_id"):
        assert normalized["adjustments"][0][key] is None


def test_numeric_zero_remains_explicit_and_invalid_amounts_still_fail():
    raw = body()
    raw["expenses"][0]["current_amount"] = "0"
    assert validate_body(raw)["expenses"][0]["current_amount"] == 0
    raw["expenses"][0]["current_amount"] = "not a number"
    with pytest.raises(HTTPException) as error:
        validate_body(raw)
    assert error.value.status_code == 422


@pytest.mark.parametrize("category", ["cogs", "marketing", "software", "salaries", "professional_fees", "insurance"])
def test_unit_costs_work_for_every_operating_category_and_do_not_add_source_total(category):
    disclosed = expense(category=category, cost_basis="per_unit", unit_label="seat", current_units_per_period=20,
        current_unit_cost=50, current_fixed_amount=0)
    proposed = action(cost_basis="per_unit", unit_label="seat", current_units_per_period=20,
        proposed_units_per_period=15, current_unit_cost=50, proposed_unit_cost=40,
        current_fixed_amount=0, proposed_fixed_amount=100, current_amount=None, proposed_amount=None)
    result = plan.evaluate(body(proposed, expenses=[disclosed]), context(), now=NOW)
    assert result["disclosure"]["operating_cash_monthly"] == 1000
    assert result["disclosure"]["cost_mix"][0]["annual_amount"] == 12000
    assert result["totals"]["proposed"]["annual_impact"] == 3600
    assert result["actions"][0]["calculation"]["proposed_units_per_period"] == 15
    assert result["projection"]["proposed"]["earnings"] == -6400


def test_unit_unknown_fixed_amount_is_unknown_and_explicit_zero_is_supported():
    disclosed = expense(cost_basis="per_unit", unit_label="hour", current_units_per_period=10,
        current_unit_cost=100, current_fixed_amount=None)
    result = plan.evaluate(body(expenses=[disclosed]), context(), now=NOW)
    assert not result["disclosure"]["complete"]
    assert result["disclosure"]["cost_mix"][0]["missing_count"] == 1
    assert "current_fixed_amount" in str(result["disclosure"]["missing_inputs"])
    assert not completion(body(expenses=[disclosed]))["complete"]
    disclosed.update(current_units_per_period=0, current_unit_cost=100, current_fixed_amount=0, current_amount=None)
    result = plan.evaluate(body(expenses=[disclosed]), context(), now=NOW)
    assert result["disclosure"]["operating_monthly"] == 0
    assert result["disclosure"]["complete"]


def test_unit_source_total_mismatch_and_increase_direction_cannot_be_approved():
    disclosed = expense(cost_basis="per_unit", unit_label="item", current_units_per_period=10,
        current_unit_cost=100, current_fixed_amount=50)
    proposed = action(cost_basis="per_unit", unit_label="item", current_units_per_period=10,
        proposed_units_per_period=10, current_unit_cost=100, proposed_unit_cost=120,
        current_fixed_amount=50, proposed_fixed_amount=50, current_amount=None, proposed_amount=None)
    result = plan.evaluate(body(proposed, expenses=[disclosed]), context(), now=NOW)
    assert not result["disclosure"]["complete"]
    assert any("recorded total" in message for message in result["actions"][0]["issues"])
    assert any("direction" in message for message in result["actions"][0]["issues"])
    assert result["projection"]["proposed"]["earnings"] is None
    disclosed["current_amount"] = 1050
    proposed["kind"] = "expense_increase"
    result = plan.evaluate(body(proposed, expenses=[disclosed]), context(), now=NOW)
    assert result["totals"]["proposed"]["annual_impact"] == -2400


def test_matching_total_cannot_hide_mismatched_current_unit_drivers():
    disclosed = expense(cost_basis="per_unit", unit_label="seat", current_units_per_period=20,
        current_unit_cost=50, current_fixed_amount=0)
    proposed = action(cost_basis="per_unit", unit_label="seat", current_units_per_period=10,
        proposed_units_per_period=10, current_unit_cost=100, proposed_unit_cost=90,
        current_fixed_amount=0, proposed_fixed_amount=0, current_amount=None, proposed_amount=None)
    result = plan.evaluate(body(proposed, expenses=[disclosed]), context(), now=NOW)
    assert any("not just to the same total" in issue for issue in result["actions"][0]["issues"])
    assert not result["totals"]["proposed"]["complete"]


def test_granular_defaults_preserve_total_basis_review_stamps_but_driver_edits_stale_them():
    legacy = body()
    for row in legacy["expenses"]:
        for key in ("cost_basis", "unit_label", "current_units_per_period", "current_unit_cost", "current_fixed_amount"):
            row.pop(key, None)
    for row in legacy["adjustments"]:
        for key in ("cost_basis", "unit_label", "current_units_per_period", "proposed_units_per_period", "current_fixed_amount", "proposed_fixed_amount"):
            row.pop(key, None)
    normalized = validate_body(legacy)
    assert plan.proposal_stamp(legacy["adjustments"][0], legacy["expenses"][0], legacy) == plan.proposal_stamp(normalized["adjustments"][0], normalized["expenses"][0], normalized)
    accepted = approve(normalized)
    accepted["expenses"][0].update(cost_basis="per_unit", unit_label="seat", current_units_per_period=20,
        current_unit_cost=50, current_fixed_amount=0)
    assert plan.evaluate(accepted, context(), now=NOW)["actions"][0]["review_state"] == "stale"


@pytest.mark.parametrize("ratio,earnings,recommended", [(1.09, 13080, True), (1.1, 13200, False), (1.2, -10, True), (None, None, None)])
def test_earnings_plan_recommendation_uses_signed_income_and_110_threshold(ratio, earnings, recommended):
    cockpit = context()
    cockpit["ebitda"]["headline"]["annualized_value"] = earnings
    cockpit["dscr"]["headline"]["value"] = ratio
    result = plan.evaluate(body(), cockpit, now=NOW)
    assert result["recommendation"]["recommended"] is recommended
    assert result["recommendation"]["threshold"] == 1.1
    assert any(target["ratio"] == 1.1 for target in result["targets"]["current"])


def context():
    headline = {"key": "tax_return", "kind": "tax_estimate", "value": -10000,
        "annualized_value": -10000, "period": "2025", "currency": "USD", "basis": "cash",
        "source_refs": [{"entity": "Example LLC", "file_id": SOURCE}], "provisional": True}
    return {"ebitda": {"headline": headline, "potential": {"baseline": {"revenue": 100000}},
                       "source_selection": {"selected_file_id": SOURCE, "source_stamp": "source-v1", "stale": False}},
        "noi": {"headline": {**headline, "annualized_value": -15000}},
        "debt": {"service": {"annual": 12000, "currency": "USD", "version_id": "debt-v1"}},
        "dscr": {"headline": {"value": -0.83}},
        "plan": {"record": {}, "evaluation": {"active": True, "debt_service_after_annual": 6000}},
        "operating_evidence": {"business_name": "Example LLC", "files": [
            {"file_id": SOURCE, "file_name": "statement.pdf", "content_hash": "source-hash", "analysis_id": "analysis-1", "association_stamp": "assoc-1", "eligible_for_review": True},
            {"file_id": QUOTE, "file_name": "quote.pdf", "content_hash": "quote-hash", "analysis_id": "analysis-2", "association_stamp": "assoc-2", "eligible_for_review": True}]}}


def expense(**extra):
    return {"id": EXPENSE, "category": "rent", "label": "Rent", "current_amount": 1000,
            "cadence": "monthly", "source_file_ids": [SOURCE], **extra}


def action(**extra):
    return {"id": ACTION, "kind": "expense_reduction", "label": "Renegotiate rent", "driver_key": "rent",
        "expense_row_id": EXPENSE, "current_amount": 1000, "proposed_amount": 800, "cadence": "monthly",
        "implementation_cost": 0, "effective_date": "2026-01-01", "explanation": "Lower renewal rent",
        "source_file_ids": [SOURCE], "source_note": "Landlord's documented renewal terms", **extra}


def body(*actions, expenses=None, **extra):
    return validate_body({**empty_body(), "business_name": "Example LLC", "period_start": "2025-01-01",
        "period_end": "2025-12-31", "projection_start": "2026-01-01", "currency": "USD", "basis": "cash",
        "expenses": [expense()] if expenses is None else expenses,
        "adjustments": list(actions) or [action()], **extra})


def approve(raw, cockpit=None, **extra):
    return plan.review_actions(raw, cockpit or context(), [row["id"] for row in raw["adjustments"]], "approved",
        "PRIVATE REVIEW REASON", ACTOR, baseline_treatment="included_in_baseline",
        baseline_reconciliation_note="Current amount reconciled to the 2025 baseline and renewal quote", now=NOW, **extra)


def test_negative_earnings_improve_with_positive_savings_without_rewriting_source():
    cockpit = context()
    original = deepcopy(cockpit)
    result = plan.evaluate(body(), cockpit, now=NOW)
    assert result["totals"]["proposed"]["annual_impact"] == 2400
    assert result["projection"]["proposed"]["earnings"] == -7600
    assert result["projection"]["proposed"]["noi"] == -12600
    assert result["projection"]["proposed"]["dscr_current"] == -0.63
    assert result["projection"]["proposed"]["dscr_with_refinance"] == -1.27
    assert result["projection"]["approved"]["status"] == "awaiting_review"
    assert result["projection"]["approved"]["earnings"] is None
    assert cockpit == original


def test_staff_approval_requires_evidence_and_baseline_reconciliation():
    raw = body()
    accepted = approve(raw)
    result = plan.evaluate(accepted, context(), now=NOW)
    assert result["actions"][0]["review_state"] == "approved"
    assert result["projection"]["approved"]["earnings"] == -7600
    assert result["all_actions_reviewed"]
    assert raw["adjustments"][0]["status"] == "proposed"
    unreconciled = plan.review_actions(raw, context(), [ACTION], "approved", "Evidence checked", ACTOR, now=NOW)
    result = plan.evaluate(unreconciled, context(), now=NOW)
    assert result["actions"][0]["review_state"] == "needs_baseline_reconciliation"
    assert result["projection"]["approved"]["earnings"] is None


@pytest.mark.parametrize("change", ["action", "expense", "source_hash", "association", "quote", "baseline", "scope"])
def test_any_changed_proposal_expense_or_evidence_invalidates_review(change):
    cockpit = context()
    raw = approve(body(), cockpit)
    if change == "action":
        raw["adjustments"][0]["proposed_amount"] = 750
    elif change == "expense":
        raw["expenses"][0]["notes"] = "New information"
    elif change == "source_hash":
        cockpit["operating_evidence"]["files"][0]["content_hash"] = "replacement-hash"
    elif change == "association":
        cockpit["operating_evidence"]["files"][0]["association_stamp"] = "association-updated"
    elif change == "quote":
        cockpit["operating_evidence"]["files"][1]["content_hash"] = "quote-v2"
    elif change == "baseline":
        cockpit["ebitda"]["headline"]["annualized_value"] = 50000
    else:
        raw["period_start"] = "2026-01-01"
        raw["period_end"] = "2026-12-31"
    result = plan.evaluate(raw, cockpit, now=NOW)
    assert result["actions"][0]["review_state"] == "stale"
    assert result["projection"]["approved"]["earnings"] is None


def test_price_and_product_cost_are_separate_signed_actions_with_one_volume():
    price = action(kind="price_change", driver_key="service-a", expense_row_id=None, units_per_period=100,
        current_unit_price=10, proposed_unit_price=12)
    cost = action(id=str(uuid4()), kind="product_cost_change", driver_key="service-a", expense_row_id=None,
        units_per_period=100, current_unit_cost=4, proposed_unit_cost=5)
    result = plan.evaluate(body(price, cost), context(), now=NOW)
    assert [row["net_annual_impact"] for row in result["actions"]] == [2400, -1200]
    assert result["totals"]["proposed"]["annual_impact"] == 1200
    assert result["totals"]["proposed"]["complete"]
    cost["units_per_period"] = 150
    result = plan.evaluate(body(price, cost), context(), now=NOW)
    assert not result["totals"]["proposed"]["complete"]
    assert "Overlapping" in result["actions"][0]["issues"][0]


def test_revenue_growth_uses_margin_and_cannot_stack_with_product_model():
    revenue = action(kind="revenue_change", expense_row_id=None, current_amount=10000,
        proposed_amount=12000, contribution_margin_pct=40)
    result = plan.evaluate(body(revenue), context(), now=NOW)
    assert result["totals"]["proposed"]["annual_impact"] == 9600
    price = action(id=str(uuid4()), kind="price_change", driver_key="product", expense_row_id=None,
        units_per_period=100, current_unit_price=10, proposed_unit_price=12)
    result = plan.evaluate(body(revenue, price), context(), now=NOW)
    assert not result["totals"]["proposed"]["complete"]
    assert all("aggregate revenue" in row["issues"][-1] for row in result["actions"])


def test_implementation_costs_and_effective_date_are_not_hidden_in_run_rate():
    result = plan.evaluate(body(action(implementation_cost=1000, effective_date="2026-07-01")), context(), now=NOW)
    row = result["actions"][0]
    assert row["net_annual_impact"] == 2400
    assert row["first_year_impact"] == round(2400 * 184 / 365 - 1000, 2)
    result = plan.evaluate(body(action(implementation_cost=10, implementation_cost_cadence="monthly")), context(), now=NOW)
    assert result["totals"]["proposed"]["annual_impact"] == 2280
    assert result["totals"]["proposed"]["first_year_impact"] == 2280


def test_one_time_effect_never_becomes_recurring_coverage():
    once = action(kind="one_time_adjustment", expense_row_id=None, current_amount=1000, proposed_amount=0,
        implementation_cost=100)
    result = plan.evaluate(body(once), context(), now=NOW)
    assert result["totals"]["proposed"]["annual_impact"] == 0
    assert result["totals"]["proposed"]["first_year_impact"] == 900
    assert result["projection"]["proposed"]["earnings"] == -10000
    assert result["projection"]["proposed"]["first_year_earnings"] == -9100


def test_unknown_implementation_cost_does_not_become_zero():
    result = plan.evaluate(body(action(implementation_cost=None)), context(), now=NOW)
    assert result["actions"][0]["net_annual_impact"] is None
    assert not result["totals"]["proposed"]["complete"]
    assert result["projection"]["proposed"]["earnings"] is None
    assert result["totals"]["proposed"]["annual_impact"] is None


def test_unquantified_actions_and_categories_never_claim_zero_savings_or_costs():
    unknown = expense(current_amount=None)
    raw = body(action(proposed_amount=None, implementation_cost=None), expenses=[unknown])
    result = plan.evaluate(raw, context(), now=NOW)
    totals = result["totals"]["proposed"]
    assert totals["expected_count"] == 1 and totals["count"] == 0
    assert totals["annual_impact"] is totals["monthly_impact"] is totals["first_year_impact"] is totals["one_time_cost"] is None
    assert totals["known_annual_subtotal"] is None
    category = result["disclosure"]["cost_mix"][0]
    assert category["annual_amount"] is None and category["quantified_count"] == 0
    assert category["missing_count"] == 1
    summary = plan.package_summary(result)
    assert summary["totals"]["proposed"]["annual_impact"] is None


def test_partial_plan_exposes_known_subtotal_but_no_complete_projection_total():
    second_expense, second_action = str(uuid4()), str(uuid4())
    raw = body(action(), action(id=second_action, driver_key="software", expense_row_id=second_expense,
        current_amount=100, proposed_amount=None), expenses=[expense(), expense(id=second_expense, category="software", current_amount=100)])
    result = plan.evaluate(raw, context(), now=NOW)
    totals = result["totals"]["proposed"]
    assert totals["known_annual_subtotal"] == 2400
    assert totals["annual_impact"] is None and not totals["complete"]
    assert result["projection"]["proposed"]["earnings"] is None
    raw["adjustments"] = []
    result = plan.evaluate(raw, context(), now=NOW)
    assert result["totals"]["proposed"]["annual_impact"] == 0
    raw["expenses"][0]["current_amount"] = 0
    result = plan.evaluate(raw, context(), now=NOW)
    assert result["disclosure"]["cost_mix"][0]["annual_amount"] == 0


@pytest.mark.parametrize("category", ["loan_payments", "income_taxes", "owner_draws", "transfers"])
def test_nonoperating_and_noncash_changes_do_not_improve_operating_earnings(category):
    result = plan.evaluate(body(expenses=[expense(category=category)]), context(), now=NOW)
    assert not result["totals"]["proposed"]["complete"]
    assert result["projection"]["proposed"]["earnings"] is None
    with pytest.raises(HTTPException) as caught:
        approve(body(expenses=[expense(category=category)]))
    assert caught.value.status_code == 422


def test_expense_current_amount_must_reconcile_and_frequency_is_exact():
    invalid = plan.evaluate(body(action(current_amount=900)), context(), now=NOW)
    assert "reconcile" in invalid["actions"][0]["issues"][0]
    raw = body(action(current_amount=1000, proposed_amount=800, cadence="semi_monthly"),
        expenses=[expense(cadence="semi_monthly")])
    result = plan.evaluate(raw, context(), now=NOW)
    assert result["totals"]["proposed"]["annual_impact"] == 4800
    raw["expenses"][0]["cadence"] = "biweekly"
    raw["adjustments"][0]["cadence"] = "biweekly"
    assert plan.evaluate(raw, context(), now=NOW)["totals"]["proposed"]["annual_impact"] == 5200


@pytest.mark.parametrize("fault", ["missing", "identity", "outside_scope"])
def test_unaccepted_or_foreign_support_cannot_be_approved(fault):
    cockpit = context()
    raw = body()
    extra = {}
    if fault == "missing":
        raw["adjustments"][0]["source_file_ids"] = []
        raw["expenses"][0]["source_file_ids"] = []
    elif fault == "identity":
        cockpit["operating_evidence"]["files"][0]["eligible_for_review"] = False
    else:
        extra["source_file_ids"] = [QUOTE]
    with pytest.raises(HTTPException) as caught:
        approve(raw, cockpit, **extra)
    assert caught.value.status_code == 422


@pytest.mark.parametrize("change", ["company", "currency", "basis", "stale_selection"])
def test_noncomparable_baseline_does_not_produce_projected_earnings(change):
    cockpit = context()
    raw = body()
    if change == "company":
        raw["business_name"] = "Unrelated Business Inc"
    elif change == "currency":
        raw["currency"] = "EUR"
    elif change == "basis":
        raw["basis"] = "accrual"
    else:
        cockpit["ebitda"]["source_selection"]["stale"] = True
    result = plan.evaluate(raw, cockpit, now=NOW)
    assert result["projection"]["proposed"]["earnings"] is None
    assert result["missing"]


def processing_context():
    cockpit = context()
    cockpit["operating_evidence"]["merchant_sources"] = [{"source_key": "processor:1234:USD", "file_id": SOURCE,
        "eligible_for_review": True, "currency": "USD", "volume": 100000, "total_fees": 3200,
        "effective_rate_pct": 3.2, "rate_basis": "all_in", "period_start": "2025-12-01", "period_end": "2025-12-31"}]
    cockpit["operating_evidence"]["merchant_offers"] = [{"id": OFFER, "source_file_id": QUOTE, "status": "draft",
        "terms_version": 1, "current_monthly_volume": 100000, "proposed_effective_rate_pct": 2.4,
        "proposed_monthly_fees": 2400, "early_termination_fee": 1000}]
    return cockpit


def processing_action(**extra):
    return action(**{"kind": "processing_cost_change", "driver_key": "processor", "expense_row_id": EXPENSE,
        "merchant_source_key": "processor:1234:USD", "merchant_offer_id": OFFER, "processing_volume": 100000,
        "current_processing_rate_pct": 3.2, "proposed_processing_rate_pct": 2.4,
        "current_processing_rate_basis": "all_in", "proposed_processing_rate_basis": "all_in",
        "implementation_cost": 1000, **extra})


def processing_body(*actions):
    return body(*(actions or [processing_action()]), expenses=[expense(category="merchant_processing", current_amount=3200)])


def test_merchant_savings_bind_current_statement_and_comparable_quote_and_costs():
    cockpit = processing_context()
    raw = processing_body()
    result = plan.evaluate(approve(raw, cockpit), cockpit, now=NOW)
    assert result["totals"]["approved"]["annual_impact"] == 9600
    assert result["totals"]["approved"]["first_year_impact"] == 8600
    assert result["projection"]["approved"]["earnings"] == -400
    assert result["actions"][0]["calculation"]["processing_volume"] == 100000


@pytest.mark.parametrize("fault", ["quote_rate", "statement_volume", "quote_volume", "withdrawn", "termination", "quote_identity", "partial_period"])
def test_processor_approval_rejects_unsupported_or_noncomparable_terms(fault):
    cockpit = processing_context()
    raw = processing_body()
    row = raw["adjustments"][0]
    offer = cockpit["operating_evidence"]["merchant_offers"][0]
    if fault == "quote_rate":
        row["proposed_processing_rate_pct"] = 1
    elif fault == "statement_volume":
        row["processing_volume"] = 200000
    elif fault == "quote_volume":
        offer["current_monthly_volume"] = 200000
    elif fault == "withdrawn":
        offer["status"] = "withdrawn"
    elif fault == "termination":
        row["implementation_cost"] = 0
    elif fault == "quote_identity":
        cockpit["operating_evidence"]["files"][1]["eligible_for_review"] = False
    else:
        cockpit["operating_evidence"]["merchant_sources"][0]["period_start"] = "2025-12-15"
    with pytest.raises(HTTPException) as caught:
        approve(raw, cockpit)
    assert caught.value.status_code == 422


def test_all_in_rate_cannot_count_fixed_fees_again_and_markup_basis_must_match():
    raw = processing_body()
    raw["adjustments"][0]["current_processing_fixed_fee"] = 20
    result = plan.evaluate(raw, processing_context(), now=NOW)
    assert "second time" in result["actions"][0]["issues"][0]
    raw["adjustments"][0]["current_processing_fixed_fee"] = None
    raw["adjustments"][0]["proposed_processing_rate_basis"] = "processor_markup"
    result = plan.evaluate(raw, processing_context(), now=NOW)
    assert "same documented" in result["actions"][0]["issues"][0]


def test_whole_expense_and_processor_savings_cannot_both_be_approved():
    raw = processing_body(processing_action(), action(id=str(uuid4()), driver_key="other",
        current_amount=3200, proposed_amount=2400))
    result = plan.evaluate(raw, processing_context(), now=NOW)
    assert not result["totals"]["proposed"]["complete"]
    with pytest.raises(HTTPException) as caught:
        approve(raw, processing_context())
    assert caught.value.status_code == 422
    accepted_one = plan.review_actions(raw, processing_context(), [ACTION], "approved", "Choose processor model", ACTOR,
        baseline_treatment="included_in_baseline", baseline_reconciliation_note="Reconciled statement fees", now=NOW)
    result = plan.evaluate(accepted_one, processing_context(), now=NOW)
    assert result["totals"]["approved"]["annual_impact"] == 9600


def test_unlinked_processor_action_cannot_duplicate_evidenced_expense_savings():
    raw = processing_body(processing_action(expense_row_id=None), action(id=str(uuid4()), driver_key="other",
        current_amount=3200, proposed_amount=2400))
    result = plan.evaluate(raw, processing_context(), now=NOW)
    assert not result["totals"]["proposed"]["complete"]
    with pytest.raises(HTTPException) as caught:
        approve(raw, processing_context())
    assert caught.value.status_code == 422


def test_linked_processor_expense_must_match_its_category_and_fee_amount():
    for field, value in (("category", "rent"), ("current_amount", 1000)):
        raw = processing_body()
        raw["expenses"][0][field] = value
        with pytest.raises(HTTPException) as caught:
            approve(raw, processing_context())
        assert caught.value.status_code == 422


def test_annual_markup_fixed_fees_are_normalized_to_monthly_quote_terms():
    cockpit = processing_context()
    source = cockpit["operating_evidence"]["merchant_sources"][0]
    source.update(volume=1200000, transactions=24000, rate_basis="processor_markup", effective_rate_pct=.5,
        fixed_fee=120, per_transaction_fee=.1, period_start="2025-01-01")
    offer = cockpit["operating_evidence"]["merchant_offers"][0]
    offer.update(proposed_markup_bps=30, proposed_fixed_monthly_fees=20, proposed_per_item_fee=.05)
    row = processing_action()
    row.update(expense_row_id=None, cadence="annual", processing_volume=1200000,
        processing_transaction_count=24000, current_processing_rate_basis="processor_markup",
        proposed_processing_rate_basis="processor_markup", current_processing_rate_pct=.5,
        proposed_processing_rate_pct=.3, current_processing_fixed_fee=120, proposed_processing_fixed_fee=240,
        current_processing_per_transaction_fee=.1, proposed_processing_per_transaction_fee=.05)
    result = plan.evaluate(approve(body(row), cockpit), cockpit, now=NOW)
    assert result["totals"]["approved"]["annual_impact"] == 3480


def test_client_cannot_review_and_exports_never_reveal_private_review_metadata():
    with pytest.raises(HTTPException) as caught:
        plan.review_actions(body(), context(), [ACTION], "approved", "Checked", SimpleNamespace(id=uuid4(), role=Role.CLIENT), now=NOW)
    assert caught.value.status_code == 403
    accepted = approve(body())
    facts = key_facts(accepted)
    assert all(field not in facts["operating_plan"]["adjustments"][0] for field in REVIEW_FIELDS)
    public = plan.package_summary(plan.evaluate(accepted, context(), now=NOW))
    assert public["adjustments"][0]["source_file_ids"] == [SOURCE]
    assert public["adjustments"][0]["explanation"] == "Lower renewal rent"
    assert "PRIVATE REVIEW REASON" not in str(public)
    assert str(ACTOR.id) not in str(public)
    assert accepted["adjustments"][0]["review_source_stamp"] not in str(public)


def test_recoverable_exclusion_and_duplicate_do_not_affect_current_totals():
    duplicate = action(id=str(uuid4()), duplicate_of=ACTION)
    raw = body(action(), duplicate)
    assert raw["adjustments"][1]["excluded"]
    result = plan.evaluate(raw, context(), now=NOW)
    assert result["totals"]["proposed"]["count"] == 1
    assert result["totals"]["proposed"]["annual_impact"] == 2400
    raw["adjustments"][0]["excluded"] = True
    raw["adjustments"][1]["duplicate_of"] = None
    result = plan.evaluate(raw, context(), now=NOW)
    assert result["projection"]["proposed"]["status"] == "no_actions"
    assert not result["all_actions_reviewed"]


@pytest.mark.parametrize("value", [True, float("inf"), float("nan"), -1])
def test_invalid_amounts_are_rejected_and_explicit_zero_is_complete(value):
    with pytest.raises(HTTPException) as caught:
        body(action(implementation_cost=value))
    assert caught.value.status_code == 422
    assert completion(body())["complete"]


def test_unknown_or_zero_debt_and_missing_earnings_never_fabricate_coverage():
    cockpit = context()
    cockpit["ebitda"]["headline"]["annualized_value"] = None
    cockpit["debt"]["service"]["annual"] = 0
    cockpit["plan"]["evaluation"]["coverage_unavailable_reason"] = "Replacement payment missing"
    result = plan.evaluate(body(), cockpit, now=NOW)
    assert result["projection"]["proposed"]["earnings"] is None
    assert result["projection"]["proposed"]["dscr_current"] is None
    assert result["projection"]["proposed"]["dscr_with_refinance"] is None
    assert result["targets"] == {"current": [], "refinance": []}


def test_unchanged_reload_timestamps_do_not_invalidate_review():
    cockpit = context()
    stamp = plan.context_stamp(cockpit)
    cockpit["generated_at"] = NOW.isoformat()
    assert plan.context_stamp(cockpit) == stamp
