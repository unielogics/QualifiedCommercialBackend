"""One real version/plan/budget transaction, without providers or client data."""
from copy import deepcopy
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
from app.services import application_terms, capital_readiness, deal_budget
from app.services.loan_schedule import build_schedule
from app.tests.test_atomic_client_terms_plan import env as env
from app.tests.test_atomic_client_terms_plan import payload


@pytest.fixture
def atomic_env(env, monkeypatch):
    env.profile.deal_budget = {"revision": 0, "rows": [], "costs_confirmed": False}
    env.profile.financial_plan = {}
    env.session.commit()
    monkeypatch.setattr(deal_budget, "enforce_origination_cap", AsyncMock())
    env.readiness = AsyncMock()
    monkeypatch.setattr(capital_readiness, "advisory_recompute_profiles", env.readiness)
    return env


def request(atomic_env, **changes):
    row = DealCostRow(id=uuid4(), label="Financed closing costs", category="closing_cost", method="percent",
        value=10, settlement="financed")
    data = payload().model_dump(mode="python")
    data.update(expected_collateral_revision=0, expected_deal_budget_revision=0,
        deal_budget=DealBudgetPatch(expected_revision=0, loan_amount_mode="target_net", target_net_amount=100000,
            costs_confirmed=True, rows=[row]), idempotency_key="synthetic-standard-save-1",
        financial_plan=FinancialPlanWrite(expected_updated_at=None, expected_deal_budget_revision=0,
            new_loan_amount=100000, new_loan_rate_pct=12, new_loan_term_months=36, new_loan_structure="amortizing",
            retire=["row:loan-a"], exclude=["transfer"],
            add_backs=[{"id": "retained", "kind": "other", "label": "Reviewed adjustment", "amount_monthly": 500}],
            note="Retain the desk's explanation"))
    data.update(changes)
    return ClientTermsWrite.model_validate(data)


@pytest.mark.asyncio
async def test_one_save_stages_unsaved_costs_solves_gross_and_freezes_matching_terms_and_plan(atomic_env):
    env = atomic_env
    draft = request(env)
    result = await routes.put_client_terms(env.profile.id, draft, env.actor, env.db)
    env.session.expire_all()
    rows = env.session.scalars(select(ApplicationTermSheet)).all()
    assert len(rows) == 1 and rows[0].version == 1
    row = rows[0]
    expected = build_schedule(111111.11, 12, 36, structure="amortizing", periods_per_year=12)
    assert float(row.amount) == 111111.11 and result.amount == 111111.11
    assert env.profile.financial_plan["new_loan_amount"] == 111111.11
    assert env.profile.financial_plan["retire"] == ["row:loan-a"]
    assert env.profile.financial_plan["exclude"] == ["transfer"]
    assert env.profile.financial_plan["add_backs"][0]["label"] == "Reviewed adjustment"
    assert env.profile.financial_plan["note"] == "Retain the desk's explanation"
    assert env.profile.deal_budget["revision"] == 1 and env.profile.deal_budget["target_net_amount"] == "100000"
    assert row.deal_budget_revision == 1 and row.deal_budget_snapshot["target_net_amount"] == "100000"
    assert row.deal_budget_calculation["gross_loan_amount"] == 111111.11
    assert row.deal_budget_calculation["net_client_cash"] == 100000
    assert row.deal_budget_calculation["borrower_financed_fees"] == 11111.11
    assert float(row.periodic_payment) == expected["payment_after"]
    assert result.financial_plan.record["new_loan_amount"] == 111111.11
    assert result.current_deal_budget_revision == 1 and not result.deal_budget_stale
    assert row.status == "draft" and row.issued_pdf_bytes is None
    assert env.db.commits == 1 and env.db.rollbacks == 0
    env.readiness.assert_awaited_once()
    assert str(row.id) in env.readiness.await_args.kwargs["event_key"]


