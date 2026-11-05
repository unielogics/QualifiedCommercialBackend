"""The AI accepts what it has validated; a person corrects or overrides.

Body Good Studio's six statements sat at "received unverified" because the
baseline requirement's objective sentence had become an executable document
check, which cannot run over six statements, and the failure to run was read
as a failure to pass. Its signed authorization waited for a person to confirm
a certificate the system had generated itself.
"""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from app.services.application_programs import (
    _automatic_evidence_decision,
    _checks_could_not_run,
    _is_bare_request,
    _merged_review_checks,
    _verifiable_without_a_person,
)
from app.services.use_of_funds import seed_refinance_budget


def _requirement(**overrides):
    base = dict(
        requirement_key="business_bank_statements_6_months", canonical_key=None,
        label="Last 6 months business bank statements", category="financials",
        objective_text="Collect Last 6 months business bank statements", completion_criteria=None,
        review_checks=[], verification_required=False, completion_mode="ai_can_complete",
        collection_config={}, required_level="required", playbook_id=uuid4(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_a_bare_request_is_not_an_instruction() -> None:
    assert _is_bare_request("Collect Last 6 months business bank statements", "Last 6 months business bank statements")
    assert _is_bare_request("Please provide the current advance terms or payoff letters.", "Current advance terms or payoff letters")
    assert not _is_bare_request("Confirm the statements show no more than 3 NSF events per month", "Last 6 months business bank statements")
    # The generator's completion boilerplate restates the validation every file passes.
    assert _is_bare_request("A readable bank_statement document is linked and verified.", "Last 6 months business bank statements")
    assert _is_bare_request("A readable floorplan_mca_inventory document is linked and verified.", "Current advance terms or payoff letters")


def test_only_real_instructions_become_review_checks() -> None:
    assert _merged_review_checks([_requirement()]) == []
    assert _merged_review_checks([_requirement(completion_criteria="A readable bank_statement document is linked and verified.")]) == []
    real = _requirement(objective_text="Confirm each statement shows average daily balances above $5,000.")
    checks = _merged_review_checks([real])
    assert len(checks) == 1 and checks[0]["instructions"].startswith("Confirm each statement")
    configured = _requirement(review_checks=[{"key": "nsf", "label": "NSF count", "instructions": "No more than 3 NSFs", "severity": "block"}])
    assert [check["key"] for check in _merged_review_checks([configured])] == ["nsf"]


def test_checks_that_could_not_run_are_told_apart_from_checks_that_failed() -> None:
    assert _checks_could_not_run({"status": "unknown", "reason": "The complete evidence exceeds the AI review text limit; staff review is needed.", "checks": []})
    assert _checks_could_not_run({"status": "unknown", "reason": "AI check review is unavailable; retry or request staff verification.", "checks": []})
    assert _checks_could_not_run({"status": "unknown", "reason": "AI did not return every required check.", "checks": [{"key": "a", "status": "fail"}]}) is None
    assert _checks_could_not_run({"status": "review", "reason": "Document checks need attention.", "checks": [{"key": "a", "status": "fail"}]}) is None
    assert _checks_could_not_run(None) is None


def _analysis(classification="bank_statement"):
    return SimpleNamespace(
        status="completed", classification=classification, confidence="high", content_hash="h1", analysis_version=4,
        analysis={"key_facts": {"statement_period": "2026-05-30 to 2026-06-30", "account_holder": "Body Good Studio LLC"}, "document_type_candidates": [], "evidence_claims": [], "limitations": [], "red_flags": []},
        document_type_candidates=[], evidence_claims=[], skip_reason=None, skip_detail=None, error=None,
        analyzed_at=datetime(2026, 11, 3, tzinfo=UTC),
    )


def _file():
    return SimpleNamespace(id=uuid4(), content_hash="h1", file_name="20260630-statements-1204-.pdf", statement_period="2026-06", created_at=datetime(2026, 11, 3, tzinfo=UTC), source_kind="client_room", source_detail=None)


def test_a_validated_statement_is_accepted_when_the_grounded_checks_cannot_run() -> None:
    requirement = _requirement(review_checks=[{"key": "custom_1", "label": "Objective", "instructions": "Confirm balances", "severity": "review"}])
    assessment = {"status": "unknown", "reason": "The complete evidence exceeds the AI review text limit; staff review is needed.", "checks": []}
    decision, code, explanation, _confidence = _automatic_evidence_decision(
        requirement=requirement, file=_file(), analysis=_analysis(), expected_entity="Body Good Studio LLC",
        duplicate_content=False, corroborated_entities=set(), review_checks=requirement.review_checks,
        check_assessment=assessment, requires_human=False,
    )
    assert decision == "accepted" and code == "checks_not_run"
    assert "A person can override" in explanation

    failed = {"status": "review", "reason": "Document checks need attention.", "checks": [{"key": "custom_1", "status": "fail", "severity": "review"}]}
    decision, code, _explanation, _confidence = _automatic_evidence_decision(
        requirement=requirement, file=_file(), analysis=_analysis(), expected_entity="Body Good Studio LLC",
        duplicate_content=False, corroborated_entities=set(), review_checks=requirement.review_checks,
        check_assessment=failed, requires_human=False,
    )
    assert decision == "needs_more" and code == "document_review_pending"


def test_a_certificate_the_system_signed_is_verified_without_a_person() -> None:
    human = _requirement(requirement_key="signed_credit_authorization", verification_required=True, completion_mode="requires_human_verify")
    certificate = SimpleNamespace(source_kind="generated", source_detail="signed_document")
    upload = SimpleNamespace(source_kind="client_room", source_detail=None)
    assert _verifiable_without_a_person(human, certificate, requires_human=True) is True
    assert _verifiable_without_a_person(human, upload, requires_human=True) is False
    assert _verifiable_without_a_person(_requirement(), upload, requires_human=False) is True


def test_an_mca_refinance_budget_is_the_refinance() -> None:
    profile = SimpleNamespace(use_of_funds=[], use_of_funds_revision=0, use_of_funds_updated_at=None)
    intake = SimpleNamespace(variant="mca_refi_v1", intake_state={"mca_qualification": {"active_mca_count": 2, "requested_amount": 70000.0}})
    assert seed_refinance_budget(profile, intake, None) is True
    assert profile.use_of_funds[0]["category"] == "debt_refinance" and Decimal(str(profile.use_of_funds[0]["amount"])) == Decimal("70000")
    assert profile.use_of_funds_revision == 1
    # Once set, or once the desk has touched it, nothing is overwritten.
    assert seed_refinance_budget(profile, intake, Decimal("80000")) is False
    other = SimpleNamespace(use_of_funds=[], use_of_funds_revision=0, use_of_funds_updated_at=None)
    assert seed_refinance_budget(other, SimpleNamespace(variant="main_street_v1", intake_state={}), Decimal("50000")) is False
