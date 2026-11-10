"""Explicit marketing assumptions and NOI-only operating illustrations."""

from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.schemas.earnings_improvement import completion, validate_body
from app.services import earnings_improvement as plan
from app.tests.test_earnings_improvement import NOW, action, approve, body, context, expense


def marketing(**changes):
    return action(kind="marketing_investment", label="Reach new customers", driver_key="service-advertising",
        product_name="Example service", current_amount=1000, proposed_amount=2000,
        revenue_return_multiple=3, contribution_margin_pct=50, **changes)


def marketing_body(row=None, **changes):
    return body(row or marketing(), expenses=[expense(category="marketing", label="Advertising")], **changes)


def test_marketing_return_deducts_incremental_spend_once_and_keeps_historical_baseline():
    cockpit = context()
    original = deepcopy(cockpit)
    result = plan.evaluate(marketing_body(), cockpit, now=NOW)
    row = result["actions"][0]
    assert row["net_annual_impact"] == 6000
    assert row["calculation"]["incremental_spend"] == 1000
    assert row["calculation"]["incremental_revenue"] == 3000
    assert row["calculation"]["incremental_contribution"] == 1500
    assert row["product_name"] == "Example service"
    assert result["projection"]["proposed"]["noi"] == -9000
    assert result["projection"]["proposed"]["first_year_noi"] == -9000
    assert result["projection"]["approved"]["noi"] is None
    assert cockpit == original
    reviewed = plan.evaluate(approve(marketing_body()), cockpit, now=NOW)
    assert reviewed["projection"]["approved"]["noi"] == -9000
    assert reviewed["projection"]["approved"]["noi_status"] == "reviewed_illustration"


@pytest.mark.parametrize("multiple,margin,annual", [(0, 50, -12000), (1, 50, -6000), (3, 0, -12000), (2, 50, 0)])
def test_marketing_can_have_negative_or_zero_return_without_inventing_growth(multiple, margin, annual):
    raw = marketing_body()
    raw["adjustments"][0].update(revenue_return_multiple=multiple, contribution_margin_pct=margin)
    result = plan.evaluate(raw, context(), now=NOW)
    assert result["totals"]["proposed"]["complete"]
    assert result["totals"]["proposed"]["annual_impact"] == annual


@pytest.mark.parametrize("field", ["product_name", "revenue_return_multiple", "contribution_margin_pct", "expense_row_id"])
def test_marketing_requires_explicit_product_return_margin_and_link(field):
    raw = marketing_body()
    raw["adjustments"][0][field] = "" if field == "product_name" else None
    assert not completion(raw)["complete"]
    result = plan.evaluate(raw, context(), now=NOW)
    assert not result["totals"]["proposed"]["complete"]
    assert result["projection"]["proposed"]["noi"] is None


@pytest.mark.parametrize("fault", ["decrease", "category", "current", "one_time", "excluded"])
def test_marketing_reconciles_current_spend_and_rejects_wrong_treatment(fault):
    raw = marketing_body()
    row = raw["adjustments"][0]
    if fault == "decrease":
        row["proposed_amount"] = 800
    elif fault == "category":
        raw["expenses"][0]["category"] = "rent"
    elif fault == "current":
        row["current_amount"] = 900
    elif fault == "one_time":
        row["cadence"] = "one_time"
    else:
        raw["expenses"][0]["excluded"] = True
    result = plan.evaluate(raw, context(), now=NOW)
    assert result["actions"][0]["issues"]
    assert not result["totals"]["proposed"]["complete"]
    with pytest.raises(HTTPException) as error:
        approve(raw)
    assert error.value.status_code == 422


def test_marketing_reconciles_current_spend_across_cadences():
    raw = marketing_body()
    raw["adjustments"][0].update(cadence="annual", current_amount=12000, proposed_amount=24000)
    result = plan.evaluate(raw, context(), now=NOW)
    assert result["totals"]["proposed"]["annual_impact"] == 6000
    assert result["totals"]["proposed"]["complete"]