@pytest.mark.asyncio
async def test_timeout_retry_returns_same_version_without_compounding_or_new_budget_revision(atomic_env):
    env = atomic_env
    draft = request(env)
    first = await routes.put_client_terms(env.profile.id, draft, env.actor, env.db)
    before = deepcopy(env.profile.financial_plan)
    second = await routes.put_client_terms(env.profile.id, draft, env.actor, env.db)
    assert (second.term_sheet_id, second.version, second.amount) == (first.term_sheet_id, 1, 111111.11)
    assert env.profile.financial_plan == before and env.profile.deal_budget["revision"] == 1
    assert len(env.session.scalars(select(ApplicationTermSheet)).all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["commit", "response_context"])
async def test_failure_rolls_back_real_budget_plan_and_version_writes(atomic_env, monkeypatch, failure):
    env = atomic_env
    before_plan, before_budget = deepcopy(env.profile.financial_plan), deepcopy(env.profile.deal_budget)
    draft = request(env)
    if failure == "commit":
        env.db.fail_commit = True
    else:
        monkeypatch.setattr(application_terms, "read_terms", AsyncMock(side_effect=RuntimeError("fictional response context failure")))
    with pytest.raises(RuntimeError):
        await routes.put_client_terms(env.profile.id, draft, env.actor, env.db)
    env.session.expire_all()
    assert env.profile.financial_plan == before_plan and env.profile.deal_budget == before_budget
    assert env.session.scalars(select(ApplicationTermSheet)).all() == []
    assert env.db.rollbacks == 1 and env.db.commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", ["version", "plan", "budget", "collateral"])
async def test_conflicts_reject_before_any_budget_or_version_change(atomic_env, stale):
    env = atomic_env
    draft = request(env)
    if stale == "version":
        draft = draft.model_copy(update={"expected_version": 1})
    elif stale == "plan":
        env.profile.financial_plan = {"updated_at": "2026-11-06T12:00:00+00:00"}
    elif stale == "budget":
        env.profile.deal_budget = {**env.profile.deal_budget, "revision": 3}
    else:
        draft = draft.model_copy(update={"expected_collateral_revision": 7})
    env.session.commit()
    before_plan, before_budget = deepcopy(env.profile.financial_plan), deepcopy(env.profile.deal_budget)
    with pytest.raises(HTTPException) as error:
        await routes.put_client_terms(env.profile.id, draft, env.actor, env.db)
    assert error.value.status_code == 409
    env.session.expire_all()
    assert env.profile.financial_plan == before_plan and env.profile.deal_budget == before_budget
    assert env.session.scalars(select(ApplicationTermSheet)).all() == []


@pytest.mark.asyncio
async def test_old_callers_without_nested_budget_keep_explicit_principal_guard(atomic_env):
    env = atomic_env
    env.profile.deal_budget = request(env).deal_budget.model_dump(mode="json", exclude={"expected_revision"})
    env.profile.deal_budget["revision"] = 1
    env.session.commit()
    legacy = payload(expected_deal_budget_revision=1)
    with pytest.raises(HTTPException) as error:
        await routes.put_client_terms(env.profile.id, legacy, env.actor, env.db)
    assert error.value.status_code == 409
    assert env.session.scalars(select(ApplicationTermSheet)).all() == []


@pytest.mark.asyncio
async def test_same_key_changed_request_rejected_without_creating_another_version(atomic_env):
    env = atomic_env
    draft = request(env)
    await routes.put_client_terms(env.profile.id, draft, env.actor, env.db)
    with pytest.raises(HTTPException) as error:
        await routes.put_client_terms(env.profile.id, draft.model_copy(update={"client_note": "Changed terms"}), env.actor, env.db)
    assert error.value.status_code == 409
    assert len(env.session.scalars(select(ApplicationTermSheet)).all()) == 1


@pytest.mark.asyncio
async def test_solved_principal_exceeding_terms_limit_returns_422_and_rolls_back_staged_costs(atomic_env):
    env = atomic_env
    initial = request(env)
    budget = DealBudgetPatch.model_validate({**initial.deal_budget.model_dump(mode="python"), "target_net_amount": 999999999})
    draft = request(env, amount=999999999, deal_budget=budget)
    before_plan, before_budget = deepcopy(env.profile.financial_plan), deepcopy(env.profile.deal_budget)
    with pytest.raises(HTTPException) as error:
        await routes.put_client_terms(env.profile.id, draft, env.actor, env.db)
    assert error.value.status_code == 422 and "$1,111,111,110.00" in error.value.detail
    env.session.expire_all()
    assert env.profile.financial_plan == before_plan and env.profile.deal_budget == before_budget
    assert env.session.scalars(select(ApplicationTermSheet)).all() == []
    assert env.db.commits == 0 and env.db.rollbacks == 1


