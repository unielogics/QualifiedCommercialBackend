from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.enums import Role
from app.routers import application_profiles as routes
from app.schemas.deal_budget import DealBudgetApply, DealBudgetPatch, DealBudgetPreview, DealCostRow
from app.schemas.financial_cockpit import FinancialPlanWrite
from app.services import deal_budget, financial_cockpit, financial_plan, plan_terms_link
from app.tests.test_atomic_client_terms_plan import env as env


def setup():
    stamp = datetime.now(UTC).isoformat()
    profile = SimpleNamespace(id=uuid4(), financial_plan={"updated_at": stamp, "new_loan_amount": 100000,
        "new_loan_rate_pct": 12, "new_loan_term_months": 24, "new_loan_structure": "amortizing",
        "new_loan_payments_per_year": 12, "new_payment_monthly": 999, "retire": ["retained-choice"], "exclude": [],
        "add_backs": [{"id": "reviewed", "kind": "other", "label": "Reviewed earnings", "amount_monthly": 1000}],
        "note": "Preserve the desk's context"}, deal_budget={"revision": 3, "rows": [], "costs_confirmed": False},
        forecast_fee_points=None, forecast_consulting_fee=None, use_of_funds=[], underwriting_approved_amount=80000,
        underwriting_accepted_amount=None)
    actor = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    row = DealCostRow(id=uuid4(), label="Financed closing cost", category="closing_cost", method="percent",
        value=10, settlement="financed")
    body = dict(expected_revision=3, loan_amount_mode="target_net", target_net_amount=100000,
        rows=[row], costs_confirmed=True, expected_plan_updated_at=stamp, expected_terms_ref=None,
        idempotency_key="synthetic-apply-1")
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: profile)), flush=AsyncMock(), commit=AsyncMock())
    cockpit = {"proposed_terms": {"amount": 100000}, "plan": {"record": deepcopy(profile.financial_plan)}}
    return profile, actor, body, db, cockpit


