"""Live arithmetic is not a financial-source replacement or staff approval."""
from copy import deepcopy
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.schemas.earnings_improvement import validate_body
from app.services import earnings_improvement as engine
from app.services.operating_plan_guidance import client_evaluation
from app.tests.test_operating_step3_independent import NOW, body, source_context
from app.tests.test_recognizable_plan_contract import command, price_plan


def draft(*, group=False):
    raw = body()
    raw["expenses"] = raw["expenses"][:1]
    raw["adjustments"] = raw["adjustments"][:1]
    row = raw["adjustments"][0]
    row.update(command=command(value=10), effective_date=None, explanation="", source_note="", implementation_cost=None)
    if group:
        row.update(kind="expense_group_change", expense_row_id=None, expense_category="rent",
                   source_group_key=raw["expenses"][0]["source_group_key"])
        row["command"]["allocation"] = "proportional"
    return validate_body(raw)


def preview(raw, cockpit=None):
    current = cockpit or source_context()
    current["_operating_goal"] = {"status": "set", "amount": 18000, "currency": "USD", "revision": 1}
    return engine.evaluate(raw, current, now=NOW, include_expense_changes=True)


@pytest.mark.parametrize("group", [False, True])
def test_same_item_group_command_has_live_gross_before_notes_dates_and_unknown_setup(group):
    raw = draft(group=group)
    original = deepcopy(raw)
    result = preview(raw)
    value = result["draft_feedback"]
    action, total = value["actions"][0], value["summary"]
    assert action["math_status"] == "calculated"
    assert action["gross_annual_delta"] == 1200 and action["gross_monthly_run_rate_delta"] == 100
    assert action["net_annual_delta"] is None and action["first_year_delta"] is None
    assert action["source_issues"] == [] and action["math_missing"] == []
    assert f"adjustments.{raw['adjustments'][0]['id']}.implementation_cost" in action["qualification_missing"]
    assert total["complete"] and total["draft_noi"] == -13800 and total["draft_noi_delta"] == 1200
    assert total["draft_gap"] == 31800
    assert total["qualified_noi_delta"] is None
    assert result["projection"]["proposed"]["noi"] is None and result["actions"][0]["gross_annual_impact"] is None
    assert raw == original


def test_unknown_unrelated_disclosure_does_not_erase_an_accounted_source_cost_delta():
    raw = draft()
    raw["expenses"].append({**raw["expenses"][0], "id": str(uuid4()), "label": "Unknown independent cover",
        "category": "insurance", "source_group_key": "", "source_candidate_key": "", "current_amount": None})
    result = preview(raw)
    assert not result["disclosure"]["complete"]
    assert result["draft_feedback"]["summary"]["complete"]
    assert result["draft_feedback"]["summary"]["draft_noi"] == -13800


@pytest.mark.parametrize("change", ["period", "basis", "currency", "entity", "source_amount", "source_missing", "source_unaccepted"])
def test_source_scope_errors_retain_source_headline_and_never_become_draft_or_qualified_noi(change):
    raw, current = draft(), source_context()
    raw["adjustments"][0].update(implementation_cost=0, effective_date="2026-01-01", explanation="Explicit proposal", source_note="Synthetic disclosed terms")
    if change == "period":
        raw.update(period_start="2024-01-01", period_end="2024-12-31")
    elif change == "basis":
        raw["basis"] = "accrual"
    elif change == "currency":
        raw["currency"] = "CAD"
    elif change == "entity":
        raw["business_name"] = "Different Company"
    elif change == "source_amount":
        raw["expenses"][0]["current_amount"] = 10000
    elif change == "source_missing":
        current["operating_evidence"]["guidance"]["expense_groups"] = []
    else:
        current["operating_evidence"]["files"][0]["eligible_for_review"] = False
    result = preview(raw, current)
    total = result["draft_feedback"]["summary"]
    assert result["baseline"]["noi"] == -15000
    assert total["draft_noi"] is None and total["draft_noi_delta"] is None and total["draft_gap"] is None
    assert total["qualified_noi_delta"] is None and total["qualified_first_year_noi_delta"] is None


