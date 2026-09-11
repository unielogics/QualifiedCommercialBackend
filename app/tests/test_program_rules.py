from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.schemas.application_profile import ProgramFitCandidate
from app.services.application_programs import _automatic_candidate, _is_lending_applicable
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


@pytest.mark.parametrize("factory", [chat_action_idempotency_key, missing_email_idempotency_key])
def test_delivery_idempotency_keys_are_stable_and_fit_database_column(factory) -> None:
    requirement_key = "custom_requirement_" + ("x" * 300)

    first = factory("profile", requirement_key, "email", 1)
    repeated = factory("profile", requirement_key, "email", 1)
    changed = factory("profile", requirement_key, "email", 2)

    assert first == repeated
    assert first != changed
    assert len(first) <= 160