@pytest.mark.asyncio
async def test_apply_is_atomic_idempotent_and_preserves_underwriting_lifecycle_and_plan_context():
    profile, actor, body, db, cockpit = setup()
    original = deepcopy(profile.financial_plan)
    payload = DealBudgetApply(**body)
    with patch.object(financial_cockpit, "load_cockpit", AsyncMock(return_value=cockpit)), \
        patch.object(deal_budget, "enforce_origination_cap", AsyncMock()) as cap, \
        patch.object(deal_budget, "read", AsyncMock(return_value="saved")), \
        patch("app.services.application_profiles.log_profile_action", AsyncMock()) as audit:
        assert await deal_budget.apply_principal(db, profile, payload, actor) == "saved"
        assert profile.deal_budget["revision"] == 4
        assert profile.financial_plan["new_loan_amount"] == 111111.11
        assert profile.financial_plan["new_payment_monthly"] is None
        for key in ("retire", "exclude", "add_backs", "note", "new_loan_rate_pct", "new_loan_term_months"):
            if key == "add_backs":
                assert all(profile.financial_plan[key][0][field] == value for field, value in original[key][0].items())
            else:
                assert profile.financial_plan[key] == original[key]
        assert profile.underwriting_approved_amount == 80000 and profile.underwriting_accepted_amount is None
        cap.assert_awaited_once()
        assert cap.await_args.args[-1] == 111111.11
        saved = deepcopy(vars(profile))
        assert await deal_budget.apply_principal(db, profile, payload, actor) == "saved"
        assert vars(profile) == saved
        audit.assert_awaited_once()
        db.flush.assert_awaited_once()
        db.commit.assert_not_awaited()  # The router commits both writes together.


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", ["budget", "plan", "terms", "pricing"])
async def test_apply_rejects_stale_or_invalid_pricing_before_mutating_either_record(stale):
    profile, actor, body, db, cockpit = setup()
    if stale == "budget":
        body["expected_revision"] = 2
    elif stale == "plan":
        body["expected_plan_updated_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    elif stale == "terms":
        cockpit["proposed_terms"]["recorded_terms_ref"] = {"kind": "application_term_sheet", "id": str(uuid4()), "version": 2}
    before = deepcopy(vars(profile))
    with patch.object(financial_cockpit, "load_cockpit", AsyncMock(return_value=cockpit)), \
        patch.object(deal_budget, "enforce_origination_cap", AsyncMock(side_effect=HTTPException(422, "Cap") if stale == "pricing" else None)), \
        patch("app.services.application_profiles.log_profile_action", AsyncMock()) as audit:
        with pytest.raises(HTTPException) as error:
            await deal_budget.apply_principal(db, profile, DealBudgetApply(**body), actor)
    assert error.value.status_code == (422 if stale == "pricing" else 409)
    assert vars(profile) == before
    db.flush.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_key_cannot_be_reused_for_different_inputs_or_after_later_plan_changes():
    profile, actor, body, db, cockpit = setup()
    with patch.object(financial_cockpit, "load_cockpit", AsyncMock(return_value=cockpit)), \
        patch.object(deal_budget, "enforce_origination_cap", AsyncMock()), \
        patch.object(deal_budget, "read", AsyncMock()), \
        patch("app.services.application_profiles.log_profile_action", AsyncMock()):
        await deal_budget.apply_principal(db, profile, DealBudgetApply(**body), actor)
        with pytest.raises(HTTPException) as mismatch:
            await deal_budget.apply_principal(db, profile, DealBudgetApply(**{**body, "target_net_amount": 120000}), actor)
        assert mismatch.value.status_code == 409
        profile.financial_plan["updated_at"] = datetime.now(UTC).isoformat()
        with pytest.raises(HTTPException) as later:
            await deal_budget.apply_principal(db, profile, DealBudgetApply(**body), actor)
        assert later.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("internal_kind,api_kind", [("client_terms", "application_term_sheet"), ("production_term_sheet", "production_term_sheet")])
async def test_standard_and_dealer_terms_cas_normalizes_api_ref_but_preserves_internal_adoption(internal_kind, api_kind):
    profile, actor, body, db, cockpit = setup()
    source_id = uuid4()
    internal = {"kind": internal_kind, "id": source_id, "version": 4, "label": "Current terms"}
    cockpit["proposed_terms"].update(recorded_terms_ref=internal, rate_pct=12, term_months=24,
        structure="amortizing", payments_per_year=12)
    profile.financial_plan["new_loan_amount"] = None
    body["expected_terms_ref"] = {"kind": api_kind, "id": str(source_id), "version": 4}
    assert deal_budget._terms_ref(cockpit) == body["expected_terms_ref"]
    with patch.object(financial_cockpit, "load_cockpit", AsyncMock(return_value=cockpit)), \
        patch.object(deal_budget, "enforce_origination_cap", AsyncMock()), \
        patch.object(deal_budget, "read", AsyncMock()), \
        patch("app.services.application_profiles.log_profile_action", AsyncMock()):
        await deal_budget.apply_principal(db, profile, DealBudgetApply(**body), actor)
    assert profile.financial_plan["terms_ref"]["kind"] == internal_kind
    assert profile.financial_plan["terms_ref"]["id"] == str(source_id)


def recorded_refinance(kind):
    row = SimpleNamespace(id=uuid4(), version=4, amount=100000, apr_pct=12, term_months=24,
        periodic_payment=4707.35, payments_per_year=12, program_name="Reviewed financing",
        loan_structure="amortizing", debt_service_treatment="refinance", retained_annual_debt_service=24000)
    if kind == "client_terms":
        return plan_terms_link.recorded_from_client_terms(row)
    row.extra = {"debt_service_treatment": "refinance", "retained_annual_debt_service": 24000}
    row.approved_amount, row.rate_pct, row.monthly_debt_service = 100000, 12, 4707.35
    row.funding_party_name, row.debt_service_is_level_payment = "Reviewed lender", True
    return plan_terms_link.recorded_from_production_sheet(row)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["client_terms", "production_term_sheet"])