@pytest.mark.parametrize("other_kind", ["expense_increase", "revenue_change"])
def test_marketing_cannot_count_the_same_spend_or_aggregate_return_twice(other_kind):
    raw = marketing_body()
    other = action(id=str(uuid4()), kind=other_kind, driver_key="separate-display-name",
        current_amount=1000, proposed_amount=2000, contribution_margin_pct=50)
    if other_kind == "revenue_change":
        other["expense_row_id"] = None
    raw["adjustments"].append(other)
    result = plan.evaluate(raw, context(), now=NOW)
    assert not result["totals"]["proposed"]["complete"]
    assert all(row["issues"] for row in result["actions"])
    with pytest.raises(HTTPException):
        approve(raw)


def test_additive_guidance_fields_preserve_legacy_completion_and_empty_default_review_stamp():
    legacy = body()
    normalized = validate_body(legacy)
    for row in legacy["expenses"]:
        row.pop("source_group_key", None)
        row.pop("source_candidate_key", None)
    for row in legacy["adjustments"]:
        row.pop("product_name", None)
        row.pop("revenue_return_multiple", None)
    assert plan.proposal_stamp(legacy["adjustments"][0], legacy["expenses"][0], legacy) == plan.proposal_stamp(
        normalized["adjustments"][0], normalized["expenses"][0], normalized)
    assert completion(legacy)["complete"]
    normalized["category_confirmations"] = {"rent": "confirmed", "marketing": "needs_help", "cogs": "not_applicable"}
    assert validate_body(normalized)["category_confirmations"] == normalized["category_confirmations"]
    assert completion(normalized)["missing"] == ["category_confirmations.marketing"]
    approved = approve(normalized)
    approved["expenses"][0]["source_candidate_key"] = "different-source-group"
    assert plan.evaluate(approved, context(), now=NOW)["actions"][0]["review_state"] == "stale"


@pytest.mark.parametrize("confirmations", [{"invented": "confirmed"}, {"rent": "yes"}])
def test_category_confirmations_use_known_categories_and_explicit_decisions(confirmations):
    with pytest.raises(HTTPException) as error:
        body(category_confirmations=confirmations)
    assert error.value.status_code == 422


def test_not_applicable_category_cannot_hide_active_expenses_but_excluded_rows_are_allowed():
    with pytest.raises(HTTPException) as error:
        body(category_confirmations={"rent": "not_applicable"})
    assert error.value.status_code == 422
    raw = body(expenses=[expense(excluded=True)], category_confirmations={"rent": "not_applicable"})
    assert raw["category_confirmations"]["rent"] == "not_applicable"


def test_confirmed_category_needs_active_expense_detail_while_not_applicable_does_not():
    raw = body(category_confirmations={"marketing": "confirmed"})
    assert completion(raw)["missing"] == ["category_confirmations.marketing.expense_detail"]
    raw["category_confirmations"]["marketing"] = "not_applicable"
    assert completion(raw)["complete"]
    raw["category_confirmations"] = {"rent": "confirmed"}
    raw["expenses"][0]["excluded"] = True
    assert "category_confirmations.rent.expense_detail" in completion(raw)["missing"]


def test_noi_projection_is_independent_of_ebitda_and_includes_first_year_implementation():
    cockpit = context()
    cockpit["ebitda"] = {"headline": None}
    raw = body(action(effective_date="2026-07-01", implementation_cost=100))
    result = plan.evaluate(raw, cockpit, now=NOW)
    proposed = result["projection"]["proposed"]
    assert proposed["noi"] == -12600
    assert proposed["first_year_noi"] == round(-15000 + 2400 * 184 / 365 - 100, 2)
    assert proposed["noi_status"] == "unreviewed_illustration"
    assert proposed["earnings"] is proposed["first_year_earnings"] is proposed["dscr_current"] is None
    assert result["baseline"]["dscr"] == -0.83
    assert result["baseline"]["noi"] == -15000
    assert result["projection"]["approved"]["noi"] is None


def test_noi_source_identity_works_without_an_ebitda_source_or_evidence_business_label():
    cockpit = context()
    cockpit["ebitda"] = {"headline": None}
    cockpit["operating_evidence"].pop("business_name")
    result = plan.evaluate(body(), cockpit, now=NOW)
    assert result["projection"]["proposed"]["noi"] == -12600


