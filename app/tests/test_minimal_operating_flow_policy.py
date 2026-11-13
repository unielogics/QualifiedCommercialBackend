"""Minimal source review and proposed changes retain their server boundaries."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.models.file_event import FileEvent
from app.models.financial_form_version import FinancialFormDraft
from app.schemas.earnings_improvement import (
    action_missing,
    completion,
    empty_body,
    key_facts,
    validate_body,
)
from app.services import earnings_improvement as engine
from app.services import financial_workspace as ws
from app.services import operating_plan_service as service
from app.services.operating_plan_guidance import client_evaluation
from app.services.operating_source_review import evaluate as source_review
from app.tests.test_earnings_improvement import (
    ACTOR,
    NOW,
    action,
    approve,
    body,
    context,
    expense,
    processing_action,
    processing_body,
    processing_context,
)
from app.tests.test_financial_workspace import Memory
from app.tests.test_financial_workspace import context as workspace_context
from app.tests.test_minimal_operating_flow_independent import ALLOCATION, scenario


def reviewed_source(raw, current):
    value = deepcopy(raw)
    review = source_review(value, current)
    value["source_group_confirmation_stamp"] = review["source_fingerprint"]
    value["source_group_confirmations"] = {row["key"]: "confirmed" for row in review["groups"]}
    return value


def completed_funding(raw, current):
    result = engine.evaluate(raw, current, now=NOW)
    cash = result["cash_bridge"]
    value = deepcopy(raw)
    value["released_cash_flow"] = {"source_fingerprint": cash["funding_source_fingerprint"], "allocations": [
        {"id": ALLOCATION, "label": "Retain cash", "purpose": "reserve", "amount_monthly": cash["funding_capacity_monthly"]}]}
    return validate_body(value)


def test_evidence_owner_and_dates_are_optional_why_and_known_setup_are_not():
    raw = body(action(effective_date=None, source_note="", owner_name="", source_file_ids=[]), projection_start=None)
    assert action_missing(raw["adjustments"][0]) == []
    assert completion(raw)["complete"]
    result = engine.evaluate(raw, context(), now=NOW, include_expense_changes=True)
    assert result["projection"]["proposed"]["earnings"] == -7600
    assert result["projection"]["proposed"]["first_year_earnings"] is None
    assert result["draft_feedback"]["actions"][0]["qualification_missing"] == []
    assert raw["adjustments"][0]["effective_date"] is None
    for key in ("explanation", "implementation_cost"):
        changed = deepcopy(raw)
        changed["adjustments"][0][key] = "" if key == "explanation" else None
        assert key in action_missing(changed["adjustments"][0])


def test_explicit_shared_plan_date_is_used_without_synthesizing_row_dates():
    raw = body(action(effective_date=None, source_note="", source_file_ids=[]))
    result = engine.evaluate(raw, context(), now=NOW)
    assert result["projection"]["proposed"]["first_year_earnings"] == -7600
    assert raw["adjustments"][0]["effective_date"] is None


@pytest.mark.parametrize("field", ["owner_name", "source_note", "explanation", "label", "effective_date"])
def test_annual_funding_ack_does_not_stale_for_descriptive_or_optional_timing_edits(field):
    raw, current = scenario()
    raw = completed_funding(raw, current)
    stamp = raw["released_cash_flow"]["source_fingerprint"]
    changed = deepcopy(raw)
    changed["adjustments"][0][field] = "2029-01-01" if field == "effective_date" else "Revised human explanation"
    value = engine.evaluate(changed, current, now=NOW)["cash_bridge"]
    assert value["funding_source_fingerprint"] == stamp
    assert not value["funding_stale"] and value["funding_complete"]


@pytest.mark.parametrize("field,value", [("command", {"target": "total_cost", "mode": "amount", "direction": "decrease", "value": 200, "allocation": "none"}),
                                          ("implementation_cost", 12)])
def test_real_funding_math_changes_require_explicit_reacknowledgement(field, value):
    raw, current = scenario()
    raw = completed_funding(raw, current)
    raw["adjustments"][0][field] = value
    if field == "implementation_cost":
        raw["adjustments"][0]["implementation_cost_cadence"] = "monthly"
    assert engine.evaluate(raw, current, now=NOW)["cash_bridge"]["funding_stale"]


@pytest.mark.parametrize("formatted", [1000, "1000.0", "1e3"])
def test_equal_numeric_formatting_preserves_funding_ack(formatted):
    raw, current = scenario()
    raw = completed_funding(raw, current)
    stamp = raw["released_cash_flow"]["source_fingerprint"]
    raw["expenses"][1]["current_amount"] = formatted
    raw["adjustments"][0]["current_amount"] = formatted
    result = engine.evaluate(validate_body(raw), current, now=NOW)["cash_bridge"]
    assert result["funding_source_fingerprint"] == stamp
    assert result["funding_complete"] and not result["funding_stale"]


def test_processor_zero_item_fees_need_no_count_but_positive_fee_does():
    raw = processing_body(processing_action(current_processing_rate_basis="processor_markup",
        proposed_processing_rate_basis="processor_markup", processing_transaction_count=None,
        current_processing_fixed_fee=0, proposed_processing_fixed_fee=0,
        current_processing_per_transaction_fee=0, proposed_processing_per_transaction_fee=0))
    row = raw["adjustments"][0]
    assert "processing_transaction_count" not in action_missing(row)
    result = engine.evaluate(raw, processing_context(), now=NOW, include_expense_changes=True)
    assert result["actions"][0]["gross_annual_impact"] == 9600
    row["proposed_processing_per_transaction_fee"] = .01
    assert "processing_transaction_count" in action_missing(row)
    assert f"adjustments.{row['id']}.processing_transaction_count" in engine.evaluate(
        raw, processing_context(), now=NOW, include_expense_changes=True)["draft_feedback"]["actions"][0]["math_missing"]


def test_reviewed_undated_run_rate_reaches_financing_without_invented_first_year():
    from app.services.financial_plan_operating import coverage_adjustment

    raw = body(action(effective_date=None, source_note="", owner_name=""), projection_start=None)
    current = context()
    reviewed = approve(raw, current)
    evaluated = engine.evaluate(reviewed, current, now=NOW)
    result = coverage_adjustment({"available": True, "state": "published", "evaluation": evaluated}, {},
        {"headline": {"key": "tax_return"}, "candidates": [{"key": "tax_return", "numerator": -10000, "currency_comparable": True}]})
    assert result["applied"] and result["applied_annual_impact"] == 2400
    assert result["timing_basis"] == "undated_run_rate" and result["first_year_impact"] is None
    assert reviewed["projection_start"] is None and reviewed["adjustments"][0]["effective_date"] is None


def test_source_only_confirm_does_not_create_empty_industry_expense_rows():
    raw, current = scenario()
    raw["expenses"], raw["adjustments"] = [], []
    current["plan"]["record"] = {}
    raw["released_cash_flow"] = {"source_fingerprint": None, "allocations": []}
    raw["category_confirmations"] = {"insurance": "needs_help", "utilities": "confirmed"}
    raw = reviewed_source(raw, current)
    assert source_review(raw, current)["complete"]
    assert completion(raw)["complete"]
    assert service.completion(raw, current)["complete"]
    assert raw["expenses"] == [] and not raw["no_operating_expenses_confirmed"]


def test_new_source_defaults_leave_legacy_key_facts_identical():
    raw = empty_body()
    facts = key_facts(raw)["operating_plan"]
    assert "source_group_confirmations" not in facts
    assert "source_group_confirmation_stamp" not in facts


@pytest.mark.parametrize("mutation", ["amount", "source_file", "accepted", "year"])
def test_source_confirmation_is_bound_to_saved_facts_not_just_category(mutation):
    raw, current = scenario()
    raw = reviewed_source(raw, current)
    changed = deepcopy(current)
    if mutation == "amount":
        changed["operating_evidence"]["guidance"]["expense_groups"][0]["baseline_annual_amount"] += 1
    elif mutation == "source_file":
        changed["operating_evidence"]["files"][0]["content_hash"] = "new-source-bytes"
    elif mutation == "accepted":
        changed["operating_evidence"]["files"][0]["eligible_for_review"] = False
    else:
        changed["operating_evidence"]["guidance"]["expense_groups"][0]["year"] = "2024"
    value = source_review(raw, changed)
    assert value["stale"] and not value["complete"]
    assert value["missing"] == ["source_group_confirmation_stamp"]


def test_unknown_group_cannot_confirm_other_sources_and_old_clients_cannot_unenroll_review():
    raw, current = scenario()
    raw = reviewed_source(raw, current)
    incoming = deepcopy(raw)
    incoming.pop("source_group_confirmation_stamp")
    incoming.pop("source_group_confirmations")
    assert service.protect_reviews(incoming, raw)["source_group_confirmations"] == raw["source_group_confirmations"]
    raw["source_group_confirmations"]["forged-source"] = "confirmed"
    assert not source_review(raw, current)["complete"]


def test_numeric_source_correction_is_audited_not_earned_cash_or_changed_irs_fact():
    raw, current = scenario()
    raw = reviewed_source(raw, current)
    original = deepcopy(current)
    changed = raw["expenses"][1]
    key = changed["source_group_key"]
    changed["current_amount"] = 900
    raw["source_group_confirmations"][key] = "needs_correction"
    raw["historical_corrections"] = [{"source_group_key": key, "reason": "Disclosed amount needs source review", "source_file_ids": [], "evidence_note": ""}]
    progress = source_review(raw, current)
    assert progress["complete"] and any(row["correction_pending"] for row in progress["groups"])
    result = engine.evaluate(raw, current, now=NOW, include_expense_changes=True)
    assert result["baseline"]["noi"] == original["noi"]["headline"]["annualized_value"]
    assert result["draft_feedback"]["summary"]["draft_noi"] is None
    assert result["cash_bridge"]["funding_capacity_monthly"] is None
    assert current == original
    assert result["preparer_handoff"]["historical"]["corrections_for_preparer_review"][0]["status"] == "unresolved_preparer_review"


def new_campaign(**changes):
    current = context()
    current["_operating_goal"] = {"status": "set", "amount": 18000, "currency": "USD", "revision": 1}
    row = expense(category="marketing", current_amount=0, source_file_ids=[])
    planned = action(kind="marketing_investment", product_name="New campaign", current_amount=0, proposed_amount=None,
        source_file_ids=[], source_note="", owner_name="", effective_date=None, revenue_return_multiple="2.5", contribution_margin_pct=60,
        **changes)
    raw = body(planned, expenses=[row], projection_start=None)
    return raw, current


def review_new(raw, current):
    return engine.review_actions(raw, current, [row["id"] for row in raw["adjustments"]], "approved", "Reviewed the conditional new activity", ACTOR,
        baseline_treatment="incremental_to_baseline", baseline_reconciliation_note="New activity starts from a disclosed zero; source supports the business baseline only", now=NOW)


def test_marketing_filled_zero_and_decimal_do_not_get_generic_missing_buttons():
    raw, current = new_campaign()
    row = engine.evaluate(raw, current, now=NOW, include_expense_changes=True)["draft_feedback"]["actions"][0]
    assert row["math_missing"] == [f"adjustments.{raw['adjustments'][0]['id']}.proposed_amount"]
    assert row["qualification_missing"] == [] and row["source_issues"] == []
    raw["adjustments"][0]["proposed_amount"] = 0
    row = engine.evaluate(raw, current, now=NOW, include_expense_changes=True)["draft_feedback"]["actions"][0]
    assert row["math_missing"] == [] and row["cash_net_annual_delta"] == 0


def test_new_zero_assumption_review_uses_accepted_baseline_context_not_an_invented_growth_quote():
    raw, current = new_campaign()
    raw["adjustments"][0]["proposed_amount"] = 1000
    reviewed = review_new(raw, current)
    row = engine.evaluate(reviewed, current, now=NOW)["actions"][0]
    assert row["included_in_approved_projection"] and row["calculation"]["baseline_context_only"]
    assert row["calculation"]["baseline_classification"] == "new_planning_assumption"
    assert reviewed["adjustments"][0]["source_file_ids"] == []


@pytest.mark.parametrize("mutation", ["nonzero", "source_claim", "unaccepted", "other_business", "other_period"])
def test_new_activity_exception_cannot_launder_unknown_existing_costs_or_bad_baseline(mutation):
    raw, current = new_campaign()
    raw["adjustments"][0]["proposed_amount"] = 1000
    if mutation == "nonzero":
        raw["expenses"][0]["current_amount"] = raw["adjustments"][0]["current_amount"] = 100
    elif mutation == "source_claim":
        raw["expenses"][0]["source_group_key"] = "forged-existing-source"
    elif mutation == "unaccepted":
        current["operating_evidence"]["files"][0]["eligible_for_review"] = False
    elif mutation == "other_business":
        raw["business_name"] = "Unrelated company"
    else:
        raw["period_start"], raw["period_end"] = "2024-01-01", "2024-12-31"
    with pytest.raises(HTTPException):
        review_new(raw, current)


@pytest.mark.asyncio
@pytest.mark.parametrize("client", [False, True])
@pytest.mark.parametrize("retain_default", [False, True])
async def test_actual_draft_save_version_reload_accepts_composite_funding_without_false_loan_stale(client, retain_default):
    raw, current = scenario()
    raw["projection_start"] = None
    raw["adjustments"][0].update(effective_date=None, source_note="", owner_name="")
    raw = completed_funding(reviewed_source(raw, current), current)
    if retain_default:
        raw["released_cash_flow"] = {"source_fingerprint": None, "allocations": []}
    assert service.completion(raw, current)["complete"]
    db = Memory()
    profile, family, draft = workspace_context("operating_plan")
    draft.body = deepcopy(raw)
    db.objects[(FinancialFormDraft, family.id)] = draft
    db.add(FileEvent(profile_id=profile.id, kind="operating_plan.goal_set", meta={"amount": "18000", "currency": "USD", "revision": 1}))
    actor = SimpleNamespace(id=None if client else uuid4(), name="Synthetic client" if client else "Staff", role=Role.CLIENT if client else Role.LOAN_EXEC)
    sources = [SimpleNamespace(id=row["file_id"]) for row in current["operating_evidence"]["files"]]
    slot = SimpleNamespace(id=uuid4(), status="requested")
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "load_workspace", AsyncMock(return_value={})), \
         patch.object(ws, "_render", return_value=b"%PDF synthetic"), patch.object(ws, "_write_pdf"), \
         patch.object(ws, "source_files_for_profile", AsyncMock(return_value=sources)), patch.object(ws, "filter_current_files", AsyncMock(return_value=sources)), \
         patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=current)), \
         patch("app.routers.buckets._bucket_storage_config", return_value=("bucket", "prefix", None)), \
         patch("app.routers.application_profiles._requested_slot", AsyncMock(return_value=slot)), \
         patch("app.services.financial_publication.after_publication", AsyncMock()), patch.object(ws, "_project", AsyncMock()):
        await ws.stage_draft(db, profile, family, draft, raw, actor=actor)
        result = await ws.publish_version(db, profile, kind="operating_plan", expected_revision=draft.revision, idempotency_key="composite-save", actor=actor, submit=client)
        saved = db.versions[0]
        reloaded = engine.evaluate(saved.body, current, now=NOW, include_expense_changes=True)
    assert result["saved_version"]["id"] == str(saved.id)
    assert reloaded["cash_bridge"]["funding_complete"] and not reloaded["cash_bridge"]["funding_stale"]
    assert reloaded["released_cash_flow"]["complete"] and reloaded["released_cash_flow"]["status"] == "complete"
    assert reloaded["projection"]["proposed"]["first_year_noi"] is None
    assert saved.body["released_cash_flow"] == raw["released_cash_flow"]
    assert saved.body["expenses"] == raw["expenses"]
    assert saved.status == ("client_submitted" if client else "published")
    assert client_evaluation(reloaded)["cash_bridge"]["funding_capacity_monthly"] == 3600
    if retain_default:
        cash = reloaded["cash_bridge"]
        assert cash["funding_retained_monthly"] == 3600 and cash["funding_retained_annual"] == 43200
        assert cash["funding_reinvested_monthly"] == 0 and not cash["funding_required"]
        assert cash["funding_missing"] == [] and cash["allocations"] == []
        assert saved.body["released_cash_flow"]["source_fingerprint"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("client", [False, True])
async def test_partial_why_is_retained_in_draft_but_required_for_final_version(client):
    raw, current = scenario()
    raw["adjustments"][0].update(explanation="", effective_date=None, source_note="", owner_name="")
    raw["projection_start"] = None
    db = Memory()
    profile, family, draft = workspace_context("operating_plan")
    draft.body = deepcopy(raw)
    db.objects[(FinancialFormDraft, family.id)] = draft
    db.add(FileEvent(profile_id=profile.id, kind="operating_plan.goal_set", meta={"amount": "18000", "currency": "USD", "revision": 1}))
    actor = SimpleNamespace(id=None if client else uuid4(), role=Role.CLIENT if client else Role.LOAN_EXEC)
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), \
         patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=current)), \
         patch.object(ws, "_render") as render:
        await ws.stage_draft(db, profile, family, draft, raw, actor=actor)
        assert draft.body["adjustments"][0]["explanation"] == ""
        with pytest.raises(HTTPException) as caught:
            await ws.publish_version(db, profile, kind="operating_plan", expected_revision=draft.revision,
                idempotency_key="why-required", actor=actor, submit=client)
    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "operating_change_reason_required"
    assert caught.value.detail["missing"] == [f"adjustments.{raw['adjustments'][0]['id']}.explanation"]
    assert draft.body["adjustments"][0]["effective_date"] is None
    render.assert_not_called()
