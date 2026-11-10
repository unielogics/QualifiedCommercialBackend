"""A dealer budget, solved loan, immutable version and plan share one commit."""
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    MetaData,
    Numeric,
    Table,
    Text,
    Uuid,
    create_engine,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, registry

from app.enums import Role
from app.models.production_package import ProductionPackage, ProductionTermSheet
from app.routers import production_packages as routes
from app.schemas.deal_budget import DealBudgetRead
from app.schemas.production_package import ProductionTermSheetBody
from app.services import (
    application_terms,
    capital_readiness,
    deal_budget,
    financial_cockpit,
    financial_plan,
    plan_terms_link,
)
from app.services import production_term_sheets as sheets
from app.services.loan_schedule import build_schedule


class DB:
    def __init__(self, session):
        self.session, self.commits, self.rollbacks = session, 0, 0

    @property
    def no_autoflush(self):
        return self.session.no_autoflush

    async def execute(self, query):
        return self.session.execute(query)

    async def refresh(self, row, **kwargs):
        self.session.refresh(row, **kwargs)

    async def get(self, model, identity, **kwargs):
        return self.session.get(model, identity, **kwargs)

    def add(self, row):
        self.session.add(row)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.session.commit()
        self.commits += 1

    async def rollback(self):
        self.session.rollback()
        self.rollbacks += 1


@pytest.fixture
def env(monkeypatch):
    engine, metadata = create_engine("sqlite:///:memory:"), MetaData()
    for model in (ProductionTermSheet, ProductionPackage):
        table = model.__table__.to_metadata(metadata)
        for constraint in list(table.constraints):
            if constraint in table.foreign_key_constraints or isinstance(constraint, CheckConstraint):
                table.constraints.remove(constraint)
        table.foreign_keys.clear()
        for column in table.columns:
            column.foreign_keys.clear()
            if isinstance(column.type, JSONB):
                column.type = JSON()
            if column.server_default is not None and "::" in str(column.server_default.arg):
                column.server_default = None
        for index in table.indexes:
            if index.dialect_options["postgresql"].get("where") is not None:
                index.dialect_options["sqlite"]["where"] = index.dialect_options["postgresql"]["where"]
    profiles = Table("application_profiles", metadata, Column("id", Uuid, primary_key=True),
        Column("financial_plan", JSON), Column("deal_budget", JSON), Column("investor_collateral", JSON),
        Column("forecast_fee_points", Numeric), Column("forecast_consulting_fee", Numeric),
        Column("underwriting_approved_amount", Numeric), Column("underwriting_term_sheet_amount", Numeric),
        Column("underwriting_status", Text))
    mapper = registry()

    class Profile:
        primary_bucket_id = intake_id = dealer_id = loan_id = deal_id = None

    mapper.map_imperatively(Profile, profiles)
    metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    profile = Profile()
    profile.id, profile.underwriting_status = uuid4(), "in_underwriting"
    profile.financial_plan = financial_plan.normalize_plan({"retire": ["row:old"], "exclude": ["payee:transfer"],
        "add_backs": [{"kind": "other", "amount_monthly": 250, "note": "Reviewed operating cost"}], "note": "Keep payoff evidence"})
    session.add(profile)
    session.commit()
    actor, db = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC), DB(session)
    cap = AsyncMock()
    readiness = AsyncMock()
    monkeypatch.setattr(deal_budget, "enforce_origination_cap", cap)
    monkeypatch.setattr(capital_readiness, "advisory_recompute_profiles", readiness)
    monkeypatch.setattr(sheets.profiles, "log_profile_action", AsyncMock())
    monkeypatch.setattr(application_terms, "_dscr_context", AsyncMock(return_value=application_terms.DscrContext(
        "business", 10, 120000, "Reviewed annual earnings", 12000, None, "Synthetic P&L")))

    async def underwriting(_db, target, _actor, changes):
        target.underwriting_approved_amount = changes["approved_amount"]
        if "term_sheet_amount" in changes:
            target.underwriting_term_sheet_amount = changes["term_sheet_amount"]
        if "underwriting_status" in changes:
            target.underwriting_status = changes["underwriting_status"]

    monkeypatch.setattr("app.routers.application_profiles.apply_underwriting_changes", underwriting)
    value = SimpleNamespace(db=db, session=session, profile=profile, actor=actor, cap=cap, readiness=readiness)
    try:
        yield value
    finally:
        session.close()
        engine.dispose()
        mapper.dispose()