@pytest.mark.parametrize("legacy_reference", [False, True])
async def test_empty_plan_adoption_preserves_recorded_refinance_basis(kind, legacy_reference):
    profile, actor, body, db, cockpit = setup()
    recorded = recorded_refinance(kind)
    profile.financial_plan = {"updated_at": profile.financial_plan["updated_at"], "retire": [], "exclude": []}
    ref = {key: recorded[key] for key in ("kind", "id", "version", "label")}
    cockpit["proposed_terms"] = {**recorded, "recorded_terms_ref": ref, "monthly_payment": recorded["payment_monthly"]}
    cockpit["plan"]["terms_link"] = {"recorded": None if legacy_reference else recorded}
    body["expected_terms_ref"] = {"kind": "application_term_sheet" if kind == "client_terms" else kind,
        "id": recorded["id"], "version": 4}
    with patch.object(financial_cockpit, "load_cockpit", AsyncMock(return_value=cockpit)), \
        patch.object(deal_budget, "enforce_origination_cap", AsyncMock()), \
        patch.object(deal_budget, "read", AsyncMock()), \
        patch("app.services.application_profiles.log_profile_action", AsyncMock()):
        await deal_budget.apply_principal(db, profile, DealBudgetApply(**body), actor)
    result = financial_plan.evaluate_plan(profile.financial_plan, obligations_list=[], recurring={}, months=[],
        dscr={}, service={"monthly": 6000}, proposed=None, requested_amount=None)
    assert profile.financial_plan["new_payment_source"] == "recorded_terms"
    assert profile.financial_plan["terms_ref"]["treatment"] == "refinance"
    assert profile.financial_plan["terms_ref"]["retained_annual_debt_service"] == 24000
    assert result["debt_service_before_new_payment_monthly"] == 2000
    assert result["debt_service_after_monthly"] == pytest.approx(2000 + result["new_payment_monthly"], abs=.01)


@pytest.mark.asyncio
async def test_existing_payoff_choices_are_not_deducted_from_recorded_retained_service_twice():
    profile, actor, body, db, cockpit = setup()
    recorded = recorded_refinance("client_terms")
    old_stamp = profile.financial_plan["updated_at"]
    profile.financial_plan = plan_terms_link.adopt(profile.financial_plan, recorded)
    profile.financial_plan["updated_at"] = old_stamp
    body["financial_plan"] = {**profile.financial_plan, "retire": ["paid-off-loan"]}
    cockpit["proposed_terms"] = {**recorded, "recorded_terms_ref": {key: recorded[key] for key in ("kind", "id", "version")}}
    body["expected_terms_ref"] = {"kind": "application_term_sheet", "id": recorded["id"], "version": 4}
    with patch.object(financial_cockpit, "load_cockpit", AsyncMock(return_value=cockpit)), \
        patch.object(deal_budget, "enforce_origination_cap", AsyncMock()), \
        patch.object(deal_budget, "read", AsyncMock()), \
        patch("app.services.application_profiles.log_profile_action", AsyncMock()):
        await deal_budget.apply_principal(db, profile, DealBudgetApply(**body), actor)
    obligations = [{"id": "paid-off-loan", "monthly": 1000, "payoff": 50000, "in_debt_service": True}]
    result = financial_plan.evaluate_plan(profile.financial_plan, obligations_list=obligations, recurring={}, months=[],
        dscr={}, service={"monthly": 6000}, proposed=None, requested_amount=None)
    assert profile.financial_plan["retire"] == ["paid-off-loan"]
    assert result["debt_service_before_new_payment_monthly"] == 2000


@pytest.mark.asyncio
async def test_budget_only_save_cannot_leave_a_known_changed_principal_unsynchronized():
    profile, actor, body, db, cockpit = setup()
    payload = DealBudgetPatch.model_validate({key: value for key, value in body.items() if key in DealBudgetPatch.model_fields})
    before = deepcopy(vars(profile))
    with patch.object(financial_cockpit, "load_cockpit", AsyncMock(return_value=cockpit)):
        with pytest.raises(HTTPException) as error:
            await deal_budget.update(db, profile, payload, actor)
    assert error.value.status_code == 409 and vars(profile) == before


@pytest.mark.asyncio
async def test_legacy_fee_writer_cannot_change_target_net_costs_without_applying_principal():
    profile, actor, _body, _db, _cockpit = setup()
    row = DealCostRow(id=uuid4(), label="QC points", category="origination", method="percent", value=10, settlement="financed").model_dump(mode="json")
    profile.deal_budget = {"revision": 3, "loan_amount_mode": "target_net", "target_net_amount": 90000,
        "costs_confirmed": True, "rows": [row]}
    before = deepcopy(vars(profile))
    with patch("app.services.application_terms.lock_profile_terms", AsyncMock()), \
        patch("app.services.application_profiles.log_profile_action", AsyncMock()) as audit:
        with pytest.raises(HTTPException) as error:
            await deal_budget.sync_legacy_write(SimpleNamespace(), profile, {"forecast_fee_points": 12}, actor)
    assert error.value.status_code == 409 and vars(profile) == before
    audit.assert_not_awaited()


