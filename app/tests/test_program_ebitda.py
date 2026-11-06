"""Source-backed current earnings, never hypothetical refinance qualification."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.enums import Role
from app.schemas.funding_program import FundingProgramFinancialLimits, FundingProgramVersionCreate
from app.schemas.funding_program_baseline import FundingProgramBaselineRead
from app.services import application_programs as programs
from app.services import (
    financial_cockpit,
    financial_metric_review,
    funding_program_baselines,
    funding_program_drafts,
    funding_programs,
)
from app.services.program_earnings_facts import annual_ebitda_fact
from app.services.program_rules import (
    ProgramRuleError,
    compile_financial_limits,
    evaluate_rules,
    validate_rules,
)

ENTITY = "Example LLC"
LIMITS = {"minimum_annual_ebitda": 50000, "currency": "USD", "source_reference": "Reviewed lender guideline, page 2"}
RULE = {"field": "annual_ebitda", "op": "gte", "value": 50000}


def pnl(**extra):
    return {"file_id": "source", "analysis_id": "analysis-1", "content_hash": "a" * 64,
            "file_name": "Statement.pdf", "classification": "current_p_and_l", "accepted": True,
            "key_facts": {"business_name": ENTITY, "currency": "USD", "basis": "accrual",
                          "period_start": "2026-01-01", "period_end": "2026-06-30",
                          "gross_revenue": 200000, "net_income": 20000, "interest": 1000,
                          "income_taxes": 2000, "depreciation_and_amortization": 3000, **extra}}


def test_current_reconciled_annual_ebitda_has_its_actual_period_and_source():
    value, source = annual_ebitda_fact([pnl()], entity=ENTITY)
    assert value == 52000
    assert source["verification_status"] == "verified"
    assert source["months_covered"] == 6 and source["annualization_factor"] == 2
    assert source["currency"] == "USD" and source["accounting_basis"] == "accrual"
    assert source["basis"] == "validated_profit_and_loss_annual_ebitda"
    assert source["source_refs"][0]["analysis_id"] == "analysis-1"
    assert source["source_refs"][0]["period_end"] == "2026-06-30"


@pytest.mark.parametrize("changes", [
    {"currency": "EUR"}, {"currency": None}, {"basis": None},
    {"business_name": "Another business LLC"}, {"business_name": None},
    {"period_start": None}, {"period_start": "2026-03-01"},
    {"income_taxes": None}, {"net_income": None}, {"ebitda": 99999},
])
def test_unavailable_provisional_or_unreconciled_source_never_qualifies(changes):
    value, source = annual_ebitda_fact([pnl(**changes)], entity=ENTITY)
    assert value is None and source["verification_status"] == "unknown"
    assert source["reason"] and source["action"] == "request_or_review_profit_and_loss"
    assert not evaluate_rules({"fit": RULE}, {"annual_ebitda": value}).matched


def test_unknown_target_or_unaccepted_source_does_not_qualify():
    row = pnl()
    assert annual_ebitda_fact([row], entity=None)[0] is None
    row["accepted"] = False
    assert annual_ebitda_fact([row], entity=ENTITY)[0] is None


def test_newer_unreviewed_statement_does_not_fall_back_to_older_accepted_earnings():
    old = pnl(period_start="2025-01-01", period_end="2025-12-31")
    old["file_id"] = "old"
    current = {**pnl(), "accepted": False}
    assert annual_ebitda_fact([old, current], entity=ENTITY)[0] is None


def test_tax_and_bank_cash_are_not_substitutes_and_zero_is_a_known_earnings_value():
    for classification in ("tax_return", "bank_statement"):
        row = {**pnl(ordinary_business_income=900000, ebitda=900000), "classification": classification}
        assert annual_ebitda_fact([row], entity=ENTITY)[0] is None
    value, source = annual_ebitda_fact([pnl(net_income=0, interest=0, income_taxes=0, depreciation_and_amortization=0)], entity=ENTITY)
    assert value == 0 and source["verification_status"] == "verified"


def test_current_factual_correction_applies_but_stale_or_hypothetical_changes_do_not():
    analyses = [pnl()]
    cockpit = financial_cockpit.build_cockpit(analyses, [], business_entity=ENTITY)
    operand = next(i for i in cockpit["metric_review"]["inputs"] if i["key"] == "profit_and_loss.net_income")
    state, _ = financial_metric_review.change(None, cockpit["metric_review"], input_key=operand["key"],
        action="correct", value=30000, reason="Private staff review reason", expected_revision=0,
        source_stamp=operand["source_stamp"], idempotency_key="qualify-current-source", actor=SimpleNamespace(id=uuid4(), name="Staff", role=Role.LOAN_EXEC))
    value, source = annual_ebitda_fact(analyses, entity=ENTITY, state=state)
    assert value == 72000 and source["corrected_inputs"] == [operand["key"]]
    assert "Private staff review reason" not in str(source)
    newer = deepcopy(analyses)
    newer[0]["analysis_id"] = "replacement-analysis"
    assert annual_ebitda_fact(newer, entity=ENTITY, state=state)[0] == 52000
    # A refinance plan is not an operand of the factual provider.
    hypothetical = {"overrides": {"ebitda": 900000}, "addbacks": [{"amount": 900000}]}
    assert annual_ebitda_fact(analyses, entity=ENTITY, state={**state, "financial_plan": hypothetical})[0] == 72000


@pytest.mark.parametrize("status", ["unknown", "provisional", "conflicting", "estimated_from_statements", "estimated_from_deposits"])
def test_verified_only_gate_is_specific_to_annual_ebitda(status):
    context = {"annual_ebitda": 999999, "cash_flow": 999999}
    source = {key: {"verification_status": status} for key in context}
    values = programs.evaluation_values(context, source)
    assert values["annual_ebitda"] is None
    if status in programs.EVALUABLE_FACT_STATUSES:
        assert values["cash_flow"] == 999999  # Existing rule semantics preserved.
    assert evaluate_rules({"fit": {"not": RULE}}, values).state == "unknown"


def test_standalone_accepted_scalar_cannot_bypass_source_bound_earnings():
    fact = SimpleNamespace(field_key="annual_ebitda", status="accepted", reviewed_by_user_id=uuid4(),
                           value={"value": 900000}, normalized_value="900000", source_file_id=None, source_analysis_id=None,
                           extraction_method="staff", id=uuid4())
    context = {}
    sources = programs._fit_fact_sources(context, [fact], [], {})
    assert "annual_ebitda" not in context and sources["annual_ebitda"]["verification_status"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False])
async def test_profile_context_materializes_current_source_instead_of_cached_ai_or_refinance(accepted):
    intake = SimpleNamespace(id=uuid4(), bucket_id=None, variant="main_street_v1", business_name=ENTITY,
        estimated_credit_score=None, asset_rows=[], intake_state={},
        result_snapshot={"key_metrics": {"annual_ebitda": 900000, "estimated_ebitda_or_cash_flow": 900000}})
    profile = SimpleNamespace(id=uuid4(), intake_id=intake.id, primary_bucket_id=None, vertical="main_street",
        funding_category="term", entity_type=None, industry=None, subindustry=None, naics_code=None,
        financial_metric_review={}, financial_plan={"overrides": {"ebitda": 900000}})
    file_id = uuid4()
    source_file = SimpleNamespace(id=file_id, file_name="Current P&L.pdf", content_hash="a" * 64, statement_period=None)
    analysis = SimpleNamespace(id=uuid4(), analysis_version=1, classification="current_p_and_l", status="completed",
        content_hash="a" * 64, analysis={"key_facts": pnl()["key_facts"]})
    query = Mock()
    query.scalars.return_value.all.return_value = []
    query.scalar_one_or_none.return_value = None
    db = SimpleNamespace(execute=AsyncMock(return_value=query), get=AsyncMock(return_value=intake))
    budget = SimpleNamespace(items=[], total=None, real_estate_equipment_amount=None, real_estate_equipment_pct=None,
                             complete=False, category_totals={"equipment": 0})
    with (patch.object(programs, "source_funding_data", AsyncMock(return_value=(None, None, "working_capital", None))),
          patch.object(programs, "summarize_use_of_funds", return_value=budget),
          patch.object(programs, "_evidence_inventory", AsyncMock(return_value=([source_file], {file_id: analysis}, {"current_p_and_l"}))),
          patch.object(programs, "accepted_evidence_file_ids", AsyncMock(return_value={file_id} if accepted else set()))):
        context = await programs.profile_fit_context(db, profile)
    assert context["annual_ebitda"] == (52000 if accepted else None)
    source = context["fact_sources"]["annual_ebitda"]
    assert source["verification_status"] == ("verified" if accepted else "unknown")
    assert source["source_refs"][0]["file_id"] == str(file_id)
    assert context["cash_flow"] == 900000  # Existing cached cash-flow semantics unchanged.


@pytest.mark.parametrize("status,expected", [("verified", True), ("provisional", False), ("unknown", False)])
@pytest.mark.asyncio
async def test_published_candidates_use_current_fact_and_keep_provenance(status, expected):
    item = SimpleNamespace(id=uuid4(), program_key="term", public_slug="term", name="Term")
    version = SimpleNamespace(id=uuid4(), version=4, rules={"fit": RULE})
    context = {"vertical": "main_street", "annual_ebitda": 52000,
               "fact_sources": {"annual_ebitda": {"verification_status": status, "kind": "validated_annual_ebitda",
                    "currency": "USD", "period": "2026-01-01 – 2026-06-30", "months_covered": 6,
                    "annualization_factor": 2, "basis": "validated_profit_and_loss_annual_ebitda",
                    "source_refs": [{"file_id": "current", "analysis_id": "analysis-1"}]}}}
    with (patch.object(programs, "profile_fit_context", AsyncMock(return_value=context)),
          patch.object(programs.program_catalog, "catalog_rows", AsyncMock(return_value=[item])),
          patch.object(programs.program_catalog, "scopes_by_program", AsyncMock(return_value={})),
          patch.object(programs.program_catalog, "published_versions_by_program", AsyncMock(return_value={item.id: version})),
          patch.object(programs, "_catalog_scope_match", return_value=(True, []))):
        candidate = (await programs.published_candidates(SimpleNamespace(), SimpleNamespace()))[0]
    assert candidate.eligible is expected
    assert candidate.playbook_version == 4 and candidate.fact_sources["annual_ebitda"]["annualization_factor"] == 2
    assert candidate.rule_results[0]["source_refs"] == [{"file_id": "current", "analysis_id": "analysis-1"}]
    assert candidate.rule_state == ("pass" if expected else "unknown")
    assert ("annual_ebitda" in candidate.evidence_gaps) is (not expected)


def test_typed_limit_compiles_once_preserves_other_rules_and_roundtrips_source():
    original = {"fit": {"field": "vertical", "op": "eq", "value": "main_street"}}
    before = deepcopy(original)
    payload = FundingProgramVersionCreate(rules=original, financial_limits=LIMITS, confirmed=True)
    assert original == before
    assert payload.rules["fit"] == {"all": [before["fit"], RULE]}
    restored = FundingProgramVersionCreate(rules=payload.rules, confirmed=True)
    assert restored.rules == payload.rules and restored.financial_limits.source_reference == LIMITS["source_reference"]
    validate_rules(restored.rules, enforce_supported_fields=True)
    read = funding_programs._version_read(SimpleNamespace(id=uuid4(), version=1, status="draft", rules=restored.rules, published_at=None), [])
    assert read.financial_limits.minimum_annual_ebitda == 50000
    assert compile_financial_limits({"fit": RULE}, LIMITS)["fit"] == RULE


def test_malformed_legacy_financial_metadata_is_readable_for_repair_but_not_publishable():
    rules = {"fit": RULE, "financial_limits": {"minimum_annual_ebitda": "bad"}}
    read = funding_programs._version_read(SimpleNamespace(id=uuid4(), version=1, status="draft", rules=rules, published_at=None), [])
    assert read.financial_limits is None and read.rules == rules
    with pytest.raises(ProgramRuleError):
        validate_rules(read.rules, enforce_supported_fields=True)


@pytest.mark.parametrize("fit", [
    {"field": "annual_ebitda", "op": "gte", "value": 1000},
    {"any": [RULE, {"field": "vertical", "op": "present"}]},
    {"not": RULE}, {"all": [RULE, RULE]},
])
def test_typed_limit_rejects_conflicting_or_nonmandatory_rule(fit):
    with pytest.raises(ValidationError, match="conflicts"):
        FundingProgramVersionCreate(rules={"fit": fit}, financial_limits=LIMITS, confirmed=True)


@pytest.mark.parametrize("invalid", [
    {"minimum_annual_ebitda": True}, {"minimum_annual_ebitda": float("inf")},
    {"minimum_annual_ebitda": float("nan")}, {"currency": "EUR"}, {"source_reference": "  "},
])
def test_typed_limits_validate_numeric_unit_and_explicit_policy_source(invalid):
    with pytest.raises(ValidationError):
        FundingProgramFinancialLimits.model_validate({**LIMITS, **invalid})
    assert FundingProgramFinancialLimits(**{**LIMITS, "minimum_annual_ebitda": -100}).minimum_annual_ebitda == -100


@pytest.mark.parametrize("node", [
    {"field": "annual_ebitda", "op": "in", "value": [50000]},
    {"field": "annual_ebitda", "op": "eq", "value": "50000"},
    {"field": "annual_ebitda", "op": "evidence_available"},
])
def test_annual_ebitda_rules_are_typed_monetary_comparisons(node):
    with pytest.raises(ProgramRuleError):
        validate_rules({"fit": node}, enforce_supported_fields=True)


def test_explicit_baseline_limit_remains_a_sourced_review_draft(monkeypatch):
    spec = deepcopy(funding_program_baselines.CATALOG["microcap"])
    spec["financial_limits"] = deepcopy(LIMITS)
    monkeypatch.setitem(funding_program_baselines.CATALOG, "microcap", spec)
    baseline = FundingProgramBaselineRead.model_validate(funding_program_baselines.suggested_baseline("microcap"))
    assert baseline.needs_review and baseline.financial_limits.source_reference == LIMITS["source_reference"]
    assert any(LIMITS["source_reference"] in note for note in baseline.source_notes)
    program = SimpleNamespace(program_key="microcap", name="Microcap")
    payload, _source, error = funding_program_drafts.build_review_draft_payload(program, legacy_draft=None, scopes=[])
    assert error is None and payload.financial_limits.minimum_annual_ebitda == 50000
    assert payload.rules["financial_limits"] == LIMITS
    assert evaluate_rules(payload.rules, {"annual_ebitda": None}).matched is False


def test_existing_baselines_do_not_acquire_invented_ebitda_thresholds():
    for key in funding_program_baselines.CATALOG:
        baseline = funding_program_baselines.suggested_baseline(key)
        assert baseline["financial_limits"] is None
        assert "financial_limits" not in baseline["rules"]


@pytest.mark.asyncio
async def test_specialist_arf_route_rejects_new_ebitda_rule_on_create_and_publish():
    program = SimpleNamespace(id=uuid4(), program_key="arf_example")
    user = SimpleNamespace(id=uuid4())
    payload = FundingProgramVersionCreate(financial_limits=LIMITS, confirmed=True)
    with pytest.raises(HTTPException) as create_error:
        await funding_programs.create_version(SimpleNamespace(), program, payload, user)
    assert create_error.value.status_code == 422 and "specialist policy" in str(create_error.value.detail)
    result = Mock()
    result.scalar_one_or_none.return_value = SimpleNamespace(status="draft", rules=payload.rules)
    with pytest.raises(HTTPException) as publish_error:
        await funding_programs.publish_version(SimpleNamespace(execute=AsyncMock(return_value=result)), program, uuid4(), None, user)
    assert publish_error.value.status_code == 422


@pytest.mark.asyncio
async def test_legacy_specialist_arf_ebitda_config_cannot_silently_pass():
    item = SimpleNamespace(id=uuid4(), program_key="arf_example", name="Specialist", public_slug="specialist")
    version = SimpleNamespace(id=uuid4(), version=1, rules={"fit": RULE})
    with (patch.object(programs, "profile_fit_context", AsyncMock(return_value={"annual_ebitda": 900000})),
          patch.object(programs.program_catalog, "catalog_rows", AsyncMock(return_value=[item])),
          patch.object(programs.program_catalog, "scopes_by_program", AsyncMock(return_value={})),
          patch.object(programs.program_catalog, "published_versions_by_program", AsyncMock(return_value={item.id: version})),
          patch.object(programs, "_catalog_scope_match", return_value=(True, [])),
          patch("app.services.arf_channel.program_candidate", AsyncMock()) as specialist):
        result = (await programs.published_candidates(SimpleNamespace(), SimpleNamespace()))[0]
    assert result.eligible is False and result.criteria_status == "invalid"
    specialist.assert_not_called()
