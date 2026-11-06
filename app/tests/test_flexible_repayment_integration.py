"""Granular terms survive every versioned boundary and use actual cash due."""

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.enums import Role
from app.schemas.application_terms import ClientTermsWrite, LoanTypeOption
from app.schemas.financial_cockpit import FinancialPlanWrite
from app.schemas.production_package import ProductionTermSheetBody
from app.services import application_terms as client_terms
from app.services import financial_cockpit, financial_plan, plan_terms_link
from app.services import production_term_sheets as sheets
from app.services import production_term_structure as production
from app.services.loan_schedule import build_schedule

CONFIG = {"amortization_months": 120, "after_balloon": "recast", "balloons": [
    {"id": "partial", "month": 6, "type": "percent_balance", "value": 20},
    {"id": "fixed", "month": 18, "type": "amount", "value": 5000},
]}


def client_payload(**extra):
    return ClientTermsWrite.model_validate({"expected_version": 0, "loan_type": "term", "amount": 100000, "apr_pct": 12,
        "term_months": 36, "funder_type": "bank", "repayment_frequency": "monthly", "expiration_days": 7,
        "closing_estimate_days": 5, "loan_structure": "amortizing", "repayment_config": deepcopy(CONFIG), **extra})


def production_body(**extra):
    return {"funding_party_kind": "Lender", "funding_party_name": "Example lender", "facility_type": "Term loan",
        "approved_amount": 100000, "min_activation_amount": 1, "rate_pct": 12, "term_months": 36,
        "repayment_structure": "fully_amortizing", "payment_frequency": "monthly", "repayment_config": deepcopy(CONFIG), **extra}


def plan_for(config=CONFIG, **extra):
    return financial_plan.normalize_plan({"new_loan_amount": 100000, "new_loan_rate_pct": 12,
        "new_loan_term_months": 36, "new_loan_structure": "amortizing", "new_loan_repayment_config": deepcopy(config), **extra})


def evaluate(plan):
    return financial_plan.evaluate_plan(plan, obligations_list=[], recurring={}, months=[],
        dscr={"headline": {"key": "profit_and_loss", "value": 10, "numerator": 120000},
              "candidates": [{"key": "profit_and_loss", "numerator": 120000, "value": 10}]},
        service={"monthly": 1000}, proposed=None, requested_amount=None)


@pytest.mark.parametrize("frequency,ppy", [("monthly", 12), ("weekly", 51.96), ("daily", 252)])
def test_client_quote_and_adopted_plan_use_identical_cadence_and_cash_due(frequency, ppy):
    expected = build_schedule(100000, 12, 36, structure="amortizing", periods_per_year=ppy, repayment_config=CONFIG)
    payment, count, periods, annual, total, cost = client_terms.repayment_math(amount=100000, apr_pct=12,
        term_months=36, frequency=frequency, loan_structure="amortizing", repayment_config=CONFIG)
    assert annual == expected["coverage_annual_debt_service"] and total == expected["total_paid"]
    assert cost == pytest.approx(total - 100000, abs=.01)
    row = SimpleNamespace(id=uuid4(), version=2, program_name="Term", amount=100000, apr_pct=12,
        term_months=36, loan_structure="amortizing", interest_only_months=0, payments_per_year=periods,
        periodic_payment=payment, annual_new_debt_service=annual, debt_service_treatment="additive", repayment_config=CONFIG)
    recorded = plan_terms_link.recorded_from_client_terms(row)
    plan = financial_plan.normalize_plan(plan_terms_link.adopt({}, recorded))
    result = evaluate(plan)
    assert plan["new_loan_payments_per_year"] == periods
    assert result["new_payment_monthly"] == expected["coverage_payment_monthly"]
    assert result["schedule"]["total_paid"] == total
    assert result["schedule"]["scheduled_balloons"] == expected["scheduled_balloons"]
    assert plan_terms_link.terms_link(plan, result, recorded)["state"] == "mirrored"