@pytest.mark.parametrize("change", [{"payer": "investor"}, {"timing": "monthly"}, {"payer": "sponsor"}])
def test_only_borrower_closing_costs_can_be_financed(change):
    with pytest.raises(ValidationError):
        DealCostRow(id=uuid4(), label="Invalid capitalized cost", category="closing_cost", value=100,
            settlement="financed", **change)


def test_preview_validates_working_plan_and_retains_explicit_null_custom_cadence():
    preview = DealBudgetPreview(expected_revision=0, financial_plan={"new_loan_amount": 100000,
        "new_loan_rate_pct": 10, "new_loan_term_months": 24, "new_loan_payments_per_year": None})
    assert preview.financial_plan["new_loan_payments_per_year"] is None
    assert "retire" not in preview.financial_plan
    with pytest.raises(ValidationError):
        DealBudgetPreview(expected_revision=0, financial_plan={"new_loan_rate_pct": 999})


@pytest.mark.asyncio
async def test_preview_exposes_saved_cas_context_even_after_local_principal_application():
    profile, actor, body, db, cockpit = setup()
    cockpit["debt"] = {"obligations": []}
    cockpit["plan"]["evaluation"] = {}
    profile.use_of_funds = []
    plan = FinancialPlanWrite(new_loan_amount=111111.11, new_loan_rate_pct=12, new_loan_term_months=24).model_dump(mode="json")
    preview = {"loan": {"amount": 111111.11, "rate_pct": 12, "term_months": 24, "requested_amount": None},
        "evaluation": {"headline_after": -0.5, "principal_solution": {"requires_apply": True,
            "current_loan_amount": 100000, "gross_loan_amount": 111111.11}}, "record": plan}
    with patch.object(financial_cockpit, "load_cockpit", AsyncMock(return_value=cockpit)), \
        patch.object(financial_cockpit, "financing_preview", return_value=preview) as evaluate:
        result = await deal_budget.read(db, profile, actor, budget=DealBudgetPatch.model_validate({key: value for key, value in body.items() if key in DealBudgetPatch.model_fields}).model_dump(mode="json"), financial_plan=plan)
    assert result.financing_context["current_loan_amount"] == 100000
    assert result.financing_context["plan_updated_at"] == profile.financial_plan["updated_at"]
    assert result.financial_plan_evaluation["headline_after"] == -0.5
    assert result.calculation["principal_solution"]["requires_apply"] is True
    assert result.calculation["principal_solution"]["current_loan_amount"] == 100000
    assert evaluate.call_args.args[1]["new_loan_amount"] == 111111.11
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_atomic_route_commit_failure_rolls_back_both_real_json_writes(env):
    template, _actor, body, _db, cockpit = setup()
    env.profile.financial_plan = deepcopy(template.financial_plan)
    env.profile.deal_budget = deepcopy(template.deal_budget)
    env.session.commit()
    old_plan, old_budget = deepcopy(env.profile.financial_plan), deepcopy(env.profile.deal_budget)
    env.db.fail_commit = True
    with patch.object(routes.profiles, "load_profile", AsyncMock(return_value=env.profile)), \
        patch.object(env.db, "execute", AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: env.profile))), \
        patch.object(financial_cockpit, "load_cockpit", AsyncMock(return_value=cockpit)), \
        patch.object(deal_budget, "enforce_origination_cap", AsyncMock()), \
        patch.object(deal_budget, "read", AsyncMock(return_value=SimpleNamespace(revision=4))), \
        patch.object(routes.capital_readiness, "advisory_recompute_profiles", AsyncMock()) as readiness:
        with pytest.raises(RuntimeError, match="commit failure"):
            await routes.apply_application_deal_budget_principal(env.profile.id, DealBudgetApply(**body), env.actor, env.db)
    assert env.db.rollbacks == 1 and env.db.commits == 0
    env.session.expire(env.profile, ["financial_plan", "deal_budget"])
    assert env.profile.financial_plan == old_plan and env.profile.deal_budget == old_budget
    readiness.assert_awaited_once()