@pytest.mark.parametrize("change", ["cleared", "overlap", "group_leaf_unknown"])
def test_unknown_affected_values_and_overlapping_actions_never_get_a_complete_goal_point(change):
    raw = draft(group=change == "group_leaf_unknown")
    if change == "cleared":
        raw["adjustments"][0]["command"]["value"] = None
    elif change == "overlap":
        raw["adjustments"].append({**raw["adjustments"][0], "id": str(uuid4())})
    else:
        raw["expenses"].append({**raw["expenses"][0], "id": str(uuid4()), "current_amount": None})
    value = preview(raw)["draft_feedback"]
    assert not value["summary"]["complete"] and value["summary"]["draft_noi"] is None
    assert value["summary"]["gross_annual_delta"] is None
    assert value["summary"]["missing"]


def test_missing_current_amount_is_a_money_input_not_a_source_verification_label():
    raw = draft()
    raw["expenses"][0]["current_amount"] = None
    action = preview(raw)["draft_feedback"]["actions"][0]
    assert action["math_missing"] and action["gross_annual_delta"] is None
    assert not any("Missing current" in issue for issue in action["source_issues"])


def test_net_runrate_and_firstyear_timing_require_real_inputs_and_never_use_the_clock_fallback():
    raw = draft()
    raw["adjustments"][0].update(implementation_cost=600, implementation_cost_cadence="one_time", effective_date="2026-07-01")
    result = preview(raw)["draft_feedback"]["actions"][0]
    assert result["net_annual_delta"] == 1200
    assert result["first_year_delta"] == pytest.approx(1200 * 184 / 365 - 600, abs=.01)
    raw["projection_start"] = None
    value = preview(raw)["draft_feedback"]
    assert value["actions"][0]["first_year_delta"] is None
    assert value["summary"]["qualified_first_year_noi_delta"] is None
    assert "projection_start" not in value["actions"][0]["qualification_missing"]


def test_annual_price_and_cost_compose_from_canonical_math_without_qualified_metadata():
    raw = price_plan()
    price = raw["adjustments"][0]
    price.update(effective_date=None, implementation_cost=None, explanation="", source_note="")
    rent = draft()
    raw["expenses"] = rent["expenses"]
    raw["adjustments"].append(rent["adjustments"][0])
    value = preview(raw)["draft_feedback"]
    assert value["summary"]["gross_annual_delta"] == 11200
    assert value["summary"]["draft_noi"] == -3800
    assert all(row["net_annual_delta"] is None for row in value["actions"])


def test_same_basis_dscr_deltas_retain_small_ratio_precision_and_do_not_subtract_the_headline():
    raw, current = draft(), source_context()
    raw["adjustments"][0]["command"] = command(mode="amount", value=1)
    current["dscr"]["headline"]["value"] = 99
    total = preview(raw, current)["draft_feedback"]["summary"]
    assert total["dscr_current_debt_before"] == -.8333
    assert total["dscr_current_debt_after"] == -.8323
    assert total["dscr_current_debt_delta"] == .001


def test_qualified_setup_and_explicit_firstyear_ratios_use_a_coherent_earnings_basis():
    raw = draft()
    raw["adjustments"][0].update(implementation_cost=50, implementation_cost_cadence="monthly",
        effective_date="2026-07-01", explanation="Documented change", source_note="Synthetic source terms")
    value = preview(raw)["draft_feedback"]["summary"]
    assert value["dscr_current_debt_delta"] == .1  # Gross1200 /12000.
    assert value["qualified_noi_delta"] == 600
    assert value["qualified_dscr_current_debt_delta"] == .05  # Net600 /12000.
    assert value["qualified_first_year_dscr_current_debt_delta"] == pytest.approx((600 * 184 / 365) / 12000, abs=.0001)
    raw["projection_start"] = None
    value = preview(raw)["draft_feedback"]["summary"]
    assert value["qualified_first_year_dscr_current_debt_delta"] is None


def test_unknown_noi_does_not_hide_money_math_or_claim_a_complete_noi_and_zero_debt_is_not_missing():
    raw, current = draft(), source_context()
    current["noi"]["headline"]["annualized_value"] = None
    current["debt"]["service"]["annual"] = 0
    value = preview(raw, current)["draft_feedback"]["summary"]
    assert value["complete"] and value["gross_annual_delta"] == 1200
    assert value["status"] == "source_blocked" and value["draft_noi"] is None
    assert value["dscr_current_debt_before"] is None and "annual_debt_service" not in value["missing"]