def payload(env, **changes):
    costs = {"expected_revision": 0, "loan_amount_mode": "target_net", "target_net_amount": 100000,
        "costs_confirmed": True, "rows": [
            {"id": str(uuid4()), "label": "Financed points", "category": "origination", "method": "percent", "value": 2, "settlement": "financed"},
            {"id": str(uuid4()), "label": "Closing legal", "category": "closing_cost", "value": 1000, "settlement": "withheld"},
            {"id": str(uuid4()), "label": "Servicing", "category": "servicing", "value": 100, "timing": "monthly", "settlement": "separate"}]}
    plan = {**deepcopy(env.profile.financial_plan), "expected_updated_at": env.profile.financial_plan.get("updated_at")}
    return {"expected_version": 0, "expected_collateral_revision": 0, "expected_deal_budget_revision": 0,
        "expected_plan_updated_at": env.profile.financial_plan.get("updated_at"), "financial_plan": plan,
        "idempotency_key": "dealer-combined-save-1", "deal_budget": costs,
        "funding_party_kind": "Lender", "funding_party_name": "Synthetic lender", "facility_type": "Term loan",
        "approved_amount": 100000, "min_activation_amount": 1, "rate_pct": 12, "term_months": 24,
        "repayment_structure": "interest_only_then_amortizing", "interest_only_months": 6,
        "payment_frequency": "custom", "payments_per_year": 51.5,
        "repayment_config": {"amortization_months": 60, "after_balloon": "recast", "balloons": [{"id": "partial", "month": 12, "type": "percent_balance", "value": 15}]},
        "debt_service_treatment": "refinance", "retained_annual_debt_service": 0,
        "extra": {"staff_metadata": "Keep this context"}, "notes": "Reviewed client terms", **changes}


@pytest.mark.asyncio
async def test_one_save_freezes_solved_principal_and_mirrors_schedule_plan_and_costs(env):
    body = payload(env)
    original = deepcopy(body)
    row, child = await sheets.record_sheet(env.db, profile=env.profile, user=env.actor, body=body, request=None)
    await env.db.commit()
    gross = 103061.22  # 2% rounds to 2,061.22; the client receives exactly 100,000.
    expected = build_schedule(gross, 12, 24, structure="amortizing", interest_only_months=6,
        periods_per_year=51.5, repayment_config=body["repayment_config"])
    assert child is None and row.version == 1 and float(row.approved_amount) == gross
    assert row.extra["deal_budget_revision"] == env.profile.deal_budget["revision"] == 1
    assert row.extra["deal_budget_calculation"]["net_client_cash"] == 100000
    assert row.extra["deal_budget_calculation"]["principal_solution"]["requires_apply"] is False
    assert row.extra["staff_metadata"] == "Keep this context" and row.notes == "Reviewed client terms"
    assert row.extra["repayment_config"] == expected["repayment_config"]
    assert row.monthly_debt_service == expected["coverage_payment_monthly"]
    plan = env.profile.financial_plan
    assert plan["new_loan_amount"] == env.profile.underwriting_approved_amount == gross
    assert plan["new_loan_payments_per_year"] == 51.5 and plan["new_loan_interest_only_months"] == 6
    assert plan["retire"] == ["row:old"] and plan["exclude"] == ["payee:transfer"] and plan["note"] == "Keep payoff evidence"
    assert plan["add_backs"][0]["amount_monthly"] == 250
    assert plan["overrides"]["debt_service_monthly"] == 0
    assert row.extra["dscr_after"] == pytest.approx(row.extra["deal_budget_calculation"]["financing_fee_coverage"]["with_financing_fees_dscr"], abs=.001)
    assert all(call.args[-1] == gross for call in env.cap.await_args_list)
    assert body == original and env.db.commits == 1


@pytest.mark.asyncio
async def test_retry_does_not_create_versions_or_reapply_old_context(env):
    body = payload(env)
    first, _ = await sheets.record_sheet(env.db, profile=env.profile, user=env.actor, body=body, request=None)
    await env.db.commit()
    saved = deepcopy(env.profile.financial_plan)
    retried, _ = await sheets.record_sheet(env.db, profile=env.profile, user=env.actor, body=body, request=None)
    assert retried.id == first.id and env.profile.financial_plan == saved
    assert len(env.session.scalars(select(ProductionTermSheet)).all()) == 1
    assert env.profile.deal_budget["revision"] == 1
    with pytest.raises(HTTPException) as conflict:
        await sheets.record_sheet(env.db, profile=env.profile, user=env.actor, body={**body, "rate_pct": 13}, request=None)
    assert conflict.value.status_code == 409


