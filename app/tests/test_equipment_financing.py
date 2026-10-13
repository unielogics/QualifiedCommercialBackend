from __future__ import annotations

from pathlib import Path
from runpy import run_path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.routers.dealer_ai_intake import (
    DEALER_VARIANT,
    MAIN_STREET_VARIANT,
    DealerIntakePatch,
    DealerIntakeStart,
    EquipmentFinancingDetails,
    PublicIntakeAttribution,
    _is_business_equipment_application,
    _is_equipment_application,
    _is_main_street_equipment_intake,
    _public_intake_attribution,
    _require_dealer_intake,
)
from app.services import application_programs
from app.services.equipment_financing import (
    EQUIPMENT_DETAIL_FIELDS,
    MAXIMUM_AMOUNT,
    MAXIMUM_TERM_MONTHS,
    MINIMUM_AMOUNT,
    MINIMUM_TERM_MONTHS,
    assess_equipment_fit,
    normalize_funding_intent,
)
from app.services.funding_program_baselines import suggested_baseline
from app.services.program_rules import evaluate_rules, validate_rules

PASSING_FACTS = {
    "equipment_financing_intent": True,
    "business_age_years": 3,
    "annual_revenue": 750_000,
    "credit_score": 700,
    "cash_flow": 125_000,
    "requested_amount": 250_000,
}


def _published_equipment_candidate(
    *,
    vertical: str,
    requested_amount: float = 250_000,
    cash_flow: float | None = 125_000,
):
    migration = run_path(
        str(
            Path(__file__).parents[2]
            / "alembic"
            / "versions"
            / "0236_equipment_financing_sync.py"
        )
    )
    program_id = uuid4()
    catalog = SimpleNamespace(
        id=program_id,
        program_key="equipment_financing",
        name="Equipment Financing",
        public_slug="equipment-financing",
    )
    scope = SimpleNamespace(
        vertical=vertical,
        scope_key=migration["SCOPE_KEY"],
        intake_variants=[
            "dealer_gatekeeper_v1" if vertical == "dealer" else "main_street_v1"
        ],
        intent_keys=(
            migration["DEALER_SCOPE_INTENT_KEYS"]
            if vertical == "dealer"
            else migration["MAIN_STREET_SCOPE_INTENT_KEYS"]
        ),
        naics_prefixes=[],
        excluded_naics_prefixes=[],
        industry_keys=[],
        required_fact_keys=migration["REQUIRED_FACT_KEYS"],
        is_active=True,
    )
    playbook = SimpleNamespace(
        id=uuid4(), version=1, rules=migration["EQUIPMENT_FIT_RULES"]
    )
    metric_values = {
        "business_age_years": 3,
        "annual_revenue": 750_000,
        "credit_score": 700,
        "cash_flow": cash_flow,
    }
    context = {
        "vertical": vertical,
        "intake_variant": (
            "dealer_gatekeeper_v1" if vertical == "dealer" else "main_street_v1"
        ),
        # Dealer deliberately has no Main Street intent. The canonical
        # equipment fit fact below is what establishes the requested use.
        "intent": None if vertical == "dealer" else "equipment",
        "intent_kind": "lending",
        "equipment_financing_intent": True,
        **metric_values,
        "requested_amount": requested_amount,
        "fact_sources": {
            key: {
                "verification_status": "verified",
                "source_refs": [{"kind": "accepted_fact", "field": key}],
            }
            for key in (
                "business_age_years",
                "annual_revenue",
                "credit_score",
                "cash_flow",
            )
            if metric_values[key] is not None
        },
    }
    with (
        patch.object(
            application_programs,
            "profile_fit_context",
            AsyncMock(return_value=context),
        ),
        patch.object(
            application_programs.program_catalog,
            "catalog_rows",
            AsyncMock(return_value=[catalog]),
        ),
        patch.object(
            application_programs.program_catalog,
            "scopes_by_program",
            AsyncMock(return_value={program_id: [scope]}),
        ),
        patch.object(
            application_programs.program_catalog,
            "published_versions_by_program",
            AsyncMock(return_value={program_id: playbook}),
        ),
    ):
        coroutine = application_programs.published_candidates(
            SimpleNamespace(), SimpleNamespace()
        )
        try:
            coroutine.send(None)
        except StopIteration as completed:
            return completed.value[0]
        raise AssertionError("published candidate evaluation attempted unexpected I/O")


