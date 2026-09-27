from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.schemas.application_profile import ApplicationRequirementPatch
from app.schemas.funding_program import (
    FundingProgramRequirementWrite,
    FundingProgramScopeWrite,
    FundingProgramVersionCreate,
)
from app.services import application_programs, funding_programs
from app.services.ai.plan_builder import _compute_readiness_score
from app.services.ai.visibility_filter import filter_facts
from app.services.application_programs import (
    _automatic_evidence_decision,
    _catalog_scope_match,
    _coverage_for_files,
    _expected_classes,
    _merged_review_checks,
    _requires_human_verification,
)

CHECK = {
    "key": "net_income_nonnegative",
    "label": "No negative earnings",
    "instructions": "Compare net income in each of the two filed years.",
    "severity": "block",
}


def scope(**values):
    return SimpleNamespace(
        vertical="dealer",
        is_active=True,
        intake_variants=[],
        intent_keys=[],
        industry_keys=[],
        naics_prefixes=[],
        required_fact_keys=[],
        **values,
    )


def test_prohibited_naics_ranges_are_validated_and_expanded():
    value = FundingProgramScopeWrite(
        vertical="dealer", excluded_naics_prefixes=[" 522 ", "524", "44-45", "522"]
    )
    assert value.excluded_naics_prefixes == ["522", "524", "44", "45"]
    for invalid in ["banking", "522110-522100", "12-999999", "522110 OR true"]:
        with pytest.raises(ValidationError):
            FundingProgramScopeWrite(vertical="dealer", excluded_naics_prefixes=[invalid])


def test_prohibited_industry_wins_over_a_second_unrestricted_route():
    rows = [scope(excluded_naics_prefixes=["522", "524"]), scope(excluded_naics_prefixes=[])]
    assert _catalog_scope_match(rows, {"vertical": "dealer", "naics_code": "522110"})[0] is False
    assert _catalog_scope_match(rows, {"vertical": "dealer", "naics_code": "524210"})[0] is False
    assert _catalog_scope_match(rows, {"vertical": "dealer", "naics_code": "441120"})[0] is True
    assert (
        _catalog_scope_match(
            rows, {"vertical": "dealer", "naics_code": "999999", "naics_code_valid": False}
        )[0]
        is None
    )


@pytest.mark.parametrize("code", [None, "", "52", "52-53", "not classified", "522110 / 441120"])
def test_exclusions_cannot_be_bypassed_by_missing_or_ambiguous_classification(code):
    assert (
        _catalog_scope_match(
            [scope(excluded_naics_prefixes=["522"])],
            {
                "vertical": "dealer",
                "naics_code": code,
                "industry_key": "retail",
            },
        )[0]
        is None
    )


def test_old_inclusive_and_new_exclusive_scopes_are_both_enforced():
    row = scope(excluded_naics_prefixes=["48412"])
    row.naics_prefixes = ["484"]
    assert _catalog_scope_match([row], {"vertical": "dealer", "naics_code": "484121"})[0] is False
    assert _catalog_scope_match([row], {"vertical": "dealer", "naics_code": "484110"})[0] is True
    assert _catalog_scope_match([row], {"vertical": "dealer", "naics_code": "441120"})[0] is False


def test_document_checks_are_bounded_unique_and_allow_multiple_stable_custom_keys():
    base = {"requirement_key": "business_tax_returns_2_years", "label": "Two tax returns"}
    value = FundingProgramRequirementWrite(
        **base,
        review_checks=[
            CHECK,
            {**CHECK, "key": "custom_debt", "label": " Debt review "},
            {**CHECK, "key": "custom_addbacks"},
        ],
    )
    assert value.review_checks[1].label == "Debt review"
    assert value.model_dump()["review_checks"][0] == CHECK
    for checks in [
        [CHECK, CHECK],
        [{**CHECK, "key": "invented_automatic_check"}],
        [{**CHECK, "instructions": " "}],
        [{**CHECK, "severity": "accept"}],
        [{**CHECK, "key": f"custom_{i}"} for i in range(21)],
    ]:
        with pytest.raises(ValidationError):
            FundingProgramRequirementWrite(**base, review_checks=checks)


