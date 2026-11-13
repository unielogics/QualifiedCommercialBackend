"""Readmodel currency authority, metric separation and unchanged legacy facts."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services import earnings_improvement as engine
from app.services.financial_cockpit import accepted_schedule_currency, build_cockpit
from app.services.operating_plan_guidance import client_evaluation
from app.services.released_cash_flow import source
from app.tests.test_earnings_improvement import NOW, SOURCE, action, approve, body, expense
from app.tests.test_live_plan_feedback_independent import source_context
from app.tests.test_plan_goals_independent import saved_loan

KEY = "source:synthetic:da"


def da_context():
    current = source_context([(KEY, "depreciation_amortization", 12000)])
    current["noi"]["definition"] = {"includes_depreciation_and_amortization": True}
    current["noi"]["headline"].update(kind="tax_derived_noi", annualized_value=18000)
    current["ebitda"]["headline"].update(annualized_value=30000)
    loan = saved_loan()
    current["plan"], current["debt"] = loan["plan"], loan["debt"]
    return current


def da_body(**patch):
    return body(action(**{"current_amount": 1000, "proposed_amount": 500, **patch}),
                expenses=[expense(category="depreciation_amortization", source_group_key=KEY)])


def evaluate(raw=None, current=None):
    return engine.evaluate(raw or da_body(), current or da_context(), now=NOW, include_expense_changes=True)


def accepted(**patch):
    return {"source": "form", "status": "filed", "version_id": str(uuid4()),
            "body": {"schema_version": "qc_ds.v2", "business_name": "Example LLC", "debts": [
                {"id": str(uuid4()), "lender": "Synthetic bank", "balance": 50000,
                 "monthly_payment": 1000, "status": "active", "count_in_dscr": True}]}, **patch}


@pytest.mark.parametrize("status", ["published", "filed"])
def test_registered_first_party_usd_schema_survives_real_cockpit_read_without_record_edits(status):
    version = accepted(status=status)
    original = deepcopy(version)
    result = build_cockpit([], [], accepted_schedule=version)
    service = result["debt"]["service"]
    assert service["currency"] == "USD" and service["currency_basis"] == "first_party_qc_ds_v2_usd"
    assert (service["monthly"], service["annual"]) == (1000, 12000)
    assert version == original


@pytest.mark.parametrize("patch", [
    {"source": "original"}, {"source": "legacy_baseline"}, {"source": "form", "status": "draft"},
    {"body": {"schema_version": "unknown", "debts": []}}, {"body": {}},
])
def test_version_schema_or_accepted_status_alone_never_invents_currency(patch):
    result = accepted_schedule_currency(accepted(schema_version="qc_ds.v2", **patch))
    assert result["currency"] is None and result["currency_issue"]


@pytest.mark.parametrize("location", ["body", "key_facts", "row"])
def test_explicit_non_usd_is_retained_without_conversion(location):
    value = accepted()
    if location == "row":
        value["body"]["debts"][0]["currency"] = "CAD"
    else:
        value.setdefault(location, {})["currency"] = "CAD"
    result = accepted_schedule_currency(value)
    assert result["currency"] == "CAD" and result["currency_basis"] == "explicit_accepted_schedule"


@pytest.mark.parametrize("stated", ["EUR", "not a currency"])
def test_explicit_conflict_cannot_be_overridden_by_native_schema(stated):
    value = accepted(key_facts={"currency": stated})
    value["body"]["currency"] = "USD"
    result = accepted_schedule_currency(value)
    assert result["currency"] is None and result["currency_basis"] == "conflicting_accepted_schedule"


def test_real_d_and_a_math_has_separate_noi_earnings_cash_and_dscr_results():
    raw, current = da_body(), da_context()
    original = deepcopy((raw, current))
    result = evaluate(raw, current)
    assert result["projection"]["proposed"]["noi"] == 24000
    assert result["projection"]["proposed"]["earnings"] == 30000
    feedback = result["draft_feedback"]
    row, summary = feedback["actions"][0], feedback["summary"]
    assert (row["noi_gross_annual_delta"], row["earnings_gross_annual_delta"], row["cash_gross_annual_delta"]) == (6000, 0, 0)
    assert (summary["draft_noi_delta"], summary["gross_earnings_annual_delta"], summary["gross_cash_annual_delta"]) == (6000, 0, 0)
    assert summary["dscr_current_debt_delta"] == 0
    assert result["cash_bridge"]["modeled_operations_annual"] == 0
    assert (raw, current) == original


def test_cleared_setup_has_known_accounting_gross_but_no_invented_net_or_first_year():
    result = evaluate(da_body(implementation_cost=None, effective_date=None))
    row = result["draft_feedback"]["actions"][0]
    assert row["noi_gross_annual_delta"] == 6000 and row["cash_gross_annual_delta"] == 0
    for metric in ("noi", "earnings", "cash"):
        assert row[metric + "_net_annual_delta"] is None and row[metric + "_first_year_delta"] is None
    assert result["cash_bridge"]["net_loan_cash_change_annual"] == 42000


@pytest.mark.parametrize("mutation", ["definition", "kind", "other_file", "group_amount", "period"])
def test_unknown_or_unreconciled_noi_basis_does_not_erase_independent_cash_zero(mutation):
    current = da_context()
    if mutation == "definition":
        current["noi"]["definition"] = {}
    elif mutation == "kind":
        current["noi"]["headline"]["kind"] = "staff_reviewed_noi"
    elif mutation == "other_file":
        current["noi"]["headline"]["source_refs"] = [{"file_id": str(uuid4())}]
    elif mutation == "group_amount":
        current["operating_evidence"]["guidance"]["expense_groups"][0]["baseline_annual_amount"] = 10000
    else:
        current["operating_evidence"]["guidance"]["expense_groups"][0]["period_start"] = "2024-01-01"
    result = evaluate(current=current)
    assert result["projection"]["proposed"]["noi"] is None
    assert result["projection"]["proposed"]["earnings"] == 30000
    assert result["actions"][0]["cash_net_annual_impact"] == 0
    assert result["draft_feedback"]["summary"]["draft_noi"] is None


def test_unknown_earnings_basis_can_keep_known_noi_without_inventing_a_coverage_effect():
    current = da_context()
    current["ebitda"]["headline"]["kind"] = "custom_operating_measure"
    result = evaluate(current=current)
    assert result["projection"]["proposed"]["noi"] == 24000
    assert result["projection"]["proposed"]["earnings"] is None
    assert result["draft_feedback"]["summary"]["draft_noi"] == 24000
    assert result["draft_feedback"]["summary"]["dscr_current_debt_after"] is None
    assert result["cash_bridge"]["modeled_operations_annual"] == 0


def test_noncash_review_binds_the_metric_definition_and_cannot_use_legacy_fallback():
    current = da_context()
    reviewed = approve(da_body(), current)
    assert evaluate(reviewed, current)["actions"][0]["included_in_approved_projection"]
    current["noi"]["definition"]["includes_depreciation_and_amortization"] = False
    result = evaluate(reviewed, current)
    assert result["actions"][0]["review_state"] == "stale"
    assert not result["actions"][0]["included_in_approved_projection"]


def test_public_preview_allows_only_safe_metric_values_and_preserves_explicit_nulls():
    result = evaluate()
    result["draft_feedback"]["actions"][0]["private_review_reason"] = "PRIVATE"
    safe = client_evaluation(result)["draft_feedback"]
    assert safe["actions"][0]["cash_net_annual_delta"] == 0
    assert "PRIVATE" not in str(safe) and "private_review_reason" not in safe["actions"][0]


def test_stale_allocation_never_hides_known_loan_run_rate_or_silently_reacknowledges():
    raw, current = da_body(proposed_amount=None), da_context()
    raw["released_cash_flow"] = {"source_fingerprint": "f" * 64, "allocations": []}
    result = evaluate(raw, current)["cash_bridge"]
    assert result["stale"] and result["loan_status"] == "calculated"
    assert (result["before_debt_service_annual"], result["after_debt_service_annual"], result["gross_loan_relief_annual"], result["net_loan_cash_change_annual"]) == (120000, 72000, 48000, 42000)
    assert result["modeled_operations_annual"] is None and result["combined_incremental_annual"] is None
    assert raw["released_cash_flow"]["source_fingerprint"] == "f" * 64


@pytest.mark.parametrize("issue", ["principal", "override", "unknown_charge", "currency"])
def test_source_blockers_keep_observed_numbers_but_never_mint_usable_capacity(issue):
    current = da_context()
    if issue == "principal":
        current["plan"]["evaluation"]["principal_solution"] = {"status": "calculated", "requires_apply": True}
    elif issue == "override":
        current["plan"]["record"]["overrides"] = {"debt_service_monthly": 100}
    elif issue == "unknown_charge":
        current["plan"]["evaluation"]["annual_new_operating_expenses"] = None
    else:
        current["debt"]["service"]["currency"] = None
    value = evaluate(current=current)["cash_bridge"]
    assert value["loan_status"] in {"incomplete", "source_blocked"} and value["loan_missing"] and value["loan_fix_fields"]
    assert value["before_debt_service_annual"] == 120000 and value["gross_loan_relief_annual"] == 48000
    assert value["net_loan_cash_change_annual"] is None and value["recurring_cash_released_annual"] is None


def test_funding_explicit_growth_spend_does_not_deduct_its_funding_twice():
    current = da_context()
    raw = body(action(kind="marketing_investment", product_name="Synthetic campaign", current_amount=0, proposed_amount=1000,
                      revenue_return_multiple=2.5, contribution_margin_pct=60),
               expenses=[expense(category="marketing", current_amount=0)])
    raw["released_cash_flow"] = {"source_fingerprint": source(current)["fingerprint"], "allocations": [
        {"id": str(uuid4()), "label": "Retain cash", "purpose": "reserve", "amount_monthly": 2500},
        {"id": str(uuid4()), "label": "Conditional marketing", "purpose": "planned_increment", "amount_monthly": 1000,
         "expense_row_id": raw["expenses"][0]["id"], "action_id": raw["adjustments"][0]["id"]}]}
    value = evaluate(raw, current)["cash_bridge"]
    assert value["complete"] and (value["modeled_operations_monthly"], value["modeled_operations_annual"]) == (500, 6000)
    assert (value["combined_incremental_monthly"], value["combined_incremental_annual"]) == (4000, 48000)
    assert value["allocated_annual"] == 42000 and value["unallocated_annual"] == 0
    assert value["combined_incremental_first_year"] is None


def test_preview_only_additions_never_enter_ordinary_saved_summary_or_source_stamps():
    raw = body()
    current = source_context()
    before = engine.evaluate(raw, current, now=NOW)
    preview = engine.evaluate(raw, current, now=NOW, include_expense_changes=True)
    assert engine.package_summary(before) == engine.package_summary(preview)
    assert engine.context_stamp(current) == before["context_stamp"] == preview["context_stamp"]
    assert "draft_feedback" not in engine.package_summary(preview)
    assert "loan_status" not in engine.package_summary(preview)["cash_bridge"]
    assert SOURCE in str(raw)


@pytest.mark.parametrize("currency", ["CAD", "EUR"])
def test_known_foreign_debt_never_creates_cross_currency_full_or_preview_coverage(currency):
    current = da_context()
    current["debt"]["service"]["currency"] = currency
    result = evaluate(current=current)
    assert result["projection"]["proposed"]["noi"] == 24000
    assert result["projection"]["proposed"]["earnings"] == 30000
    assert result["baseline"]["dscr"] is None and result["baseline"]["dscr_unavailable_reason"]
    for projection in result["projection"].values():
        for field in ("dscr_current", "dscr_with_refinance", "first_year_dscr_current", "first_year_dscr_with_refinance"):
            assert projection[field] is None
    assert result["expense_changes"]["gross_projection"]["dscr_current"] is None
    assert result["draft_feedback"]["summary"]["working"]["cash_after_debt_before_annual"] is None
    assert result["targets"] == {"current": [], "refinance": []}


@pytest.mark.parametrize("conflict", [False, True])
def test_actual_native_source_currency_flows_into_source_dscr_composition(conflict):
    from app.tests.test_financial_metric_review import ENTITY, pnl

    native = accepted(key_facts={"currency": "CAD"})
    if conflict:
        native["body"]["currency"] = "USD"
    result = build_cockpit([pnl()], [], accepted_schedule=native, business_entity=ENTITY)
    assert result["ebitda"]["headline"]["annualized_value"] == 52000
    assert result["dscr"]["headline"] is None
    candidate = next(row for row in result["dscr"]["candidates"] if row["key"] == "profit_and_loss")
    assert candidate["value"] is None and candidate["numerator"] is None
    assert candidate["denominator_currency"] == (None if conflict else "CAD")


@pytest.mark.parametrize("earnings_kind", ["tax_estimate", "custom_operating_measure"])
@pytest.mark.parametrize("setup", [0, 500])
def test_real_reviewed_adapter_applies_noi_effect_once_without_a_noncash_coverage_addback(earnings_kind, setup):
    from app.services.financial_noi import preview_scenario
    from app.services.financial_plan_operating import coverage_adjustment

    current = da_context()
    current["ebitda"]["headline"]["kind"] = earnings_kind
    reviewed = approve(da_body(implementation_cost=setup), current)
    result = evaluate(reviewed, current)
    context = {"available": True, "state": "staff_published", "evaluation": result}
    dscr = {"headline": {"key": "tax_return"}, "candidates": [{"key": "tax_return", "numerator": 30000, "currency_comparable": True}]}
    adjustment = coverage_adjustment(context, {}, dscr)
    assert adjustment["noi_applied"] and adjustment["applied_noi_annual_impact"] == 6000 - setup
    if earnings_kind == "tax_estimate":
        assert adjustment["applied"] and adjustment["applied_annual_impact"] == -setup
    else:
        assert not adjustment["applied"]
    source = {**current["noi"], "scenario": {"current": 18000}}
    scenario = preview_scenario(source, {"operating_plan": adjustment, "annual_new_operating_expenses": 0})
    assert scenario["plan"] == 24000 - setup
    assert source["scenario"]["current"] == 18000
    client = coverage_adjustment({**context, "state": "client_submitted"}, {}, dscr)
    assert not client["noi_applied"]


def test_working_cash_level_is_a_deficit_illustration_not_a_positive_bank_balance():
    result = evaluate()
    value = result["draft_feedback"]["summary"]["working"]
    assert (value["cash_after_debt_before_annual"], value["cash_after_debt_after_annual"], value["cash_after_debt_delta_annual"]) == (-90000, -48000, 42000)
    assert value["cash_after_debt_before_monthly"] == -7500
    assert value["cash_after_debt_after_monthly"] == -4000
    assert value["cash_after_debt_mode"] == "saved_refinance_and_operations"
    assert value["cash_after_debt_basis"] == "earnings_less_debt_service_illustration"


def test_saved_blocked_loan_keeps_current_cash_level_but_not_a_false_zero_planned_change():
    current = da_context()
    current["plan"]["evaluation"]["principal_solution"] = {"status": "missing", "requires_apply": True}
    value = evaluate(current=current)["draft_feedback"]["summary"]["working"]
    assert value["cash_after_debt_before_annual"] == -90000
    assert value["cash_after_debt_after_annual"] is None and value["cash_after_debt_delta_annual"] is None


def test_no_saved_refinance_compares_current_debt_and_operations_without_inventing_loan_relief():
    current = da_context()
    current["plan"]["record"] = {}
    value = evaluate(da_body(implementation_cost=25, implementation_cost_cadence="monthly"), current)["draft_feedback"]["summary"]["working"]
    assert (value["cash_after_debt_before_annual"], value["cash_after_debt_after_annual"]) == (-90000, -90300)
    assert value["cash_after_debt_mode"] == "current_debt_operations_only"
    assert value["loan_cash_change_annual"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("public", [False, True])
async def test_actual_evaluate_http_keeps_noncash_metrics_and_native_currency_through_pending_operations(public):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import get_current_user
    from app.enums import Role
    from app.routers import application_profiles, operating_plan
    from app.services import financial_workspace
    from app.tests.test_operating_client_preview import TOKEN, ReadOnlyMemory

    native = accepted()
    native["body"]["debts"][0]["monthly_payment"] = 10000
    native_service = build_cockpit([], [], accepted_schedule=native)["debt"]["service"]
    current = da_context()
    current["debt"]["service"] = native_service
    raw = da_body(implementation_cost=None, effective_date=None)
    db, app = ReadOnlyMemory(), FastAPI()
    db.draft.body = deepcopy(raw)
    routes = application_profiles if public else operating_plan
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    path = f"/application-profiles/public/financial-forms/{TOKEN}/evaluate" if public else f"/application-profiles/{db.profile.id}/operating-plan/evaluate"
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=current)), \
         patch.object(operating_plan.profiles, "load_profile", AsyncMock(return_value=db.profile)), \
         patch.object(financial_workspace, "existing_family", AsyncMock(return_value=db.family)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(path, json={"body": raw})
            assert response.status_code == 200, response.text
            evaluation = response.json()["evaluation"]
            feedback, cash = evaluation["draft_feedback"], evaluation["cash_bridge"]
            assert feedback["summary"]["draft_noi_delta"] == 6000
            assert feedback["summary"]["gross_earnings_annual_delta"] == 0
            assert feedback["actions"][0]["cash_net_annual_delta"] is None
            assert (cash["currency"], cash["loan_status"], cash["net_loan_cash_change_annual"]) == ("USD", "calculated", 42000)
            assert cash["modeled_operations_annual"] is None
            forged = {**raw, "draft_feedback": {"summary": {"draft_noi": 9000000}}}
            assert (await client.post(path, json={"body": forged})).status_code == 422
    db.assert_unchanged(raw)
