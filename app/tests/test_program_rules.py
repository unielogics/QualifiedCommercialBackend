from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.enums import LoanStage
from app.schemas.application_profile import ProgramFitCandidate
from app.services import application_programs
from app.services.application_programs import (
    _automatic_candidate,
    _is_lending_applicable,
    _requirement_is_fully_loaded,
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


def test_missing_published_fit_rule_is_not_an_implicit_match() -> None:
    result = evaluate_rules({}, {"annual_revenue": 1_000_000})

    assert result.matched is False
    assert result.total == 0
    assert result.reasons == ["No published fit rule"]


def _candidate(key: str, *, eligible: bool, confidence: float = 0) -> ProgramFitCandidate:
    return ProgramFitCandidate(
        program_key=key,
        program_name=key.replace("_", " ").title(),
        playbook_id=uuid4(),
        playbook_version=1,
        eligible=eligible,
        fit_score=confidence * 100,
        confidence=confidence,
        priority=0,
        reasons=[],
    )


def test_automatic_program_prefers_eligible_then_vertical_baseline() -> None:
    profile = SimpleNamespace(vertical="dealer")
    baseline = _candidate("business_baseline", eligible=False)
    fit = _candidate("equipment_financing", eligible=True, confidence=0.8)

    assert _automatic_candidate(profile, [fit, baseline]) is fit
    assert _automatic_candidate(profile, [baseline]) is baseline
    assert _automatic_candidate(SimpleNamespace(vertical="mca"), [baseline]) is None


@pytest.mark.parametrize("intent_kind", ["non_lending", "route_out"])
def test_non_lending_intents_do_not_apply_program_readiness(intent_kind: str) -> None:
    assert _is_lending_applicable({"intent_kind": intent_kind}) is False


def test_lending_intent_applies_program_readiness() -> None:
    assert _is_lending_applicable({"intent_kind": "lending"}) is True


def test_received_requirement_only_counts_as_loaded_when_coverage_is_complete() -> None:
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

    assert _requirement_is_fully_loaded(partial, None) is False
    assert _requirement_is_fully_loaded(complete, None) is True


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

    with patch.object(application_programs, "log_activity", AsyncMock()) as loan_log, patch.object(
        application_programs.profiles, "log_profile_action", AsyncMock()
    ) as profile_log, patch.object(application_programs.file_events, "emit", AsyncMock()) as emit:
        changed = asyncio.run(
            application_programs._auto_start_underwriting_if_loaded(
                Db(), profile, ["business_baseline"]
            )
        )
        repeated = asyncio.run(
            application_programs._auto_start_underwriting_if_loaded(
                Db(), profile, ["business_baseline"]
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