def test_plan_manual_regular_payment_cannot_remove_balloon_coverage_and_replay_is_labelled():
    plan = plan_for(new_payment_monthly=1)
    result = evaluate(plan)
    assert result["new_payment_monthly"] == result["schedule"]["coverage_payment_monthly"]
    assert result["new_payment_monthly"] > result["actual_regular_payment_monthly"]
    assert result["replay_basis"] == "coverage_payment_proxy" and "No loan commencement" in result["replay_note"]


def test_config_change_reports_drift_even_when_legacy_scalar_terms_match():
    plan = plan_for()
    result = evaluate(plan)
    recorded = {"label": "Terms", "amount": 100000, "rate_pct": 12, "term_months": 36,
        "structure": "amortizing",
        "interest_only": False, "interest_only_months": 0, "payment_monthly": result["new_payment_monthly"],
        "payments_per_year": 12, "repayment_config": {**CONFIG, "after_balloon": "keep_payment"}}
    assert plan_terms_link.terms_link(plan, result, recorded)["differences"] == ["repayment schedule"]
    released = plan_terms_link.release({**plan, "terms_ref": {"id": "terms"}}, "terms")
    assert released["new_loan_repayment_config"] is None and released["new_loan_payments_per_year"] is None


@pytest.mark.parametrize("repayment,kind", [("fully_amortizing", "amortizing"), ("simple_interest", "simple_interest")])
def test_production_bridge_matches_shared_engine_and_preserves_extra_without_persisting_rows(repayment, kind):
    raw = production_body(repayment_structure=repayment, extra={"custom_label": "Retain me"})
    before = deepcopy(raw)
    values = production.calculate(raw)
    expected = build_schedule(100000, 12, 36, structure=kind, repayment_config=CONFIG)
    assert raw == before
    assert values["total_repayment"] == expected["total_paid"]
    assert values["annual_debt_service"] == expected["coverage_annual_debt_service"]
    assert values["monthly_program_coverage_amount"] == expected["coverage_payment_monthly"]
    assert values["periodic_payment"] == expected["payment_after"]
    assert values["balloon_events"] == expected["scheduled_balloons"]
    stored = production.stored_extra(raw["extra"], values)
    assert stored["custom_label"] == "Retain me" and stored["repayment_config"] == CONFIG
    assert "schedule" not in stored and "rows" not in stored
    assert production.public_values({**raw, "extra": stored})["schedule"]["total_paid"] == expected["total_paid"]
    assert "rows" not in values["schedule"] and "cash_flows" not in values["schedule"]


def test_production_variable_rate_and_nonmonthly_cadence_are_exact_on_adoption():
    raw = production_body(payment_frequency="weekly", rate_structure="variable", rate_index="Prime",
        rate_index_rate_pct=7, rate_margin_pct=2, rate_as_of="2026-11-06")
    values = production.calculate(raw)
    expected = build_schedule(100000, 9, 36, structure="amortizing", periods_per_year=51.96, repayment_config=CONFIG)
    assert values["schedule"]["total_paid"] == expected["total_paid"]
    sheet = SimpleNamespace(id=uuid4(), version=1, funding_party_name="Lender", approved_amount=100000,
        rate_pct=9, term_months=36, monthly_debt_service=values["monthly_program_coverage_amount"],
        extra=production.stored_extra({}, values))
    recorded = plan_terms_link.recorded_from_production_sheet(sheet)
    plan = financial_plan.normalize_plan(plan_terms_link.adopt({}, recorded))
    result = evaluate(plan)
    assert result["schedule"]["total_paid"] == expected["total_paid"]
    assert plan_terms_link.terms_link(plan, result, recorded)["state"] == "mirrored"