@pytest.mark.parametrize("severity", ["review", "block"])
def test_readable_document_never_satisfies_configured_financial_traits(severity):
    requirement = SimpleNamespace(
        requirement_key="business_tax_returns_2_years",
        label="Tax returns",
        category="financials",
        verification_required=False,
        completion_mode="ai_can_complete",
        review_checks=[{**CHECK, "severity": severity}],
    )
    file = SimpleNamespace(
        file_name="Business tax return 2025.pdf", content_hash="hash", statement_period=None
    )
    analysis = SimpleNamespace(
        status="completed",
        content_hash="hash",
        confidence="high",
        classification="tax_return",
        analysis={"key_facts": {"tax_year": "2025", "net_income": 100000}},
    )
    decision = _automatic_evidence_decision(
        requirement=requirement,
        file=file,
        analysis=analysis,
        expected_entity=None,
        duplicate_content=False,
    )
    assert decision[:2] == ("needs_more", "document_review_pending")
    assert "staff verification" in decision[2]
    assert _requires_human_verification(requirement) is True


def test_selected_program_checks_are_unioned_not_overwritten():
    checks = _merged_review_checks(
        [
            SimpleNamespace(review_checks=[CHECK]),
            SimpleNamespace(review_checks=[]),
            SimpleNamespace(
                review_checks=[CHECK, {**CHECK, "instructions": "Compare three filed years."}]
            ),
        ]
    )
    assert len(checks) == 2
    assert checks[0] == CHECK


def test_public_visibility_strips_private_traits_from_otherwise_visible_upload_request():
    item = {
        "requirement_key": "tax_returns",
        "visibility": "client_visible",
        "review_checks": [CHECK],
    }
    assert "review_checks" not in filter_facts([item], "borrower")[0]
    assert filter_facts([item], "underwriter")[0]["review_checks"] == [CHECK]
    assert item["review_checks"] == [CHECK]


def test_plan_readiness_requires_verification_of_document_traits():
    item = {"required_level": "required", "review_checks": [CHECK]}
    assert _compute_readiness_score([{**item, "status": "uploaded"}]) == 0
    assert _compute_readiness_score([{**item, "status": "provided_unverified"}]) == 0
    assert _compute_readiness_score([{**item, "status": "verified"}]) == 100


@pytest.mark.asyncio
async def test_funding_version_creation_forces_verification_and_round_trips_traits():
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)),
        add=Mock(),
        flush=AsyncMock(),
    )
    payload = FundingProgramVersionCreate(
        requirements=[
            {
                "requirement_key": "business_tax_returns_2_years",
                "label": "Two filed tax years",
                "review_checks": [CHECK],
                "verification_required": False,
                "completion_mode": "ai_can_complete",
            }
        ],
        reason="Approved reviewer criteria",
        confirmed=True,
    )
    program = SimpleNamespace(
        id=uuid4(), program_key="sba_7a", name="SBA 7(a)", short_description="Capabilities"
    )
    playbook = await funding_programs.create_version(
        db, program, payload, SimpleNamespace(id=uuid4())
    )
    requirement = db.add.call_args_list[1].args[0]
    assert requirement.review_checks == [CHECK]
    assert requirement.verification_required is True
    assert requirement.completion_mode == "requires_human_verify"
    playbook.id = uuid4()
    result = funding_programs._version_read(playbook, [requirement])
    assert result.requirements[0].review_checks[0].model_dump() == CHECK


@pytest.mark.parametrize(
    "kind,count,periods,complete",
    [
        ("bank_statements", 3, [1, 2, 2], False),
        ("bank_statements", 3, [1, 2, 3], True),
        ("tax_returns", 3, [2023, 2024, 2024], False),
        ("tax_returns", 3, [2023, 2024, 2025], True),
        ("tax_returns", 1, [2025], True),
    ],
)
def test_baseline_period_coverage_is_distinct_and_not_one_file(kind, count, periods, complete):
    unit = "months" if kind == "bank_statements" else "years"
    requirement = SimpleNamespace(
        requirement_key=f"business_{kind}_{count}_{unit}",
        label="Business evidence",
        category="financials",
    )
    files = [
        SimpleNamespace(
            id=uuid4(),
            statement_period=None,
            file_name=f"Statement 2026-{period:02d}.pdf"
            if unit == "months"
            else f"Tax return {period}.pdf",
        )
        for period in periods
    ]
    result, coverage = _coverage_for_files(requirement, files, {})
    assert result is complete
    assert coverage["required"] == count
    assert coverage["current"] == len(set(periods))
    assert _expected_classes(requirement) == {
        "bank_statement" if unit == "months" else "tax_return"
    }