@pytest.mark.parametrize("currency", [None, "CAD"])
def test_unconfirmed_or_other_currency_debt_does_not_create_a_cross_currency_dscr_delta(currency):
    raw, current = draft(), source_context()
    current["debt"]["service"]["currency"] = currency
    value = preview(raw, current)["draft_feedback"]["summary"]
    assert value["draft_noi_delta"] == 1200
    assert value["dscr_current_debt_before"] is None and value["dscr_current_debt_after"] is None
    assert value["qualified_dscr_current_debt_after"] is None
    assert "debt_service_currency" in value["missing"]


def test_one_time_effect_is_separate_from_recurring_goal_and_requires_explicit_firstyear_window():
    from app.tests.test_earnings_improvement import action
    from app.tests.test_earnings_improvement import body as standard_body

    raw = standard_body(action(kind="one_time_adjustment", expense_row_id=None, cadence="one_time",
        current_amount=1000, proposed_amount=0, implementation_cost=100, effective_date="2026-01-01"))
    value = preview(raw)["draft_feedback"]
    assert value["actions"][0]["gross_one_time_delta"] == 1000
    assert value["summary"]["gross_annual_delta"] == 0
    assert value["summary"]["draft_noi_delta"] == 0
    assert value["actions"][0]["first_year_delta"] == 900
    raw["projection_start"] = None
    assert preview(raw)["draft_feedback"]["actions"][0]["first_year_delta"] is None


def test_no_changes_real_zero_stays_distinct_from_unavailable_source():
    raw = draft()
    raw["adjustments"] = []
    value = preview(raw)["draft_feedback"]["summary"]
    assert value["status"] == "no_changes" and value["complete"]
    assert value["gross_annual_delta"] == 0 and value["draft_noi"] == -15000
    raw["basis"] = "accrual"
    value = preview(raw)["draft_feedback"]["summary"]
    assert value["gross_annual_delta"] == 0 and value["draft_noi"] is None and not value["source_comparable"]


def test_client_sidecar_allowlist_omits_receipts_notes_hashes_and_unknown_keys():
    result = preview(draft())
    result["draft_feedback"]["actions"][0].update(review_reason="PRIVATE review", actor_id="PRIVATE actor", source_hash="PRIVATE hash")
    result["draft_feedback"]["summary"]["private_offer"] = "PRIVATE quote"
    safe = client_evaluation(result)["draft_feedback"]
    assert "PRIVATE" not in str(safe)
    assert safe["summary"]["draft_noi_delta"] == 1200


def test_sidecar_is_opt_in_and_never_changes_legacy_actions_projection_facts_or_package():
    raw, current = draft(), source_context()
    baseline = engine.evaluate(raw, current, now=NOW)
    previewed = engine.evaluate(raw, current, now=NOW, include_expense_changes=True)
    assert "draft_feedback" not in baseline
    for key in baseline:
        assert previewed[key] == baseline[key]
    assert engine.package_summary(previewed) == engine.package_summary(baseline)


def test_fresh_computed_review_can_reconcile_an_unscoped_manual_cost_without_inventing_a_group():
    from app.tests.test_earnings_improvement import approve
    from app.tests.test_earnings_improvement import body as standard_body

    raw = standard_body()
    current = source_context()
    reviewed = approve(raw, current)
    value = preview(reviewed, current)
    assert value["actions"][0]["included_in_approved_projection"]
    assert value["draft_feedback"]["actions"][0]["source_issues"] == []
    assert value["draft_feedback"]["summary"]["draft_noi_delta"] == 2400
    assert value["draft_feedback"]["summary"]["qualified_noi_delta"] == 2400
    assert reviewed["expenses"][0]["source_group_key"] == ""