def test_configured_zero_regular_payment_preserves_maturity_cash_and_positive_coverage():
    config = {"after_balloon": "keep_payment", "balloons": []}
    quote = client_terms.repayment_math(amount=100000, apr_pct=0, term_months=12, frequency="monthly",
                                       loan_structure="interest_only", repayment_config=config)
    assert quote[0] == 0 and quote[3] == 100000 and quote[4] == 100000
    values = production.calculate(production_body(rate_pct=0, term_months=12, repayment_structure="interest_only", repayment_config=config))
    assert values["periodic_payment"] == 0 and values["monthly_equivalent_payment"] == 0
    assert values["monthly_program_coverage_amount"] == 8333.33 and values["maturity_balance"] == 100000
    assert not production.validation_errors(values)


@pytest.mark.parametrize("invalid", [
    {"balloons": [{"id": "bad", "month": 37, "type": "amount", "value": 100}]},
    {"balloons": [{"id": "bad", "month": 6, "type": "percent_balance", "value": 101}]},
    {"balloons": [{"id": "bad", "month": 6, "type": "amount", "value": True}]},
    {"balloons": [{"id": "bad", "month": 6, "type": "amount", "value": 999999}]},
])
def test_api_writes_reject_invalid_or_unpayable_balloon_events(invalid):
    with pytest.raises(ValidationError):
        ClientTermsWrite.model_validate({**client_payload().model_dump(), "repayment_config": invalid})
    with pytest.raises(ValidationError):
        FinancialPlanWrite(new_loan_amount=100000, new_loan_rate_pct=12, new_loan_term_months=36,
                           new_loan_structure="amortizing", new_loan_repayment_config=invalid)
    with pytest.raises(ValueError):
        production.calculate(production_body(repayment_config=invalid))


def test_production_extra_roundtrip_and_explicit_null_clear_are_distinct():
    restored = ProductionTermSheetBody.model_validate(production_body(repayment_config=None, extra={"repayment_config": CONFIG}))
    assert restored.repayment_config is None
    raw = production_body(extra={"repayment_config": CONFIG})
    raw.pop("repayment_config")
    assert ProductionTermSheetBody.model_validate(raw).repayment_config.model_dump(exclude_none=True) == CONFIG


def test_production_fixed_custom_overrides_cannot_hide_configured_cashflows():
    for extra in ({"repayment_structure": "fixed_payment"}, {"repayment_structure": "custom"}, {"lender_payment_override": True}):
        with pytest.raises(ValueError, match="override"):
            production.calculate(production_body(**extra))


def test_cockpit_proposed_schedule_and_plan_keep_events_without_touching_ebitda():
    from app.tests.test_financial_cockpit import LOAN_ROW, SIX_MONTHS, TAX_2025

    baseline = financial_cockpit.build_cockpit([*SIX_MONTHS, TAX_2025], [LOAN_ROW])
    planned = financial_cockpit.build_cockpit([*SIX_MONTHS, TAX_2025], [LOAN_ROW], plan=plan_for())
    assert planned["ebitda"] == baseline["ebitda"]
    assert planned["dscr"] == baseline["dscr"]
    proposed = planned["proposed_terms"]
    assert proposed["repayment_config"] == CONFIG and proposed["schedule"]["scheduled_balloons"]
    assert any(row["scheduled_balloon"] for row in proposed["schedule_rows"])
    assert proposed["risk"]["maturity_balloon"] > 0


class SaveDB:
    def __init__(self):
        self.rows = []
        result = Mock()
        result.scalar_one_or_none.return_value = None
        result.scalars.return_value.all.return_value = []
        self.execute = AsyncMock(return_value=result)

    def add(self, row):
        self.rows.append(row)

    async def flush(self):
        for row in self.rows:
            if hasattr(row, "id") and row.id is None:
                row.id = uuid4()