@pytest.mark.parametrize("fault", ["missing", "stale", "period", "currency", "identity"])
def test_noi_only_illustration_does_not_bypass_source_comparability(fault):
    cockpit = context()
    cockpit["ebitda"] = {"headline": None}
    noi = cockpit["noi"]["headline"]
    if fault == "missing":
        noi["annualized_value"] = None
    elif fault == "stale":
        cockpit["noi"]["review"] = {"stale": True}
    elif fault == "period":
        noi["period"] = "2024"
    elif fault == "currency":
        noi["currency"] = "EUR"
    else:
        cockpit["operating_evidence"]["business_name"] = "Different Business Inc."
    projection = plan.evaluate(body(), cockpit, now=NOW)["projection"]["proposed"]
    assert projection["noi"] is projection["first_year_noi"] is None
    assert projection["noi_status"] == "unavailable"


@pytest.mark.parametrize("kind", ["price_change", "product_cost_change"])
def test_product_name_is_preserved_in_action_and_shareable_outputs(kind):
    raw = body(action(kind=kind, product_name="Example product", expense_row_id=None,
        units_per_period=100, current_unit_price=10, proposed_unit_price=12, current_unit_cost=4, proposed_unit_cost=3))
    result = plan.evaluate(raw, context(), now=NOW)
    assert result["actions"][0]["product_name"] == "Example product"
    assert plan.package_summary(result)["adjustments"][0]["product_name"] == "Example product"


def evidence_before_and_after_guidance():
    old = context()
    old["operating_evidence"]["expense_candidates"] = [{"key": "bank:rent", "current_amount": 1000,
        "category": "rent", "cadence": "monthly", "source_file_ids": [old["operating_evidence"]["files"][0]["file_id"]]}]
    current = deepcopy(old)
    evidence = current["operating_evidence"]
    evidence["guidance"] = {"expense_groups": [{"key": "source:example:rent", "category": "rent",
        "label": "Rent", "baseline_annual_amount": 12000}]}
    for row in evidence["files"]:
        row["eligible_for_guidance"] = True
    for row in evidence["expense_candidates"]:
        row.update(source_group_key="", source_candidate_key=row["key"])
    evidence["expense_candidates"].append({"key": "source:example:rent", "source_group_key": "source:example:rent",
        "source_candidate_key": "source:example:rent", "current_amount": 12000})
    return old, current


@pytest.mark.parametrize("receipt_version", ["v2", "pre_v2"])
def test_existing_operating_reviews_survive_only_additive_guidance_shape(receipt_version):
    old, current = evidence_before_and_after_guidance()
    raw = approve(body(), old)
    row = raw["adjustments"][0]
    if receipt_version == "v2":
        original_context = plan._context_values(old)
        for key in ("financing", "legacy_adjustments", "legacy_overrides"):
            original_context.pop(key)
        row["review_source_stamp"] = plan._stamp({"review_schema": "operating-evidence-v2",
            "proposal": plan.proposal_stamp(row, raw["expenses"][0], raw), "context": plan._stamp(original_context),
            "treatment": row["baseline_treatment"], "reconciliation": row["baseline_reconciliation_note"]})
    else:
        row["review_source_stamp"] = plan._legacy_review_stamp(row, old, raw["expenses"][0], raw)
    assert plan.evaluate(raw, current, now=NOW)["actions"][0]["review_state"] == "approved"
    for field in ("content_hash", "analysis_id", "association_stamp"):
        changed = deepcopy(current)
        changed["operating_evidence"]["files"][0][field] = "changed-original-source"
        assert plan.evaluate(raw, changed, now=NOW)["actions"][0]["review_state"] == "stale"


@pytest.mark.parametrize("start_with_guidance", [False, True])
def test_new_review_cannot_use_legacy_fallback_to_ignore_changed_guidance(start_with_guidance):
    old, current = evidence_before_and_after_guidance()
    raw = approve(body(), current if start_with_guidance else old)
    current["operating_evidence"]["guidance"]["expense_groups"][0]["baseline_annual_amount"] = 24000
    assert plan.evaluate(raw, current, now=NOW)["actions"][0]["review_state"] == "stale"


def test_expense_group_reconciliation_is_in_evaluation_and_shareable_summary_without_changing_noi():
    _, current = evidence_before_and_after_guidance()
    raw = body(expenses=[expense(source_group_key="source:example:rent")])
    result = plan.evaluate(raw, current, now=NOW)
    group = result["expense_groups"][0]
    assert group["status"] == "matches_source"
    assert group["disclosed_annual_amount"] == group["baseline_annual_amount"] == 12000
    assert result["baseline"]["noi"] == -15000
    assert plan.package_summary(result)["expense_groups"] == result["expense_groups"]
