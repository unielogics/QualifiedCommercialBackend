from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.enums import LoanStage
from app.schemas.application_profile import ProgramFitCandidate
from app.schemas.funding_program import FundingProgramVersionCreate
from app.services import application_programs
from app.services.application_programs import (
    _analysis_nsf_count,
    _automatic_candidate,
    _automatic_evidence_decision,
    _coverage_for_files,
    _is_lending_applicable,
    _requirement_is_effectively_accepted,
    _requirement_needs_client_evidence,
    _scope_match,
    analysis_is_high_confidence_match,
)
from app.services.intake_chat_actions import _idempotency_key as chat_action_idempotency_key
from app.services.missing_item_automation import _idempotency_key as missing_email_idempotency_key
from app.services.program_rules import ProgramRuleError, evaluate_rules, validate_rules


def test_program_rules_evaluate_bounded_deterministic_criteria() -> None:
    rules = {
        "fit": {
            "all": [
                {"field": "annual_revenue", "op": "gte", "value": 500_000},
                {"field": "industry", "op": "in", "value": ["dealer", "retail"]},
                {
                    "field": "business_bank_statements_6_months",
                    "op": "evidence_available",
                },
            ]
        }
    }

    result = evaluate_rules(
        rules,
        {
            "annual_revenue": 750_000,
            "industry": "dealer",
            "evidence_available": {"business_bank_statements_6_months"},
        },
    )

    assert result.matched is True
    assert result.passed == 3
    assert result.total == 3
    assert result.confidence == 1.0


@pytest.mark.parametrize(
    "rules",
    [
        {"fit": {"field": "revenue", "op": "python", "value": "__import__('os')"}},
        {"fit": {"field": "revenue", "op": "gte", "value": "lots"}},
        {"fit": {"all": []}},
        {"fit": {"field": "revenue", "op": "eq", "value": 1, "script": "return true"}},
    ],
)
def test_program_rules_reject_executable_or_unbounded_shapes(rules: dict) -> None:
    with pytest.raises(ProgramRuleError):
        validate_rules(rules)


def test_program_version_rejects_invalid_requirement_taxonomy() -> None:
    with pytest.raises(ValidationError):
        FundingProgramVersionCreate(
            rules={"fit": {"field": "annual_revenue", "op": "gte", "value": 1}},
            requirements=[
                {
                    "requirement_key": "Tax Returns",
                    "label": "Tax returns",
                    "category": "made_up_category",
                }
            ],
            reason="Reviewed invalid payload",
            confirmed=True,
        )


def test_missing_published_fit_rule_is_not_an_implicit_match() -> None:
    result = evaluate_rules({}, {"annual_revenue": 1_000_000})

    assert result.matched is False
    assert result.total == 0
    assert result.reasons == ["No published fit rule"]


def _candidate(key: str, *, eligible: bool, confidence: float = 0) -> ProgramFitCandidate:
    return ProgramFitCandidate(
        program_key=key,
        program_name=key.replace("_", " ").title(),
        catalog_id=uuid4(),
        public_slug=key.replace("_", "-"),
        playbook_id=uuid4(),
        playbook_version=1,
        eligible=eligible,
        recommendation_status="recommended" if eligible else "not_eligible",
        fit_score=confidence * 100,
        confidence=confidence,
        priority=0,
        reasons=[],
    )


def test_automatic_program_selects_only_an_eligible_real_product() -> None:
    profile = SimpleNamespace(vertical="dealer")
    baseline = _candidate("business_baseline", eligible=False)
    fit = _candidate("equipment_financing", eligible=True, confidence=0.8)

    assert _automatic_candidate(profile, [fit, baseline]) is fit
    assert _automatic_candidate(profile, [baseline]) is None
    assert _automatic_candidate(SimpleNamespace(vertical="mca"), [baseline]) is None


@pytest.mark.parametrize("intent_kind", ["non_lending", "route_out"])
def test_non_lending_intents_do_not_apply_program_readiness(intent_kind: str) -> None:
    assert _is_lending_applicable({"intent_kind": intent_kind}) is False


def test_lending_intent_applies_program_readiness() -> None:
    assert _is_lending_applicable({"intent_kind": "lending"}) is True


def test_requirement_advances_only_after_effective_acceptance() -> None:
    partial = SimpleNamespace(
        status="received_unverified",
        evidence_file_id=uuid4(),
        provenance={"coverage": {"required": 6, "current": 5, "complete": False}},
    )
    complete = SimpleNamespace(
        status="received_unverified",
        evidence_file_id=uuid4(),
        provenance={"coverage": {"required": 6, "current": 6, "complete": True}},
    )

    assert _requirement_is_effectively_accepted(partial, None) is False
    assert _requirement_is_effectively_accepted(complete, None) is False
    complete.status = "verified"
    assert _requirement_is_effectively_accepted(complete, None) is True
    assert _requirement_needs_client_evidence(partial) is True
    assert _requirement_needs_client_evidence(complete) is False