@pytest.mark.asyncio
@pytest.mark.parametrize("frequency,custom", [("monthly", None), ("custom", 51.5)])
async def test_versioned_client_save_read_and_adoption_preserve_config_and_actual_total(frequency, custom):
    db = SaveDB()
    profile = SimpleNamespace(id=uuid4(), financial_plan={}, primary_bucket_id=None)
    actor = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    payload = client_payload(repayment_frequency=frequency, custom_payments_per_year=custom)
    context = client_terms.DscrContext("business", 5, 120000, "Annual earnings", 24000, None, "fixture")
    with (patch.object(client_terms, "current_term_sheet", AsyncMock(return_value=None)),
          patch.object(client_terms, "loan_type_options", AsyncMock(return_value=[LoanTypeOption(value="term", label="Term")])),
          patch.object(client_terms, "_dscr_context", AsyncMock(return_value=context)),
          patch.object(client_terms.profiles, "log_profile_action", AsyncMock()),
          patch.object(client_terms, "_recipient_state", AsyncMock(return_value=(None, False))),
          patch.object(client_terms, "_plan_block", AsyncMock(return_value=None))):
        row = await client_terms.save_terms(db, profile, actor, payload)
        row.updated_at = datetime.now(UTC)
        read = await client_terms.read_terms(db, profile, row)
    expected = build_schedule(100000, 12, 36, structure="amortizing", periods_per_year=custom or 12, repayment_config=CONFIG)
    assert row.repayment_config == CONFIG and read.repayment_config == CONFIG
    assert read.calculation.total_repayment == expected["total_paid"]
    assert read.calculation.total_repayment != row.periodic_payment * row.payment_count
    assert read.calculation.schedule["scheduled_balloons"] == expected["scheduled_balloons"]
    assert profile.financial_plan["new_loan_repayment_config"] == CONFIG
    assert read.custom_payments_per_year == custom


@pytest.mark.asyncio
async def test_stale_client_and_production_versions_fail_before_replacing_terms():
    db = SaveDB()
    profile = SimpleNamespace(id=uuid4())
    actor = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    with (patch.object(client_terms, "current_term_sheet", AsyncMock(return_value=SimpleNamespace(version=3))),
          patch.object(client_terms, "loan_type_options", AsyncMock(return_value=[]))):
        with pytest.raises(HTTPException) as error:
            await client_terms.save_terms(db, profile, actor, client_payload())
    assert error.value.status_code == 409 and not db.rows
    with patch.object(sheets, "current_sheet", AsyncMock(return_value=SimpleNamespace(version=3))):
        with pytest.raises(HTTPException) as error:
            await sheets.record_sheet(db, profile=profile, user=actor, body=production_body(expected_version=2), request=None)
    assert error.value.status_code == 409 and not db.rows


@pytest.mark.asyncio
@pytest.mark.parametrize("zero_regular", [False, True])
async def test_recorded_production_version_preserves_schedule_and_adopts_exact_coverage(zero_regular):
    db = SaveDB()
    profile = SimpleNamespace(id=uuid4(), financial_plan={}, primary_bucket_id=None,
                              underwriting_term_sheet_amount=None, underwriting_status="in_underwriting")
    actor = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    body = production_body(expected_version=0)
    if zero_regular:
        body.update(rate_pct=0, term_months=12, repayment_structure="interest_only",
                    repayment_config={"after_balloon": "keep_payment", "balloons": []})
    with (patch.object(sheets, "current_sheet", AsyncMock(return_value=None)),
          patch.object(sheets, "_add_dscr_snapshot", AsyncMock()),
          patch.object(sheets.profiles, "log_profile_action", AsyncMock()),
          patch("app.routers.application_profiles.apply_underwriting_changes", AsyncMock())):
        row, reapplied = await sheets.record_sheet(db, profile=profile, user=actor, body=body, request=None)
    values = sheets.sheet_terms(row)
    assert reapplied is None and row.version == 1
    assert row.extra["repayment_config"] == body["repayment_config"]
    assert "schedule" not in row.extra
    assert row.monthly_debt_service == values["monthly_program_coverage_amount"]
    assert profile.financial_plan["new_loan_repayment_config"] == body["repayment_config"]
    assert evaluate(profile.financial_plan)["new_payment_monthly"] == row.monthly_debt_service
    if zero_regular:
        assert row.extra["periodic_payment"] == 0 and row.extra["monthly_equivalent_payment"] == 0
        assert row.extra["balloon_amount"] == 100000 and row.monthly_debt_service > 0