@pytest.mark.parametrize("failure", ["raw_status", "changed_row", "changed_context", "missing_reconciliation", "unaccepted_source", "populated_missing_group"])
def test_manual_cost_fallback_never_uses_forged_stale_or_unreconciled_review(failure):
    from app.tests.test_earnings_improvement import approve
    from app.tests.test_earnings_improvement import body as standard_body

    current = source_context()
    reviewed = approve(standard_body(), current)
    if failure == "raw_status":
        reviewed["adjustments"][0]["review_source_stamp"] = None
    elif failure == "changed_row":
        reviewed["expenses"][0]["current_amount"] = 1100
    elif failure == "changed_context":
        current["noi"]["headline"]["annualized_value"] -= 1
    elif failure == "missing_reconciliation":
        reviewed["adjustments"][0]["baseline_reconciliation_note"] = ""
    elif failure == "unaccepted_source":
        current["operating_evidence"]["files"][0]["eligible_for_review"] = False
    else:
        reviewed["expenses"][0]["source_group_key"] = "missing-current-source"
    value = preview(reviewed, current)["draft_feedback"]["summary"]
    assert value["draft_noi"] is None and value["qualified_noi_delta"] is None


@pytest.mark.parametrize("source", ["missing", "different_amount"])
def test_even_fresh_legacy_approval_cannot_bypass_a_populated_unreconciled_source_group(source):
    from app.tests.test_earnings_improvement import approve
    from app.tests.test_earnings_improvement import body as standard_body

    current, raw = source_context(), standard_body()
    raw["expenses"][0]["source_group_key"] = "removed-source" if source == "missing" else "irs:2025:rents"
    if source == "different_amount":
        current["operating_evidence"]["guidance"]["expense_groups"][0]["baseline_annual_amount"] = 20000
    reviewed = approve(raw, current)
    result = preview(reviewed, current)
    assert result["actions"][0]["included_in_approved_projection"]  # Existing reviewed authority is unchanged.
    assert result["projection"]["proposed"]["noi"] == -12600
    assert result["draft_feedback"]["summary"]["draft_noi"] is None
    assert result["draft_feedback"]["summary"]["qualified_noi_delta"] is None


@pytest.mark.asyncio
async def test_actual_public_preview_copies_only_safe_sidecar_and_cannot_forge_staff_approval():
    from app.services import operating_plan_service
    from app.tests.test_operating_client_preview import ReadOnlyMemory

    db = ReadOnlyMemory()
    raw = draft()
    raw["adjustments"][0].update(status="approved", review_reason="FORGED", reviewed_by_user_id=str(uuid4()))
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=source_context())):
        result = await operating_plan_service.client_preview(db, db.profile, raw, current=draft())
    assert result["evaluation"]["draft_feedback"]["summary"]["draft_noi_delta"] == 1200
    assert "FORGED" not in str(result)
    db.commit.assert_not_awaited()
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("public", [False, True])
async def test_real_http_evaluate_roundtrip_exposes_nullable_canonical_delta_and_rejects_posted_computed_values(public):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import get_current_user
    from app.enums import Role
    from app.routers import application_profiles, operating_plan
    from app.services import financial_workspace
    from app.tests.test_operating_client_preview import TOKEN, ReadOnlyMemory

    db, app = ReadOnlyMemory(), FastAPI()
    db.draft.body = draft(group=True)
    before = deepcopy(db.draft.body)
    routes = application_profiles if public else operating_plan
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    path = f"/application-profiles/public/financial-forms/{TOKEN}/evaluate" if public else f"/application-profiles/{db.profile.id}/operating-plan/evaluate"
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=source_context())), \
         patch.object(operating_plan.profiles, "load_profile", AsyncMock(return_value=db.profile)), \
         patch.object(financial_workspace, "existing_family", AsyncMock(return_value=db.family)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(path, json={"body": before})
            assert response.status_code == 200, response.text
            value = response.json()["evaluation"]["draft_feedback"]
            assert value["summary"]["draft_noi_delta"] == 1200
            assert value["actions"][0]["net_annual_delta"] is None
            assert "review_reason" not in str(value) and "source_hash" not in str(value)
            forged = {**before, "draft_feedback": {"summary": {"draft_noi": 9999999}}}
            assert (await client.post(path, json={"body": forged})).status_code == 422
    db.assert_unchanged(before)