@pytest.mark.parametrize("policy_changed", [True, False])
@pytest.mark.asyncio
async def test_new_review_policy_invalidates_prior_staff_acceptance(monkeypatch, policy_changed):
    file_id, link_id = uuid4(), uuid4()
    staff = SimpleNamespace(
        id=uuid4(),
        actor_kind="staff",
        content_hash="hash",
        analysis_version=1,
        policy_version=1,
        decision="accepted",
    )
    link = SimpleNamespace(
        id=link_id,
        file_id=file_id,
        verified_at="earlier",
        verified_by_user_id=uuid4(),
        reason="Prior staff review",
    )
    file = SimpleNamespace(id=file_id, content_hash="hash")
    analysis = SimpleNamespace(id=uuid4(), analysis_version=1, analyzed_at=None)
    monkeypatch.setattr(
        application_programs, "_latest_evidence_decisions", AsyncMock(return_value={link_id: staff})
    )
    monkeypatch.setattr(
        application_programs,
        "_automatic_evidence_decision",
        Mock(
            return_value=(
                "needs_more",
                "document_review_pending",
                "New traits need staff verification",
                "high",
            )
        ),
    )
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)),
        add=Mock(),
        flush=AsyncMock(),
    )
    result = await application_programs._reconcile_evidence_decisions(
        db,
        requirement=SimpleNamespace(verification_required=False),
        links=[link],
        inventory={file_id: file},
        analyses={file_id: analysis},
        expected_entity=None,
        corroborated_entities=set(),
        criteria_version=2,
        review_checks=[CHECK],
        review_policy_changed=policy_changed,
    )
    assert result[link_id].decision == "needs_more"
    assert link.verified_at is None and link.verified_by_user_id is None
    db.add.assert_called_once()


def _verify_fixture(monkeypatch):
    file_id, link_id = uuid4(), uuid4()
    user = SimpleNamespace(id=uuid4())
    file = SimpleNamespace(id=file_id, content_hash="current-hash")
    analysis = SimpleNamespace(
        id=uuid4(),
        content_hash="current-hash",
        analysis_version=3,
        status="completed",
        classification="tax_return",
        confidence="high",
        analyzed_at=None,
    )
    current = SimpleNamespace(
        id=uuid4(),
        actor_kind="ai",
        content_hash="current-hash",
        analysis_version=3,
        policy_version=2,
        decision="needs_more",
        reason_code="document_review_pending",
        idempotency_key="prior",
    )
    link = SimpleNamespace(id=link_id, file_id=file_id, verified_at=None, verified_by_user_id=None)
    state = SimpleNamespace(
        id=uuid4(),
        verification_required=True,
        provenance={"review_checks_fingerprint": "traits-hash"},
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(scalar_one_or_none=lambda: state),
                SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [link])),
                SimpleNamespace(scalar_one_or_none=lambda: None),
            ]
        ),
        add=Mock(),
        flush=AsyncMock(),
    )
    monkeypatch.setattr(
        application_programs,
        "get_program_readiness",
        AsyncMock(
            return_value=SimpleNamespace(
                requirements=[SimpleNamespace(requirement_key="business_tax_returns_2_years")]
            )
        ),
    )
    monkeypatch.setattr(
        application_programs,
        "_evidence_inventory",
        AsyncMock(return_value=([file], {file_id: analysis}, set())),
    )
    monkeypatch.setattr(
        application_programs,
        "_latest_evidence_decisions",
        AsyncMock(return_value={link_id: current}),
    )
    return db, file, analysis, current, link, user