@pytest.mark.asyncio
async def test_staging_helper_does_not_flush_or_commit(atomic_env):
    env = atomic_env
    before_commits = env.db.commits
    flush = env.db.flush
    env.db.flush = AsyncMock()
    draft = request(env)
    try:
        budget, solution = await deal_budget.stage_for_terms(env.db, env.profile, draft.deal_budget,
            {"amount": draft.amount, "rate_pct": draft.apr_pct, "term_months": draft.term_months}, env.actor)
        assert solution["gross_loan_amount"] == 111111.11 and budget["revision"] == 1
        env.db.flush.assert_not_awaited()
        assert env.db.commits == before_commits
    finally:
        env.db.flush = flush
        env.session.rollback()


@pytest.mark.parametrize("mode,requested,target,expected_scope", [
    ("target_net", 100000, 100000, "requested_net_budget"),
    ("target_net", 120000, 100000, "requested_reference"),
    ("base_loan", 100000, 100000, "requested_reference"),
    ("gross_loan", 100000, 100000, "requested_reference"),
])
def test_only_explicit_matching_net_target_reconciles_requested_uses_across_a_higher_note(mode, requested, target, expected_scope):
    row = DealCostRow(id=uuid4(), label="Financed closing cost", category="closing_cost", method="percent", value=10,
        settlement="financed").model_dump(mode="json")
    budget = {"revision": 1, "loan_amount_mode": mode, "target_net_amount": target, "base_loan_amount": target,
        "costs_confirmed": True, "rows": [row]}
    terms = {"amount": 111111.11, "requested_amount": requested, "rate_pct": 12, "term_months": 36,
        "structure": "amortizing"}
    uses = [{"category": "working_capital", "amount": requested}]
    original = deepcopy(uses)
    calculated = deal_budget.calculate(budget, terms, uses)
    allocation = calculated["allocation_reconciliation"]
    assert allocation["scope"] == expected_scope
    if expected_scope == "requested_net_budget":
        assert allocation["status"] == "balanced" and allocation["available_for_uses"] == 100000
    else:
        assert allocation["status"] == "different_scope"
    assert uses == original and terms["requested_amount"] == requested


def test_recorded_net_target_uses_reconciliation_and_explicit_proposal_scope_are_preserved():
    budget = {"revision": 1, "loan_amount_mode": "target_net", "target_net_amount": 100000,
        "costs_confirmed": True, "rows": []}
    frozen = {"net_client_cash": 100000, "status": "ready"}
    terms = {"amount": 111111.11, "requested_amount": 100000, "source": "term_sheet",
        "recorded_terms_ref": {"id": "recorded-version"}, "deal_budget_revision": 1,
        "deal_budget_snapshot": budget, "deal_budget_calculation": frozen}
    profile = type("Profile", (), {"id": uuid4(), "deal_budget": budget,
        "use_of_funds": [{"category": "working_capital", "amount": 100000}]})()
    report = deal_budget.reporting(profile, terms)
    assert {key: report["calculation"][key] for key in frozen} == frozen
    assert terms["deal_budget_calculation"] == frozen
    assert "investor_scenarios" in report["calculation"]
    assert report["current_allocation_reconciliation"]["scope"] == "requested_net_budget"
    assert report["current_allocation_issues"] == []
    # A partial draw / independent dealer allocation keeps its exact scope;
    # the request cannot override the separately recorded proposal budget.
    terms.update(proposal_use_of_funds={"inventory": 50000}, proposal_use_of_funds_amount=50000)
    allocation = deal_budget.reporting(profile, terms)["current_allocation_reconciliation"]
    assert allocation["scope"] == "proposed_reference" and allocation["status"] == "different_scope"
