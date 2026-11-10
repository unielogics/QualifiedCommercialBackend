"""Real transactional rollback for one scenario/terms save; no provider calls."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    MetaData,
    Numeric,
    Table,
    Text,
    Uuid,
    create_engine,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, registry

from app.enums import Role
from app.models.application_terms import ApplicationTermSheet
from app.routers import application_terms as routes
from app.schemas.application_terms import ClientTermsWrite, LoanTypeOption
from app.services import application_terms, financial_cockpit, financial_plan


class DB:
    def __init__(self, session):
        self.session, self.statements = session, []
        self.commits, self.rollbacks, self.fail_commit = 0, 0, False

    @property
    def no_autoflush(self):
        return self.session.no_autoflush

    async def execute(self, statement):
        self.statements.append(statement)
        return self.session.execute(statement)

    async def refresh(self, row, **kwargs):
        self.session.refresh(row, **kwargs)

    async def flush(self):
        self.session.flush()

    def add(self, row):
        self.session.add(row)

    async def commit(self):
        if self.fail_commit:
            raise RuntimeError("fictional commit failure")
        self.session.commit()
        self.commits += 1

    async def rollback(self):
        self.session.rollback()
        self.rollbacks += 1


def payload(**extra):
    return ClientTermsWrite.model_validate({"expected_version": 0, "loan_type": "term", "amount": 100000,
        "apr_pct": 12, "term_months": 36, "funder_type": "bank", "funder_name": "Fictional lender",
        "repayment_frequency": "monthly", "loan_structure": "amortizing", "expiration_days": 7,
        "closing_estimate_days": 5, "conditions": ["Retain reviewed condition"], "client_note": "Retain client note", **extra})


def cockpit(profile, *, service=None, obligations=None):
    plan = financial_plan.normalize_plan(profile.financial_plan)
    obligations = obligations if obligations is not None else [{"id": "row:loan-a", "monthly": 1000, "payoff": 10000, "in_debt_service": True}]
    debt_service = service if service is not None else {"monthly": 3000}
    dscr = {"headline": {"key": "profit_and_loss", "numerator": 120000, "value": 3.33},
            "candidates": [{"key": "profit_and_loss", "numerator": 120000, "value": 3.33}]}
    evaluation = financial_plan.evaluate_plan(plan, obligations_list=obligations,
        recurring={"payees": [{"key": "transfer", "monthly_average": 500}]}, months=[],
        dscr=dscr, service=debt_service, proposed=None, requested_amount=None)
    return {"dscr": dscr, "debt": {"obligations": obligations, "service": debt_service},
            "plan": {"record": plan, "evaluation": evaluation, "levers": []}}


@pytest.fixture
def env(monkeypatch):
    # Use actual terms ORM writes/uniqueness and actual SQL transactions. Only
    # adapt PostgreSQL-specific DDL and replace unrelated evidence services.
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    table = ApplicationTermSheet.__table__.to_metadata(metadata)
    for constraint in list(table.constraints):
        if constraint in table.foreign_key_constraints or "jsonb_typeof" in str(getattr(constraint, "sqltext", "")):
            table.constraints.remove(constraint)
    table.foreign_keys.clear()
    for column in table.columns:
        column.foreign_keys.clear()
        if isinstance(column.type, JSONB):
            column.type = JSON()
    for index in table.indexes:
        if index.name == "uq_application_term_sheet_current":
            index.dialect_options["sqlite"]["where"] = index.dialect_options["postgresql"]["where"]
    profiles = Table("application_profiles", metadata, Column("id", Uuid, primary_key=True),
        Column("financial_plan", JSON), Column("investor_collateral", JSON), Column("deal_budget", JSON), Column("underwriting_term_sheet_amount", Numeric),
        Column("underwriting_current_dscr", Numeric), Column("underwriting_approved_dscr", Numeric),
        Column("underwriting_updated_by_user_id", Uuid), Column("underwriting_updated_at", DateTime(timezone=True)))
    mapper = registry()

    class Profile:
        primary_bucket_id = None

    mapper.map_imperatively(Profile, profiles)
    metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    profile = Profile()
    profile.id, profile.financial_plan = uuid4(), {}
    session.add(profile)
    session.commit()
    db = DB(session)
    service = {"monthly": 3000}
    obligations = [{"id": "row:loan-a", "monthly": 1000, "payoff": 10000, "in_debt_service": True}]
    actor = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    log = AsyncMock()
    monkeypatch.setattr(routes.profiles, "load_profile", AsyncMock(return_value=profile))
    monkeypatch.setattr(application_terms, "loan_type_options", AsyncMock(return_value=[LoanTypeOption(value="term", label="Fictional term")]))
    monkeypatch.setattr(application_terms.profiles, "log_profile_action", log)
    monkeypatch.setattr(application_terms, "_recipient_state", AsyncMock(return_value=(None, False)))
    monkeypatch.setattr(application_terms, "_plan_block", AsyncMock(return_value=None))

    async def context(_db, target):
        evaluated = cockpit(target, service=service, obligations=obligations)["plan"]["evaluation"]
        existing = evaluated["debt_service_before_new_payment_monthly"]
        return application_terms.DscrContext("business", 3.33,
            evaluated.get("income_after_annual") or 120000, "Fictional earnings",
            existing * 12 if existing is not None else None, None, "fixture")

    monkeypatch.setattr(application_terms, "_dscr_context", context)
    monkeypatch.setattr(financial_cockpit, "load_cockpit", AsyncMock(side_effect=lambda _db, target: cockpit(target, service=service, obligations=obligations)))
    result = SimpleNamespace(db=db, session=session, profile=profile, actor=actor, log=log, Profile=Profile,
                             service=service, obligations=obligations)
    try:
        yield result
    finally:
        session.close()
        engine.dispose()
        mapper.dispose()


async def baseline(env):
    response = await routes.put_client_terms(env.profile.id, payload(), env.actor, env.db)
    assert response.version == 1 and response.financial_plan is None
    return deepcopy(env.profile.financial_plan)


@pytest.mark.asyncio
@pytest.mark.parametrize("earnings", [-54_369, 0])
async def test_saved_terms_preserve_full_cockpit_provenance_and_signed_earnings(env, monkeypatch, earnings):
    # The generated source includes up to four complete plan explanations. It
    # legitimately exceeds the old varchar(200), even for short business names.
    label = "2025 filed business earnings reconciled to reviewed financial evidence and operating adjustments"
    changes = [f"Reviewed change {index}: " + "documented annual operating cost reconciliation " * 3 for index in range(4)]
    source_cockpit = {
        "dscr": {"headline": {"label": label, "numerator": earnings, "value": earnings / 36_000}},
        "debt": {"service": {"monthly": 3000}},
        "plan": {"evaluation": {"active": True, "income_after_annual": earnings,
                    "debt_service_before_new_payment_monthly": 2000},
                 "modifications": [{"kind": "add_back", "label": value} for value in changes]},
    }
    monkeypatch.setattr(financial_cockpit, "load_cockpit", AsyncMock(return_value=source_cockpit))
    context = await application_terms._cockpit_context(env.db, env.profile)
    assert context is not None and context.cash_flow == earnings
    assert len(context.source) > 200 and len(context.cash_flow_label) > 80
    monkeypatch.setattr(application_terms, "_dscr_context", AsyncMock(return_value=context))

    response = await routes.put_client_terms(env.profile.id, payload(), env.actor, env.db)
    env.session.expire_all()
    row = env.session.execute(select(ApplicationTermSheet)).scalar_one()
    assert row.dscr_source == context.source
    assert all(change in row.dscr_source for change in changes)
    assert row.cash_flow_label == context.cash_flow_label
    assert row.cash_flow_value == earnings
    assert float(row.dscr_before) == pytest.approx(round(earnings / 36_000, 4))
    assert response.calculation.source == context.source
    assert response.calculation.cash_flow_value == earnings
    assert response.calculation.dscr_after is not None
    assert response.calculation.dscr_after < 0 if earnings < 0 else response.calculation.dscr_after == 0
    # SQLite permits any varchar length, so also assert the production ORM
    # schema uses unbounded text instead of a limit that would fail PostgreSQL.
    assert isinstance(ApplicationTermSheet.__table__.c.dscr_source.type, Text)
    assert isinstance(ApplicationTermSheet.__table__.c.cash_flow_label.type, Text)


def combined(env, **extra):
    draft = {"expected_updated_at": env.profile.financial_plan.get("updated_at"), "retire": ["row:loan-a"],
        "exclude": ["payee:transfer"], "add_backs": [{"kind": "other", "amount_monthly": 500, "note": "Reviewed one-time cost"}],
        "overrides": {"income_annual": 144000, "income_note": "Staff-reviewed earnings",
                      "debt_service_monthly": 2500, "debt_service_note": "Staff-reviewed debt service"},
        "note": "Unsaved refinance scenario", "new_loan_amount": 200000, "new_loan_rate_pct": 29.99,
        "new_loan_term_months": 24, "new_loan_structure": "amortizing", "new_loan_repayment_config": {"amortization_months": 60}}
    return payload(expected_version=1, amount=200000, apr_pct=29.99, term_months=24,
        repayment_config={"amortization_months": 60}, financial_plan=draft, **extra)


@pytest.mark.asyncio
async def test_combined_save_preserves_unsaved_scenario_and_returns_authoritative_adoption(env):
    await baseline(env)
    before = env.db.commits
    result = await routes.put_client_terms(env.profile.id, combined(env), env.actor, env.db)
    assert env.db.commits == before + 1 and env.db.rollbacks == 0
    assert result.version == 2 and result.status == "draft" and result.issued_at is None
    assert result.conditions == ["Retain reviewed condition"] and result.client_note == "Retain client note"
    assert result.calculation.current_annual_debt_service == 18000
    record = result.financial_plan.record
    assert record == env.profile.financial_plan
    assert record["retire"] == ["row:loan-a"] and record["exclude"] == ["payee:transfer"]
    assert record["add_backs"][0]["amount_monthly"] == 500 and record["note"] == "Unsaved refinance scenario"
    assert record["overrides"]["income_annual"] == 144000 and record["overrides"]["debt_service_monthly"] == 2500
    assert record["terms_ref"]["id"] == str(result.term_sheet_id) and record["terms_ref"]["version"] == 2
    assert record["new_loan_repayment_config"]["amortization_months"] == 60 and record["updated_at"]
    assert result.financial_plan.evaluation["schedule"]["maturity_balloon"] == 152415.68
    rows = env.session.scalars(select(ApplicationTermSheet).order_by(ApplicationTermSheet.version)).all()
    assert [row.is_current for row in rows] == [False, True]
    assert all(row.issued_pdf_bytes is None for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("income", [-24000.5, 0, None])
async def test_combined_terms_version_persists_signed_staff_income_and_matching_coverage(env, monkeypatch, income):
    await baseline(env)
    # Exercise the actual cockpit-derived terms context, including a known zero
    # rather than the fixture's older truthy fallback to source earnings.
    monkeypatch.setattr(application_terms, "_dscr_context", application_terms._cockpit_context)
    body = combined(env).model_dump(mode="json")
    body["financial_plan"]["overrides"]["income_annual"] = income
    request = ClientTermsWrite.model_validate(body)
    result = await routes.put_client_terms(env.profile.id, request, env.actor, env.db)
    record, evaluated = result.financial_plan.record, result.financial_plan.evaluation
    expected = income if income is not None else 126000  # Source earnings plus the existing operating addback.
    assert result.version == 2 and record["overrides"]["income_annual"] == income
    assert record["overrides"]["income_note"] == "Staff-reviewed earnings"
    assert record["retire"] == ["row:loan-a"] and record["exclude"] == ["payee:transfer"]
    assert evaluated["income_after_annual"] == result.calculation.cash_flow_value == expected
    assert result.calculation.dscr_after == round(expected / result.calculation.projected_annual_debt_service, 3)
    assert evaluated["headline_after"] == round(expected / evaluated["debt_service_after_annual"], 2)
    env.session.expire(env.profile, ["financial_plan"])
    assert env.profile.financial_plan["overrides"]["income_annual"] == income
    versions = env.session.scalars(select(ApplicationTermSheet).order_by(ApplicationTermSheet.version)).all()
    assert len(versions) == 2 and versions[0].cash_flow_value == 120000
    assert versions[1].cash_flow_value == expected


@pytest.mark.asyncio
async def test_terms_only_save_refreshes_concurrently_saved_financial_scenario_before_adoption(env):
    stale = await baseline(env)
    fresh = {**deepcopy(stale), "retire": ["row:loan-a"], "exclude": ["payee:transfer"],
        "add_backs": [{"kind": "other", "amount_monthly": 500, "note": "Reviewed cost"}],
        "overrides": {"income_annual": 144000, "income_note": "Reviewed earnings"},
        "note": "Newer financial-plan-only save",
        "updated_at": (datetime.now(UTC) + timedelta(seconds=5)).isoformat()}
    env.session.execute(update(env.Profile).where(env.Profile.id == env.profile.id).values(financial_plan=fresh)
                        .execution_options(synchronize_session=False))
    env.session.commit()
    assert env.profile.financial_plan == stale

    result = await routes.put_client_terms(env.profile.id, payload(expected_version=1, amount=200000), env.actor, env.db)
    persisted = env.profile.financial_plan
    assert result.version == 2 and result.financial_plan is None
    normalized_fresh = financial_plan.normalize_plan(fresh)
    for key in ("retire", "exclude", "add_backs", "note"):
        assert persisted[key] == normalized_fresh[key]
    assert persisted["overrides"]["income_annual"] == 144000
    assert persisted["terms_ref"]["version"] == 2
    assert result.calculation.current_annual_debt_service == 24000
    assert result.calculation.cash_flow_value == 144000


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", ["plan", "terms", "missing_plan_revision"])
async def test_stale_versions_are_rejected_before_either_resource_changes(env, stale):
    original = await baseline(env)
    request = combined(env)
    if stale == "terms":
        request.expected_version = 0
    elif stale == "missing_plan_revision":
        request.financial_plan.model_fields_set.discard("expected_updated_at")
    else:
        # Keep the loaded instance stale to prove the profile refresh reads the
        # concurrently committed revision under the shared terms/profile lock.
        changed = {**original, "updated_at": (datetime.now(UTC) + timedelta(seconds=5)).isoformat()}
        env.session.execute(update(env.Profile).where(env.Profile.id == env.profile.id).values(financial_plan=changed)
                            .execution_options(synchronize_session=False))
        env.session.commit()
        original = changed
    env.log.reset_mock()
    with pytest.raises(HTTPException) as error:
        await routes.put_client_terms(env.profile.id, request, env.actor, env.db)
    assert error.value.status_code == (422 if stale == "missing_plan_revision" else 409)
    assert env.session.get(env.Profile, env.profile.id).financial_plan == original
    rows = env.session.scalars(select(ApplicationTermSheet)).all()
    assert len(rows) == 1 and rows[0].is_current
    env.log.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["save", "response", "commit"])
async def test_failure_after_writes_rolls_back_terms_version_and_plan_together(env, monkeypatch, stage):
    original = await baseline(env)
    request = combined(env)
    if stage == "save":
        async def log(_db, _profile, _user, kind, *_args, **_kwargs):
            if kind == "term_sheet.saved":
                raise RuntimeError("fictional scenario-save failure after terms/adoption flush")
        env.log.side_effect = log
    elif stage == "response":
        monkeypatch.setattr(financial_cockpit, "load_cockpit", AsyncMock(side_effect=RuntimeError("fictional response failure")))
    else:
        env.db.fail_commit = True
    with pytest.raises(RuntimeError, match="fictional"):
        await routes.put_client_terms(env.profile.id, request, env.actor, env.db)
    assert env.db.rollbacks == 1
    assert env.session.get(env.Profile, env.profile.id).financial_plan == original
    rows = env.session.scalars(select(ApplicationTermSheet)).all()
    assert len(rows) == 1 and rows[0].version == 1 and rows[0].is_current and rows[0].status == "draft"


@pytest.mark.asyncio
async def test_lost_response_retry_cannot_create_another_version(env):
    await baseline(env)
    request = combined(env)
    result = await routes.put_client_terms(env.profile.id, request, env.actor, env.db)
    saved = deepcopy(result.financial_plan.record)
    with pytest.raises(HTTPException) as error:
        await routes.put_client_terms(env.profile.id, request, env.actor, env.db)
    assert error.value.status_code == 409
    assert env.session.get(env.Profile, env.profile.id).financial_plan == saved
    assert len(env.session.scalars(select(ApplicationTermSheet)).all()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_combined_refinance_keeps_selected_evidence_without_retiring_retained_service_twice(env, configured):
    await baseline(env)
    request = combined(env)
    request.debt_service_treatment = "refinance"
    request.retained_annual_debt_service = 12000
    if not configured:
        request.repayment_config = None
        request.financial_plan.new_loan_repayment_config = None
    result = await routes.put_client_terms(env.profile.id, request, env.actor, env.db)
    record, evaluated = result.financial_plan.record, result.financial_plan.evaluation
    assert record["retire"] == ["row:loan-a"]
    assert record["overrides"]["debt_service_monthly"] == 1000
    assert record["overrides"]["income_annual"] == 144000
    assert evaluated["debt_service_before_new_payment_monthly"] == 1000
    assert evaluated["debt_service_after_annual"] == pytest.approx(result.calculation.annual_debt_service + 12000, abs=.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("treatment", ["additive", "refinance"])
async def test_combined_save_preserves_unknown_debt_until_retained_service_is_explicit(env, configured, treatment):
    await baseline(env)
    env.service["monthly"] = None
    request = combined(env)
    request.financial_plan.overrides.debt_service_monthly = None
    if not configured:
        request.repayment_config = None
        request.financial_plan.new_loan_repayment_config = None
    if treatment == "refinance":
        request.debt_service_treatment = treatment
        request.retained_annual_debt_service = 0
    result = await routes.put_client_terms(env.profile.id, request, env.actor, env.db)
    evaluated = result.financial_plan.evaluation
    assert result.calculation.current_annual_debt_service is None
    assert result.calculation.projected_annual_debt_service == evaluated["debt_service_after_annual"]
    assert result.financial_plan.record["retire"] == ["row:loan-a"]
    assert evaluated["new_payment_monthly"] is not None
    if treatment == "additive":
        assert result.calculation.dscr_after is None
        assert evaluated["debt_service_before_new_payment_monthly"] is None
        assert evaluated["debt_service_after_monthly"] is None and evaluated["risk"] is None
        assert not evaluated["no_debt_left"]
        assert evaluated["coverage_unavailable_reason"] == financial_plan.UNKNOWN_DEBT_SERVICE_REASON
    else:
        assert result.calculation.dscr_after is not None
        assert evaluated["debt_service_before_new_payment_monthly"] == 0
        assert evaluated["coverage_unavailable_reason"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_combined_terms_real_context_does_not_restore_raw_debt_after_unknown_retirement(env, monkeypatch, configured):
    await baseline(env)
    env.obligations[0]["monthly"] = None
    monkeypatch.setattr(application_terms, "_dscr_context", application_terms._cockpit_context)
    request = combined(env)
    if not configured:
        request.repayment_config = None
        request.financial_plan.new_loan_repayment_config = None
    result = await routes.put_client_terms(env.profile.id, request, env.actor, env.db)
    assert env.service["monthly"] == 3000
    assert result.financial_plan.record["overrides"]["debt_service_monthly"] == 2500
    assert result.calculation.cash_flow_value == 144000
    assert result.calculation.current_annual_debt_service is None
    assert result.calculation.projected_annual_debt_service is None and result.calculation.dscr_after is None
    assert result.financial_plan.evaluation["debt_service_after_annual"] is None
    assert result.financial_plan.evaluation["headline_after"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("next_treatment,retained", [("additive", None), ("refinance", 18000)])
async def test_combined_treatment_transition_snapshots_same_scenario_as_final_plan(env, next_treatment, retained):
    await baseline(env)
    initial = combined(env)
    initial.debt_service_treatment, initial.retained_annual_debt_service = "refinance", 12000
    await routes.put_client_terms(env.profile.id, initial, env.actor, env.db)
    draft = {**deepcopy(env.profile.financial_plan), "expected_updated_at": env.profile.financial_plan["updated_at"]}
    request = ClientTermsWrite.model_validate({**initial.model_dump(), "expected_version": 2,
        "debt_service_treatment": next_treatment, "retained_annual_debt_service": retained, "financial_plan": draft})
    result = await routes.put_client_terms(env.profile.id, request, env.actor, env.db)
    record, evaluated = result.financial_plan.record, result.financial_plan.evaluation
    assert record["retire"] == ["row:loan-a"] and record["overrides"]["income_annual"] == 144000
    expected_existing = 2000 if next_treatment == "additive" else 1500
    assert evaluated["debt_service_before_new_payment_monthly"] == expected_existing
    assert result.calculation.projected_annual_debt_service == evaluated["debt_service_after_annual"]
    assert result.calculation.cash_flow_value == evaluated["income_after_annual"]
    assert result.calculation.dscr_after == pytest.approx(evaluated["headline_after"], abs=.01)
    if next_treatment == "additive":
        assert record["overrides"]["debt_service_monthly"] is None
        assert record["overrides"]["debt_service_note"] is None
    else:
        assert record["overrides"]["debt_service_monthly"] == 1500


@pytest.mark.parametrize("change,expected", [({}, 1000),
    ({"new_payment_source": "manual_terms"}, 0),
    ({"overrides": {"debt_service_monthly": 1000, "debt_service_note": "Staff basis"}}, 0),
    ({"overrides": {"debt_service_monthly": 1500, "debt_service_note": "Refinance per terms"}}, 500),
    ({"terms_ref": {"id": "terms", "treatment": "refinance", "retained_annual_debt_service": None}}, 0),
])
def test_legacy_recorded_refinance_basis_keeps_manual_override_and_source_guards(change, expected):
    plan = {"new_loan_amount": 100000, "new_loan_rate_pct": 12, "new_loan_term_months": 36,
        "new_loan_structure": "amortizing", "new_payment_source": "recorded_terms", "retire": ["row:loan-a"],
        "terms_ref": {"id": "terms", "treatment": "refinance", "retained_annual_debt_service": 12000},
        "overrides": {"debt_service_monthly": 1000, "debt_service_note": "Refinance per terms"}, **change}
    assert cockpit(SimpleNamespace(financial_plan=plan))["plan"]["evaluation"]["debt_service_before_new_payment_monthly"] == expected


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("recorded_basis", [False, True])
def test_recorded_retained_service_remains_known_when_selected_old_payment_is_unknown(configured, recorded_basis):
    retained = 12345.67
    plan = financial_plan.normalize_plan({"new_loan_amount": 100000, "new_loan_rate_pct": 12,
        "new_loan_term_months": 24, "new_loan_structure": "amortizing",
        "new_loan_repayment_config": {"amortization_months": 60} if configured else None,
        "new_payment_source": "recorded_terms" if recorded_basis else "manual_terms",
        "retire": ["row:old"],
        "terms_ref": {"id": "terms", "treatment": "refinance", "retained_annual_debt_service": retained},
        "overrides": {"debt_service_monthly": round(retained / 12, 2), "debt_service_note": "Refinance per terms"}
        if recorded_basis else None})
    result = financial_plan.evaluate_plan(plan,
        obligations_list=[{"id": "row:old", "monthly": None, "payoff": 10000, "in_debt_service": True}],
        recurring={}, months=[], dscr={"headline": {"key": "profit_and_loss", "numerator": 120000},
            "candidates": [{"key": "profit_and_loss", "numerator": 120000}]},
        service={"monthly": None}, proposed=None, requested_amount=None)
    if recorded_basis:
        assert result["debt_service_before_new_payment_monthly"] == 1028.81
        assert result["headline_after"] is not None and result["coverage_unavailable_reason"] is None
        annual_new = result["schedule"]["coverage_annual_debt_service"] if configured else result["new_payment_monthly"] * 12
        assert result["debt_service_after_annual"] - annual_new == pytest.approx(retained if configured else 12345.72, abs=.001)
    else:
        assert result["debt_service_before_new_payment_monthly"] is None
        assert result["debt_service_after_annual"] is None and result["headline_after"] is None
        assert result["coverage_unavailable_reason"] == financial_plan.UNKNOWN_DEBT_SERVICE_REASON


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("change", [
    {"overrides": {"debt_service_monthly": 0, "debt_service_note": "Staff basis"}},
    {"overrides": {"debt_service_monthly": 1500, "debt_service_note": "Staff basis"}},
    {"new_payment_source": "manual_terms"},
    {"overrides": {"debt_service_monthly": 1000, "debt_service_note": "Staff basis"}},
    {"overrides": {"debt_service_monthly": 1500, "debt_service_note": "Refinance per terms"}},
])
def test_unknown_selected_retirement_cannot_be_subtracted_from_unrelated_known_service(configured, change):
    plan = financial_plan.normalize_plan({"new_loan_amount": 100000, "new_loan_rate_pct": 12,
        "new_loan_term_months": 24, "new_loan_structure": "amortizing",
        "new_loan_repayment_config": {"amortization_months": 60} if configured else None,
        "new_payment_source": "recorded_terms", "retire": ["row:old"],
        "terms_ref": {"id": "terms", "treatment": "refinance", "retained_annual_debt_service": 12000},
        "overrides": {"debt_service_monthly": 1000, "debt_service_note": "Refinance per terms"}, **change})
    result = financial_plan.evaluate_plan(plan,
        obligations_list=[{"id": "row:old", "monthly": None, "payoff": 10000, "in_debt_service": True}],
        recurring={}, months=[], dscr={"headline": {"key": "profit_and_loss", "numerator": 120000},
            "candidates": [{"key": "profit_and_loss", "numerator": 120000}]},
        service={"monthly": 3000}, proposed=None, requested_amount=None)
    assert result["debt_service_before_monthly"] is not None
    assert result["debt_service_before_new_payment_monthly"] is None
    assert result["debt_service_after_monthly"] is None and result["debt_service_after_annual"] is None
    assert result["headline_after"] is None and result["risk"] is None and not result["no_debt_left"]
    assert result["coverage_unavailable_reason"] == financial_plan.UNKNOWN_DEBT_SERVICE_REASON
    levers = financial_plan.coverage_levers(plan, result,
        obligations_list=[{"id": "row:old", "monthly": None, "payoff": 10000, "in_debt_service": True}],
        recurring={}, dscr={"headline": {"key": "profit_and_loss", "numerator": 120000}}, requested_amount=100000,
        terms={"monthly_payment": 1000, "amount": 100000, "rate_pct": 12, "term_months": 24, "treatment": "additive"})
    assert len(levers) == 1 and levers[0]["key"] == "debt_schedule"
    assert levers[0]["dscr_after"] is None and levers[0]["apply"] is None and not levers[0]["improves"]


def test_unknown_selected_payment_outside_debt_service_does_not_block_coverage():
    result = financial_plan.evaluate_plan(financial_plan.normalize_plan({"retire": ["row:other"], "new_payment_monthly": 500}),
        obligations_list=[{"id": "row:other", "monthly": None, "in_debt_service": False}], recurring={}, months=[],
        dscr={"headline": {"key": "profit_and_loss", "numerator": 120000},
              "candidates": [{"key": "profit_and_loss", "numerator": 120000}]},
        service={"monthly": 1500}, proposed=None, requested_amount=None)
    assert result["debt_service_before_new_payment_monthly"] == 1500
    assert result["debt_service_after_monthly"] == 2000 and result["headline_after"] == 5
    assert result["coverage_unavailable_reason"] is None


@pytest.mark.asyncio
async def test_initial_combined_plan_revision_must_be_explicit_null_and_role_gate_is_unchanged(env):
    request = payload(financial_plan={"expected_updated_at": None})
    result = await routes.put_client_terms(env.profile.id, request, env.actor, env.db)
    assert result.version == 1 and result.financial_plan.record["terms_ref"]["version"] == 1
    with pytest.raises(HTTPException) as denied:
        await routes.put_client_terms(env.profile.id, request, SimpleNamespace(role=Role.BROKER), env.db)
    assert denied.value.status_code == 403


def test_combined_refinance_never_defaults_unknown_retained_service_to_zero():
    with pytest.raises(ValidationError, match="annual debt service"):
        payload(debt_service_treatment="refinance", retained_annual_debt_service=None,
                financial_plan={"expected_updated_at": None})