def test_published_equipment_terms_and_complete_preliminary_fit():
    result = assess_equipment_fit(PASSING_FACTS)
    assert (MINIMUM_AMOUNT, MAXIMUM_AMOUNT) == (25_000, 2_000_000)
    assert (MINIMUM_TERM_MONTHS, MAXIMUM_TERM_MONTHS) == (24, 84)
    assert result["recommendation_status"] == "recommended"
    assert result["request_classification"] == "published_program"
    assert result["eligible"] is True
    assert result["blocked_by"] == []
    assert result["needs"] == []


def test_missing_equipment_facts_request_information_instead_of_failing():
    result = assess_equipment_fit(
        {
            "equipment_financing_intent": True,
            "business_age_years": 2,
        }
    )
    assert result["recommendation_status"] == "needs_information"
    assert result["request_classification"] == "needs_information"
    assert result["eligible"] is False
    assert result["blocked_by"] == []
    assert {"Annual revenue", "Estimated credit score", "Positive cash flow", "Requested amount"} <= set(
        result["needs"]
    )


def test_out_of_range_equipment_request_routes_to_custom_review_not_decline():
    result = assess_equipment_fit({**PASSING_FACTS, "requested_amount": 2_500_000})
    assert result["recommendation_status"] == "custom_review"
    assert result["request_classification"] == "custom_review"
    assert result["eligible"] is False
    assert result["blocked_by"] == []
    assert any("Custom review" in item for item in result["needs"])


def test_out_of_range_with_missing_facts_stays_needs_information():
    facts = {**PASSING_FACTS, "requested_amount": 2_500_000}
    facts.pop("cash_flow")
    result = assess_equipment_fit(facts)
    assert result["recommendation_status"] == "needs_information"
    assert result["request_classification"] == "needs_information"


def test_known_failed_threshold_remains_an_explainable_preliminary_failure():
    result = assess_equipment_fit({**PASSING_FACTS, "credit_score": 620})
    assert result["recommendation_status"] == "not_eligible"
    assert result["eligible"] is False
    assert any("640" in item for item in result["blocked_by"])


def test_reviewable_baseline_has_both_scopes_rules_and_equipment_evidence():
    baseline = suggested_baseline("equipment_financing")
    assert {row["vertical"] for row in baseline["scopes"]} == {"dealer", "main_street"}
    validate_rules(baseline["rules"], enforce_supported_fields=True)
    keys = {row["requirement_key"] for row in baseline["requirements"]}
    assert {"equipment_quote", "equipment_transaction_details"} <= keys
    criteria = baseline["requirements"][-1]["completion_criteria"].lower()
    for phrase in (
        "description",
        "age",
        "condition",
        "useful life",
        "seller",
        "purchase price",
        "delivery",
        "business use",
    ):
        assert phrase in criteria


def test_published_rule_tree_uses_unknown_state_for_missing_facts():
    migration = run_path(
        str(
            Path(__file__).parents[2]
            / "alembic"
            / "versions"
            / "0236_equipment_financing_sync.py"
        )
    )
    rules = migration["EQUIPMENT_FIT_RULES"]
    validate_rules(rules, enforce_supported_fields=True)
    complete = evaluate_rules(rules, PASSING_FACTS)
    missing = evaluate_rules(
        rules,
        {key: value for key, value in PASSING_FACTS.items() if key != "credit_score"},
    )
    assert complete.state == "pass"
    assert complete.matched is True
    assert missing.state == "unknown"
    assert missing.matched is False
    assert migration["down_revision"] == "0235_capital_readiness"
    assert migration["EQUIPMENT_DETAIL_FIELDS"] == list(EQUIPMENT_DETAIL_FIELDS)


def test_migration_owns_versioned_scopes_without_overwriting_existing_rows():
    migration = run_path(
        str(
            Path(__file__).parents[2]
            / "alembic"
            / "versions"
            / "0236_equipment_financing_sync.py"
        )
    )
    calls: list[tuple[str, dict]] = []

    class RecordingBind:
        def execute(self, statement, parameters):
            calls.append((str(statement), parameters))

    migration["_insert_versioned_scope"](
        RecordingBind(),
        scope_id=migration["DEALER_SCOPE_ID"],
        program_id=uuid4(),
        vertical="dealer",
        intake_variants=["dealer_gatekeeper_v1"],
        intent_keys=migration["DEALER_SCOPE_INTENT_KEYS"],
    )
    sql, parameters = calls[0]
    assert "DO NOTHING" in sql
    assert "DO UPDATE" not in sql
    assert parameters["scope_key"] == "qc_equipment_v1"
    assert parameters["intent_keys"] == "[]"
    assert migration["MAIN_STREET_SCOPE_INTENT_KEYS"] == ["equipment"]