def test_catalog_contains_exact_products_and_vertical_placements() -> None:
    migration_path = Path("alembic/versions/0211_program_catalog_ai_evidence.py")
    spec = importlib.util.spec_from_file_location("migration_0211", migration_path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    keys = [row[0] for row in migration.PROGRAMS]
    assert len(keys) == 22
    assert len(set(keys)) == 22
    assert "business_baseline" not in keys
    ids = {key: uuid4() for key in keys}
    scopes = migration._catalog_scopes(ids)
    counts = {
        vertical: len({row["program_id"] for row in scopes if row["vertical"] == vertical})
        for vertical in ("real_estate", "dealer", "main_street", "mca")
    }
    assert counts == {"real_estate": 8, "dealer": 8, "main_street": 12, "mca": 1}


def test_legacy_fit_imports_are_bounded_inactive_drafts() -> None:
    migration_path = Path("alembic/versions/0212_legacy_fit_rule_drafts.py")
    spec = importlib.util.spec_from_file_location("migration_0212", migration_path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert "business_baseline" not in migration.LEGACY_FIT_RULE_DRAFTS
    assert set(migration.LEGACY_FIT_RULE_DRAFTS) == {
        "sba_7a",
        "dealer_real_estate_capital",
        "ez_term",
        "microcap",
        "line_of_credit",
        "equipment_financing",
        "jumbo_term",
        "hybrid_term_loc",
        "transportation_finance",
        "sba_grocery",
        "sba_made_in_america",
    }
    for rules in migration.LEGACY_FIT_RULE_DRAFTS.values():
        validate_rules(rules)
        assert rules["import"]["review_required"] is True
        assert rules["import"]["status"] == "provisional"

    assert migration.LEGACY_FIT_RULE_DRAFTS["microcap"]["unresolved_review_items"]
    assert migration.LEGACY_FIT_RULE_DRAFTS["sba_7a"]["unresolved_review_items"]


def test_nsf_count_prefers_explicit_month_rows() -> None:
    analysis = SimpleNamespace(
        analysis={
            "key_facts": {
                "nsf_or_overdraft_count": 99,
                "months": [
                    {"month": "2026-01", "nsf_or_overdraft_count": 1},
                    {"month": "2026-02", "nsf_count": 2},
                ],
            }
        }
    )

    assert _analysis_nsf_count(analysis) == 3


def test_hard_scope_requires_industry_or_declared_facts() -> None:
    transportation = SimpleNamespace(
        intake_variants=[],
        intent_keys=[],
        industry_keys=["trucking_logistics"],
        naics_prefixes=["48", "49"],
        required_fact_keys=[],
    )
    dealer_collateral = SimpleNamespace(
        intake_variants=[],
        intent_keys=[],
        industry_keys=[],
        naics_prefixes=[],
        required_fact_keys=["declared_collateral"],
    )

    assert (
        _scope_match(transportation, {"industry_key": "retail", "naics_code": "445110"})[0] is False
    )
    assert _scope_match(transportation, {"industry_key": "", "naics_code": "484121"})[0] is True
    assert _scope_match(dealer_collateral, {"declared_collateral": False})[0] is False
    assert _scope_match(dealer_collateral, {"declared_collateral": None})[0] is None


def test_ai_decisions_validate_type_entity_period_and_duplicates() -> None:
    requirement = SimpleNamespace(
        requirement_key="business_bank_statements_6_months",
        label="Last 6 months business bank statements",
        category="financials",
    )
    file = SimpleNamespace(
        file_name="Bank statement 2026-08.pdf",
        content_hash="same-hash",
        statement_period=None,
    )
    analysis = SimpleNamespace(
        status="completed",
        content_hash="same-hash",
        confidence="high",
        classification="bank_statement",
        error=None,
        skip_reason=None,
        skip_detail=None,
        analysis={"profile_facts": {"legal_entity_name": {"value": "Good Warranty Solutions LLC"}}},
    )

    accepted = _automatic_evidence_decision(
        requirement=requirement,
        file=file,
        analysis=analysis,
        expected_entity="Good Warranty Solutions",
        duplicate_content=False,
    )
    wrong_entity = _automatic_evidence_decision(
        requirement=requirement,
        file=file,
        analysis=analysis,
        expected_entity="Another Company",
        duplicate_content=False,
    )
    duplicate = _automatic_evidence_decision(
        requirement=requirement,
        file=file,
        analysis=analysis,
        expected_entity="Good Warranty Solutions",
        duplicate_content=True,
    )
    entity_unconfirmed = _automatic_evidence_decision(
        requirement=requirement,
        file=file,
        analysis=SimpleNamespace(
            **{
                **analysis.__dict__,
                "analysis": {"profile_facts": {}},
            }
        ),
        expected_entity="Good Warranty Solutions",
        duplicate_content=False,
    )

    assert accepted[:2] == ("accepted", "validated")
    assert wrong_entity[:2] == ("rejected", "wrong_entity")
    assert duplicate[:2] == ("rejected", "duplicate")
    assert entity_unconfirmed[:2] == ("needs_more", "entity_unconfirmed")


def test_coverage_counts_distinct_periods_not_file_count() -> None:
    bank_requirement = SimpleNamespace(
        requirement_key="business_bank_statements_6_months",
        label="Last 6 months business bank statements",
        category="financials",
    )
    files = [
        SimpleNamespace(
            id=uuid4(), file_name=f"Statement 2026-{month:02d}.pdf", statement_period=None
        )
        for month in (1, 2, 2, 3, 4, 5, 6)
    ]

    complete, coverage = _coverage_for_files(bank_requirement, files, {})

    assert complete is True
    assert coverage["current"] == 6
    assert len(coverage["months"]) == 6


def test_combined_financial_statement_satisfies_both_document_types() -> None:
    requirement = SimpleNamespace(
        requirement_key="ytd_p_and_l_balance_sheet",
        label="Year-to-date P&L and balance sheet",
        category="financials",
    )
    file = SimpleNamespace(
        id=uuid4(),
        file_name="Current financials.pdf",
        statement_period=None,
    )
    analysis = SimpleNamespace(
        status="completed",
        classification="current_p_and_l",
        analysis={
            "baseline_categories_supported": [
                "profit and loss",
                "balance sheet",
            ]
        },
    )

    complete, coverage = _coverage_for_files(requirement, [file], {file.id: analysis})

    assert complete is True
    assert coverage["profit_and_loss"] is True
    assert coverage["balance_sheet"] is True


def test_ai_evidence_acceptance_requires_current_high_confidence_content_match() -> None:
    requirement = SimpleNamespace(
        requirement_key="business_tax_returns_2_years",
        label="Last 2 years business tax returns",
        category="financials",
    )
    file = SimpleNamespace(content_hash="current")
    matching = SimpleNamespace(
        status="completed",
        content_hash="current",
        confidence="high",
        classification="tax_return",
    )

    assert analysis_is_high_confidence_match(requirement, file, matching) is True
    assert (
        analysis_is_high_confidence_match(
            requirement,
            file,
            SimpleNamespace(**{**matching.__dict__, "confidence": "medium"}),
        )
        is False
    )
    assert (
        analysis_is_high_confidence_match(
            requirement,
            file,
            SimpleNamespace(**{**matching.__dict__, "content_hash": "stale"}),
        )
        is False
    )
    assert (
        analysis_is_high_confidence_match(
            requirement,
            file,
            SimpleNamespace(**{**matching.__dict__, "classification": "bank_statement"}),
        )
        is False
    )


def test_loaded_program_automatically_starts_underwriting_and_syncs_early_loan() -> None:
    profile = SimpleNamespace(
        id=uuid4(),
        loan_id=uuid4(),
        dealer_id=None,
        primary_bucket_id=None,
        underwriting_status="collecting_docs",
        underwriting_updated_at=None,
        underwriting_updated_by_user_id=uuid4(),
    )
    loan = SimpleNamespace(id=profile.loan_id, stage=LoanStage.COLLECTING_DOCS)

    class Db:
        async def get(self, model, key):
            return loan if key == loan.id else None

        async def flush(self):
            return None

    with (
        patch.object(application_programs, "log_activity", AsyncMock()) as loan_log,
        patch.object(
            application_programs.profiles, "log_profile_action", AsyncMock()
        ) as profile_log,
        patch.object(application_programs.file_events, "emit", AsyncMock()) as emit,
    ):
        changed = asyncio.run(
            application_programs._auto_start_underwriting_if_loaded(
                Db(), profile, ["dealer_working_capital"]
            )
        )
        repeated = asyncio.run(
            application_programs._auto_start_underwriting_if_loaded(
                Db(), profile, ["dealer_working_capital"]
            )
        )

    assert changed is True and repeated is False
    assert profile.underwriting_status == "in_underwriting"
    assert profile.underwriting_updated_by_user_id is None
    assert loan.stage == LoanStage.PROCESSING
    loan_log.assert_awaited_once()
    profile_log.assert_awaited_once()
    emit.assert_awaited_once()


@pytest.mark.parametrize("factory", [chat_action_idempotency_key, missing_email_idempotency_key])
def test_delivery_idempotency_keys_are_stable_and_fit_database_column(factory) -> None:
    requirement_key = "custom_requirement_" + ("x" * 300)

    first = factory("profile", requirement_key, "email", 1)
    repeated = factory("profile", requirement_key, "email", 1)
    changed = factory("profile", requirement_key, "email", 2)

    assert first == repeated
    assert first != changed
    assert len(first) <= 160