@pytest.mark.asyncio
async def test_generic_verify_writes_bound_staff_decision_and_unchanged_review_is_retained(
    monkeypatch,
):
    db, file, analysis, current, link, user = _verify_fixture(monkeypatch)
    await application_programs.patch_requirement(
        db,
        SimpleNamespace(id=uuid4()),
        "business_tax_returns_2_years",
        ApplicationRequirementPatch(action="verify", confirmed=True),
        user,
    )
    decision = db.add.call_args.args[0]
    assert decision.actor_kind == "staff" and decision.actor_user_id == user.id
    assert decision.content_hash == file.content_hash and decision.analysis_version == 3
    assert decision.policy_version == 2 and decision.supersedes_decision_id == current.id
    assert decision.decision == "accepted" and link.verified_by_user_id == user.id
    reviewed_at = link.verified_at
    decision.id = uuid4()
    monkeypatch.setattr(
        application_programs,
        "_latest_evidence_decisions",
        AsyncMock(return_value={link.id: decision}),
    )
    db.add.reset_mock()
    await application_programs._reconcile_evidence_decisions(
        db,
        requirement=SimpleNamespace(verification_required=True),
        links=[link],
        inventory={file.id: file},
        analyses={file.id: analysis},
        expected_entity=None,
        corroborated_entities=set(),
        criteria_version=2,
        review_checks=[CHECK],
        review_policy_changed=False,
    )
    assert link.verified_at == reviewed_at and link.verified_by_user_id == user.id
    db.add.assert_not_called()
    # Removing verification must not auto-restore the retained staff decision.
    link.verified_at = None
    link.verified_by_user_id = None
    await application_programs._reconcile_evidence_decisions(
        db,
        requirement=SimpleNamespace(verification_required=True),
        links=[link],
        inventory={file.id: file},
        analyses={file.id: analysis},
        expected_entity=None,
        corroborated_entities=set(),
        criteria_version=2,
        review_checks=[CHECK],
        review_policy_changed=False,
    )
    assert link.verified_at is None


@pytest.mark.parametrize(
    "issue", ["locked", "processing", "failed", "unreadable", "rejected", "wrong_period", "stale"]
)
@pytest.mark.asyncio
async def test_generic_verify_cannot_bypass_unreadable_pending_or_rejected_evidence(
    monkeypatch, issue
):
    db, file, analysis, current, link, user = _verify_fixture(monkeypatch)
    if issue == "locked":
        analysis.status = "skipped"
    elif issue in {"processing", "failed"}:
        analysis.status = issue
    elif issue == "unreadable":
        analysis.classification = "unreadable"
    elif issue == "rejected":
        current.decision = "rejected"
    elif issue == "wrong_period":
        current.reason_code = "wrong_period"
    else:
        analysis.content_hash = "old-content"
    with pytest.raises(HTTPException) as error:
        await application_programs.patch_requirement(
            db,
            SimpleNamespace(id=uuid4()),
            "business_tax_returns_2_years",
            ApplicationRequirementPatch(action="verify", confirmed=True),
            user,
        )
    assert error.value.status_code == 409 and "override" in error.value.detail
    assert link.verified_at is None
    db.add.assert_not_called()


@pytest.mark.parametrize("change", ["none", "hash", "analysis_version", "analysis_timestamp"])
@pytest.mark.asyncio
async def test_legacy_timestamp_verification_preserves_unchanged_work_but_not_changed_evidence(
    monkeypatch, change
):
    db, file, analysis, current, link, user = _verify_fixture(monkeypatch)
    verified_at = datetime(2026, 9, 27, 15, 0, tzinfo=UTC)
    link.verified_at, link.verified_by_user_id, link.reason = (
        verified_at,
        user.id,
        "Legacy human verification",
    )
    analysis.analyzed_at = verified_at - timedelta(minutes=1)
    if change == "hash":
        file.content_hash = "replacement-content"
    elif change == "analysis_version":
        analysis.analysis_version = 4
    elif change == "analysis_timestamp":
        analysis.analyzed_at = verified_at + timedelta(minutes=1)
    db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
    monkeypatch.setattr(
        application_programs,
        "_automatic_evidence_decision",
        Mock(
            return_value=("needs_more", "document_review_pending", "Traits require review", "high")
        ),
    )
    await application_programs._reconcile_evidence_decisions(
        db,
        requirement=SimpleNamespace(verification_required=True),
        links=[link],
        inventory={file.id: file},
        analyses={file.id: analysis},
        expected_entity=None,
        corroborated_entities=set(),
        criteria_version=2,
        review_checks=[CHECK],
        review_policy_changed=False,
    )
    if change == "none":
        assert link.verified_at == verified_at and link.verified_by_user_id == user.id
    else:
        assert link.verified_at is None and link.verified_by_user_id is None