def test_signup_attribution_accepts_and_persists_equipment_intent():
    payload = DealerIntakeStart(
        full_name="Sam Owner",
        email="sam@example.com",
        phone="+19735550123",
        terms_accepted=True,
        privacy_accepted=True,
        funding_intent="equipment",
        source="equipment_page",
        page="/equipment-financing",
        equipment_details=EquipmentFinancingDetails(
            description="CNC mill",
            condition="used",
            purchase_price=180_000,
            intended_business_use="Increase production capacity",
        ),
    )
    assert payload.funding_intent == "equipment"
    assert _public_intake_attribution(payload) == {
        "source": "equipment_page",
        "page": "/equipment-financing",
        "funding_intent": "equipment",
    }
    assert _is_equipment_application(
        funding_intent=payload.funding_intent,
        details=payload.equipment_details,
    )
    assert normalize_funding_intent("Equipment Financing") == "equipment"


@pytest.mark.parametrize(
    "value",
    ["equipment", "equipment financing", "equipment_finance", "machinery"],
)
def test_public_attribution_normalizes_known_equipment_aliases(value):
    payload = PublicIntakeAttribution(funding_intent=value)
    assert payload.funding_intent == "equipment"


def test_public_attribution_rejects_unknown_funding_intent():
    with pytest.raises(ValidationError, match="funding_intent must be one of"):
        PublicIntakeAttribution(funding_intent="surprise_product")


def test_secure_room_updates_use_the_same_canonical_intent_contract():
    assert DealerIntakePatch(funding_intent="Equipment Financing").funding_intent == "equipment"
    with pytest.raises(ValidationError, match="funding_intent must be one of"):
        DealerIntakePatch(funding_intent="surprise_product")


def test_business_equipment_handoff_requires_both_canonical_signals():
    assert _is_business_equipment_application(
        PublicIntakeAttribution(vertical="business", funding_intent="equipment")
    )
    assert not _is_business_equipment_application(
        PublicIntakeAttribution(vertical="dealer", funding_intent="equipment")
    )
    assert not _is_business_equipment_application(
        PublicIntakeAttribution(vertical="business", funding_intent="working_capital")
    )


def test_legacy_dealer_routes_accept_only_dealer_or_main_street_equipment():
    dealer = SimpleNamespace(variant=DEALER_VARIANT, intake_state={})
    equipment = SimpleNamespace(
        variant=MAIN_STREET_VARIANT,
        intake_state={
            "funding_intent": "equipment",
            "main_street_details": {"intent": "equipment"},
        },
    )
    unrelated_main_street = SimpleNamespace(
        variant=MAIN_STREET_VARIANT,
        intake_state={
            "funding_intent": "working_capital",
            "main_street_details": {"intent": "working_capital"},
        },
    )

    assert _is_main_street_equipment_intake(equipment)
    _require_dealer_intake(dealer)
    _require_dealer_intake(equipment)
    with pytest.raises(HTTPException) as exc_info:
        _require_dealer_intake(unrelated_main_street)
    assert exc_info.value.status_code == 404


@pytest.mark.parametrize("vertical", ["dealer", "main_street"])
def test_published_candidate_matches_equipment_for_each_supported_vertical(vertical):
    candidate = _published_equipment_candidate(vertical=vertical)
    assert candidate.scope_status == "matched"
    assert candidate.rule_state == "pass"
    assert candidate.recommendation_status == "recommended"
    assert candidate.eligible is True


def test_published_candidate_routes_amount_only_range_failure_to_custom_review():
    candidate = _published_equipment_candidate(
        vertical="dealer", requested_amount=2_500_000
    )
    assert candidate.scope_status == "matched"
    assert candidate.rule_state == "fail"
    assert candidate.recommendation_status == "custom_review"
    assert candidate.eligible is False
    assert any("Custom review" in reason for reason in candidate.reasons)


def test_published_candidate_keeps_missing_fact_as_needs_information():
    candidate = _published_equipment_candidate(
        vertical="main_street", requested_amount=2_500_000, cash_flow=None
    )
    assert candidate.recommendation_status == "needs_information"
    assert candidate.eligible is False


def test_reported_zero_cash_flow_is_known_and_fails_positive_cash_flow_rule():
    assert (
        application_programs._first_numeric_metric(
            {"estimated_ebitda_or_cash_flow": 0, "cash_flow": 50_000},
            "estimated_ebitda_or_cash_flow",
            "cash_flow",
        )
        == 0
    )
    candidate = _published_equipment_candidate(vertical="dealer", cash_flow=0)
    assert candidate.recommendation_status == "not_eligible"
    assert candidate.rule_state == "fail"