@pytest.mark.asyncio
async def test_changed_costs_create_new_version_and_preserve_prior_frozen_values(env):
    body = payload(env)
    first, _ = await sheets.record_sheet(env.db, profile=env.profile, user=env.actor, body=body, request=None)
    await env.db.commit()
    original = deepcopy(first.extra)
    revised = payload(env, expected_version=1, expected_deal_budget_revision=1, idempotency_key="dealer-combined-save-2")
    revised["deal_budget"] = {**deepcopy(body["deal_budget"]), "expected_revision": 1}
    revised["deal_budget"]["rows"][0]["value"] = 3
    second, _ = await sheets.record_sheet(env.db, profile=env.profile, user=env.actor, body=revised, request=None)
    await env.db.commit()
    assert first.status == "superseded" and second.status == "current" and second.version == 2
    assert first.extra == original
    assert second.extra["deal_budget_revision"] == env.profile.deal_budget["revision"] == 2
    assert second.approved_amount > first.approved_amount
    assert second.extra["deal_budget_calculation"]["net_client_cash"] == 100000
    assert env.profile.financial_plan["new_loan_amount"] == float(second.approved_amount)
    assert len(env.session.scalars(select(ProductionTermSheet)).all()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("expected_version", 2), ("expected_collateral_revision", 9)])
async def test_stale_guards_precede_cost_mutation(env, field, value):
    before = deepcopy(env.profile.financial_plan)
    with pytest.raises(HTTPException) as error:
        await sheets.record_sheet(env.db, profile=env.profile, user=env.actor, body=payload(env, **{field: value}), request=None)
    assert error.value.status_code == 409
    assert env.profile.deal_budget is None and env.profile.financial_plan == before
    assert env.session.scalars(select(ProductionTermSheet)).all() == []


@pytest.mark.asyncio
async def test_projection_failure_rolls_back_budget_version_and_adoption(env, monkeypatch):
    before = deepcopy(env.profile.financial_plan)
    monkeypatch.setattr(routes, "_profile_access", AsyncMock(return_value=SimpleNamespace(profile=env.profile)))
    monkeypatch.setattr(routes.svc, "_load_family", AsyncMock())
    monkeypatch.setattr(routes.svc, "term_sheet_state", AsyncMock(return_value={}))
    monkeypatch.setattr(deal_budget, "read", AsyncMock(side_effect=RuntimeError("Synthetic projection failure")))
    with pytest.raises(RuntimeError, match="Synthetic projection failure"):
        await routes.record_term_sheet(env.profile.id, ProductionTermSheetBody.model_validate(payload(env)), None, env.actor, env.db)
    env.session.expire_all()
    assert env.db.commits == 0 and env.db.rollbacks == 1
    assert env.profile.financial_plan == before and env.profile.deal_budget is None
    assert env.profile.underwriting_approved_amount is None
    assert env.session.scalars(select(ProductionTermSheet)).all() == []


@pytest.mark.asyncio
async def test_combined_route_acknowledges_saved_revisions_before_single_commit(env, monkeypatch):
    monkeypatch.setattr(routes, "_profile_access", AsyncMock(return_value=SimpleNamespace(profile=env.profile)))
    monkeypatch.setattr(routes.svc, "_load_family", AsyncMock())
    monkeypatch.setattr(routes.svc, "term_sheet_state", AsyncMock(return_value={}))

    async def projection(_db, profile, _user):
        assert env.db.commits == 0
        return DealBudgetRead(profile_id=profile.id, revision=profile.deal_budget["revision"],
            financial_plan={"profile_id": str(profile.id), "record": deepcopy(profile.financial_plan)},
            calculation={"gross_loan_amount": profile.financial_plan["new_loan_amount"]})

    monkeypatch.setattr(deal_budget, "read", projection)
    result = await routes.record_term_sheet(env.profile.id, ProductionTermSheetBody.model_validate(payload(env)), None, env.actor, env.db)
    assert result.deal_budget.revision == 1 and result.financial_plan.record == env.profile.financial_plan
    assert result.financial_plan.record["updated_at"]
    assert result.deal_budget.calculation["gross_loan_amount"] == result.financial_plan.record["new_loan_amount"]
    assert env.db.commits == 1 and env.db.rollbacks == 0
    env.readiness.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["plan", "budget"])
