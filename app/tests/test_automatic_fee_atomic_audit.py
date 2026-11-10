"""Independent checks of net proceeds, canonical caps, replay and frozen versions."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.application_terms import ApplicationTermSheet
from app.routers import application_terms as routes
from app.schemas.application_terms import ClientTermsWrite
from app.schemas.deal_budget import DealBudgetPatch, DealCostRow
from app.schemas.financial_cockpit import FinancialPlanWrite
from app.services import deal_budget
from app.services.loan_schedule import build_schedule
from app.tests.test_atomic_client_terms_plan import env as env
from app.tests.test_automatic_client_terms_budget import atomic_env as atomic_env
from app.tests.test_automatic_client_terms_budget import request


@pytest.mark.asyncio
async def test_fee_cap_uses_solved_principal_and_rejection_changes_nothing(atomic_env, monkeypatch):
    target = atomic_env
    draft = request(target)
    before = deepcopy(target.profile.deal_budget), deepcopy(target.profile.financial_plan)

    async def enforce(_db, _profile, _budget, amount):
        assert amount == 111111.11
        raise HTTPException(422, "Synthetic program limit is 110000")

    check = AsyncMock(side_effect=enforce)
    monkeypatch.setattr(deal_budget, "enforce_origination_cap", check)
    with pytest.raises(HTTPException) as error:
        await routes.put_client_terms(target.profile.id, draft, target.actor, target.db)
    assert error.value.status_code == 422
    check.assert_awaited_once()
    target.session.expire_all()
    assert (target.profile.deal_budget, target.profile.financial_plan) == before
    assert target.session.scalars(select(ApplicationTermSheet)).all() == []
    assert target.db.commits == 0 and target.db.rollbacks == 1


@pytest.mark.asyncio
async def test_second_terms_version_keeps_same_net_anchor_and_does_not_revise_equivalent_costs(atomic_env):
    target = atomic_env
    first = await routes.put_client_terms(target.profile.id, request(target), target.actor, target.db)
    row_one = target.session.scalars(select(ApplicationTermSheet)).one()
    original_snapshot = deepcopy(row_one.deal_budget_snapshot)
    plan = deepcopy(target.profile.financial_plan)
    budget = deepcopy(target.profile.deal_budget)
    patch = DealBudgetPatch(expected_revision=budget["revision"], **{
        key: budget[key] for key in DealBudgetPatch.model_fields if key != "expected_revision" and key in budget
    })
    draft = ClientTermsWrite.model_validate({**request(target).model_dump(mode="python"),
        "expected_version": first.version, "expected_deal_budget_revision": 1,
        "amount": first.amount, "client_note": "A second reviewed explanation",
        "deal_budget": patch, "idempotency_key": "independent-second-version",
        "financial_plan": FinancialPlanWrite.model_validate({**plan,
            "expected_updated_at": plan["updated_at"], "expected_deal_budget_revision": 1})})
    second = await routes.put_client_terms(target.profile.id, draft, target.actor, target.db)
    target.session.expire_all()
    rows = target.session.scalars(select(ApplicationTermSheet).order_by(ApplicationTermSheet.version)).all()
    assert len(rows) == 2 and second.version == 2
    assert first.amount == second.amount == 111111.11
    assert target.profile.deal_budget["revision"] == 1
    assert float(target.profile.deal_budget["target_net_amount"]) == 100000
    assert second.deal_budget_calculation["net_client_cash"] == 100000
    assert rows[0].deal_budget_snapshot == original_snapshot
    assert rows[0].is_current is False and rows[1].is_current is True


@pytest.mark.asyncio
@pytest.mark.parametrize("later", ["budget", "plan", "collateral"])
async def test_replaying_old_save_after_later_change_cannot_create_or_restore_a_version(atomic_env, later):
    target = atomic_env
    draft = request(target)
    await routes.put_client_terms(target.profile.id, draft, target.actor, target.db)
    if later == "budget":
        target.profile.deal_budget = {**target.profile.deal_budget, "revision": 2}
    elif later == "plan":
        target.profile.financial_plan = {**target.profile.financial_plan, "updated_at": "2026-11-10T20:00:00+00:00"}
    else:
        target.profile.investor_collateral = {"revision": 1, "enabled": False, "properties": [], "assets": []}
    target.session.commit()
    before = deepcopy(target.profile.deal_budget), deepcopy(target.profile.financial_plan), deepcopy(target.profile.investor_collateral)
    with pytest.raises(HTTPException) as error:
        await routes.put_client_terms(target.profile.id, draft, target.actor, target.db)
    assert error.value.status_code == 409
    target.session.expire_all()
    assert (target.profile.deal_budget, target.profile.financial_plan, target.profile.investor_collateral) == before
    assert len(target.session.scalars(select(ApplicationTermSheet)).all()) == 1


@pytest.mark.asyncio
async def test_idempotency_receipt_cannot_be_reused_by_another_staff_actor(atomic_env):
    target = atomic_env
    draft = request(target)
    await routes.put_client_terms(target.profile.id, draft, target.actor, target.db)
    different_actor = SimpleNamespace(id=uuid4(), role=target.actor.role)
    with pytest.raises(HTTPException) as error:
        await routes.put_client_terms(target.profile.id, draft, different_actor, target.db)
    assert error.value.status_code == 409
    assert len(target.session.scalars(select(ApplicationTermSheet)).all()) == 1


@pytest.mark.asyncio
async def test_one_save_calculates_coverage_with_financed_principal_and_new_costs_once(atomic_env):
    target = atomic_env
    draft = request(target)
    budget = draft.deal_budget.model_copy(update={"rows": [*draft.deal_budget.rows,
        DealCostRow(id=uuid4(), label="Annual financing service", category="servicing", value=600,
            timing="annual", settlement="separate", earnings_treatment="financing_cost"),
        DealCostRow(id=uuid4(), label="New annual operating service", category="other", value=1200,
            timing="annual", settlement="separate", earnings_treatment="operating_expense")]})
    result = await routes.put_client_terms(target.profile.id, draft.model_copy(update={"deal_budget": budget}), target.actor, target.db)
    row = target.session.scalars(select(ApplicationTermSheet)).one()
    loan = build_schedule(111111.11, 12, 36, structure="amortizing", periods_per_year=12)
    # Fixture baseline120000 + reviewed500/month; retained debt2000/month.
    income = 120000 + 500 * 12 - 1200
    annual_service = 2000 * 12 + loan["annual_debt_service"] + 600
    assert float(row.cash_flow_value) == income
    assert float(row.projected_annual_debt_service) == pytest.approx(annual_service, abs=0.01)
    assert result.calculation.dscr_after == pytest.approx(income / annual_service, abs=0.001)
    assert row.deal_budget_calculation["net_client_cash"] == 100000