@pytest.mark.asyncio
async def test_production_prefill_preserves_plan_cadence_and_events_over_stage_one_defaults():
    profile = SimpleNamespace(id=uuid4(), underwriting_approved_amount=200000, underwriting_term_sheet_amount=200000,
        dealer_id=None, financial_plan=plan_for(new_loan_payments_per_year=51.96))
    parent = SimpleNamespace(status="draft", frozen_revision_id=None,
        arrangement={"requested": 300000, "term": 60, "dealer_cof": 20, "facility_type": "Term loan"})
    result = await sheets.defaults(SaveDB(), profile, parent)
    values = result["values"]
    expected = build_schedule(100000, 12, 36, structure="amortizing", periods_per_year=51.96, repayment_config=CONFIG)
    assert values["approved_amount"] == 100000 and values["rate_pct"] == 12 and values["term_months"] == 36
    assert values["payment_frequency"] == "weekly" and values["payments_per_year"] == 51.96
    assert values["schedule"]["total_paid"] == expected["total_paid"]


@pytest.mark.asyncio
async def test_production_recording_still_rejects_unauthorized_actor_before_any_queries():
    db = SaveDB()
    with pytest.raises(HTTPException) as error:
        await sheets.record_sheet(db, profile=SimpleNamespace(id=uuid4()), user=SimpleNamespace(role=Role.CLIENT), body=production_body(), request=None)
    assert error.value.status_code == 403
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_client_cannot_silently_drop_existing_flexible_terms():
    db = SaveDB()
    profile = SimpleNamespace(id=uuid4())
    actor = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    current = SimpleNamespace(version=1, repayment_config=CONFIG)
    payload_dict = client_payload().model_dump()
    payload_dict.update(expected_version=1)
    payload_dict.pop("repayment_config")
    with (patch.object(client_terms, "current_term_sheet", AsyncMock(return_value=current)),
          patch.object(client_terms, "loan_type_options", AsyncMock(return_value=[]))):
        with pytest.raises(HTTPException, match="flexible repayment"):
            await client_terms.save_terms(db, profile, actor, ClientTermsWrite.model_validate(payload_dict))
    old_production = production_body(expected_version=1)
    old_production.pop("repayment_config")
    with patch.object(sheets, "current_sheet", AsyncMock(return_value=SimpleNamespace(version=1, extra={"repayment_config": CONFIG}))):
        with pytest.raises(HTTPException, match="flexible repayment"):
            await sheets.record_sheet(db, profile=profile, user=actor, body=old_production, request=None)
    assert not db.rows


def test_additive_migration_is_single_new_column_and_protects_downward_history():
    migration = Path("alembic/versions/0263_flexible_loan_repayment.py").read_text()
    assert 'down_revision = "0262_financial_metric_review"' in migration
    assert "JSONB" in migration and "repayment_config IS NOT NULL" in migration
    assert "periodic_payment >= 0" in migration
    assert "jsonb_typeof(repayment_config) = 'object'" in migration
    assert "RAISE EXCEPTION" in migration and "UPDATE " not in migration and "DELETE " not in migration


def test_exact_configured_annual_service_does_not_reannualize_a_rounded_monthly_proxy():
    result = evaluate(plan_for({"amortization_months": 24, "after_balloon": "keep_payment", "balloons": []},
                               new_loan_rate_pct=0, new_loan_term_months=12))
    assert result["schedule"]["coverage_annual_debt_service"] == 100000
    assert result["debt_service_after_annual"] == 112000
    assert result["debt_service_after_annual"] != result["debt_service_after_monthly"] * 12