async def test_concurrent_budget_or_plan_edit_is_not_replaced(env, changed):
    body = payload(env)
    if changed == "plan":
        env.profile.financial_plan = {**env.profile.financial_plan, "updated_at": datetime.now(UTC).isoformat(), "note": "Concurrent review"}
    else:
        env.profile.deal_budget = {"revision": 2, "rows": []}
    await env.db.commit()
    before = deepcopy((env.profile.financial_plan, env.profile.deal_budget))
    with pytest.raises(HTTPException) as error:
        await sheets.record_sheet(env.db, profile=env.profile, user=env.actor, body=body, request=None)
    assert error.value.status_code == 409
    assert (env.profile.financial_plan, env.profile.deal_budget) == before
    assert env.session.scalars(select(ProductionTermSheet)).all() == []


@pytest.mark.asyncio
async def test_independent_configured_draw_changes_funded_principal_not_approved_limit(env):
    body = payload(env, approved_amount=200000, facility_type="Revolving LOC", facility_kind="revolving_loc",
        initial_draw_amount=50000, payment_basis_amount=50000, repayment_structure="revolving_interest_only",
        interest_only_months=24, repayment_config={"after_balloon": "keep_payment", "balloons": []})
    body["deal_budget"].update(target_net_amount=50000, rows=[{"id": str(uuid4()), "label": "Financed closing", "category": "closing_cost", "value": 1000, "settlement": "financed"}])
    row, _ = await sheets.record_sheet(env.db, profile=env.profile, user=env.actor, body=body, request=None)
    assert float(row.approved_amount) == 200000
    assert row.extra["payment_basis_amount"] == row.extra["initial_draw_amount"] == 51000
    assert env.profile.financial_plan["new_loan_amount"] == 51000
    assert row.extra["deal_budget_calculation"]["net_client_cash"] == 50000


@pytest.mark.asyncio
@pytest.mark.parametrize("add_cost", [False, True])
async def test_unconfigured_combined_draw_uses_funded_principal_even_when_gross_is_unchanged(env, add_cost):
    body = payload(env, approved_amount=200000, facility_type="Revolving LOC", facility_kind="revolving_loc",
        initial_draw_amount=100000, payment_basis_amount=100000, repayment_structure="revolving_interest_only",
        interest_only_months=24, repayment_config=None)
    body["deal_budget"].update(target_net_amount=100000, rows=[{"id": str(uuid4()), "label": "Financed closing", "category": "closing_cost", "value": 1000, "settlement": "financed"}] if add_cost else [])
    row, _ = await sheets.record_sheet(env.db, profile=env.profile, user=env.actor, body=body, request=None)
    gross = 101000 if add_cost else 100000
    assert float(row.approved_amount) == env.profile.underwriting_approved_amount == 200000
    assert row.extra["payment_basis_amount"] == row.extra["initial_draw_amount"] == gross
    assert env.profile.financial_plan["new_loan_amount"] == gross
    assert financial_cockpit.term_sheet_terms(row)["amount"] == gross
    assert plan_terms_link.recorded_from_production_sheet(row)["amount"] == gross
    legacy = SimpleNamespace(**row.__dict__)
    legacy.extra = {key: value for key, value in row.extra.items() if key != "combined_funded_principal_v1"}
    assert financial_cockpit.term_sheet_terms(legacy)["amount"] == 200000
    assert plan_terms_link.recorded_from_production_sheet(legacy)["amount"] == 200000


@pytest.mark.asyncio
async def test_ambiguous_partial_draw_does_not_silently_change_approved_facility(env):
    body = payload(env, approved_amount=200000, initial_draw_amount=50000, payment_basis_amount=50000)
    body["deal_budget"].update(target_net_amount=50000)
    with pytest.raises(HTTPException, match="independently approved facility"):
        await sheets.record_sheet(env.db, profile=env.profile, user=env.actor, body=body, request=None)
    await env.db.rollback()
    assert env.profile.deal_budget is None and env.profile.underwriting_approved_amount is None
    assert env.session.scalars(select(ProductionTermSheet)).all() == []


def test_combined_schema_requires_all_concurrency_context(env):
    for key in ("idempotency_key", "financial_plan", "expected_plan_updated_at", "expected_collateral_revision", "expected_version"):
        value = payload(env)
        value.pop(key)
        with pytest.raises(ValidationError):
            ProductionTermSheetBody.model_validate(value)