def test_exact_retained_annual_refinance_value_survives_advanced_adoption():
    recorded = {"kind": "client_terms", "id": "terms", "version": 1, "label": "Terms", "amount": 100000,
        "rate_pct": 0, "term_months": 12, "structure": "amortizing", "interest_only": False,
        "payment_monthly": 8333.33, "payments_per_year": 12,
        "repayment_config": {"amortization_months": 24, "after_balloon": "keep_payment", "balloons": []},
        "treatment": "refinance", "retained_annual_debt_service": 12345.67}
    plan = financial_plan.normalize_plan(plan_terms_link.adopt({}, recorded))
    assert evaluate(plan)["debt_service_after_annual"] == 112345.67


@pytest.mark.asyncio
async def test_production_prefill_keeps_zero_apr_and_simple_interest_io():
    config = deepcopy(CONFIG)
    for rate, kind, io in ((0, "amortizing", 0), (12, "simple_interest", 3)):
        plan = plan_for(config, new_loan_rate_pct=rate, new_loan_structure=kind, new_loan_interest_only_months=io,
                        new_loan_payments_per_year=51.5)
        profile = SimpleNamespace(id=uuid4(), underwriting_approved_amount=None, underwriting_term_sheet_amount=None,
                                  dealer_id=None, financial_plan=plan)
        parent = SimpleNamespace(status="draft", frozen_revision_id=None,
            arrangement={"requested": 300000, "term": 60, "dealer_cof": 20, "facility_type": "Term loan"})
        values = (await sheets.defaults(SaveDB(), profile, parent))["values"]
        expected = build_schedule(100000, rate, 36, structure=kind, interest_only_months=io,
                                  periods_per_year=51.5, repayment_config=config)
        assert values["rate_pct"] == rate and values["schedule"]["total_paid"] == expected["total_paid"]
        assert values["schedule"]["interest_only_months"] == expected["interest_only_months"]


@pytest.mark.parametrize("bad", ["not-a-date", "2026-11-06T12:00:00"])
def test_advanced_plan_timestamp_validation_requires_a_valid_timezone(bad):
    with pytest.raises(ValidationError):
        FinancialPlanWrite(expected_updated_at=bad)


def test_configured_plan_fullwrite_requires_current_timestamp_and_explicit_config_even_to_clear():
    stamp = "2026-11-06T12:00:00+00:00"
    current = {**plan_for(), "updated_at": stamp}
    for data in ({}, {"new_loan_repayment_config": None},
                 {"new_loan_repayment_config": None, "expected_updated_at": "2026-11-06T11:00:00+00:00"}):
        with pytest.raises(HTTPException) as error:
            financial_plan.validate_plan_write(current, FinancialPlanWrite(**data))
        assert error.value.status_code == 409
    financial_plan.validate_plan_write(current, FinancialPlanWrite(new_loan_repayment_config=None, expected_updated_at=stamp))
    financial_plan.validate_plan_write({}, FinancialPlanWrite())  # Old null-config clients still work.


@pytest.mark.asyncio
async def test_plan_api_locks_refreshes_and_rejects_stale_write_before_mutation():
    from app.routers import application_profiles as routes

    current = {**plan_for(), "updated_at": "2026-11-06T12:00:00+00:00"}
    profile = SimpleNamespace(id=uuid4(), financial_plan=deepcopy(current))
    db = SimpleNamespace(execute=AsyncMock(), refresh=AsyncMock(), commit=AsyncMock())
    actor = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    with patch.object(routes.profiles, "load_profile", AsyncMock(return_value=profile)):
        with pytest.raises(HTTPException) as error:
            await routes.put_financial_plan(profile.id, FinancialPlanWrite(new_loan_repayment_config=None,
                expected_updated_at="2026-11-06T11:00:00+00:00"), actor, db)
    assert error.value.status_code == 409 and profile.financial_plan == current
    db.execute.assert_awaited_once()
    db.refresh.assert_awaited_once_with(profile, attribute_names=["financial_plan"])
    db.commit.assert_not_awaited()
