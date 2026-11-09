from contextlib import nullcontext
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.routers.document_associations import (
    DocumentAssociationWrite,
    read_associations,
    review_association,
)
from app.services import document_associations as identity
from app.services import financial_cockpit, financial_metric_review
from app.services.financial_metric_reporting import metric_brief
from app.services.program_earnings_facts import annual_ebitda_fact
from app.tests.test_financial_metric_review import ACTOR


def sources(kind="tax_return", *, observed="Example HOMECARE", facts=None):
    profile = SimpleNamespace(id=uuid4(), intake_id=uuid4(), client_id=uuid4(), dealer_id=None, loan_id=None, deal_id=None,
                              financial_metric_review={})
    file = SimpleNamespace(id=uuid4(), file_name="2025 Returns.pdf", content_hash="a" * 64, bucket_id=uuid4(),
                           document_family_id=uuid4(), document_version_number=2, is_current_document_version=True,
                           document_version_status="current", deleted_at=None, created_at=datetime(2026, 11, 1, tzinfo=UTC))
    data = facts or {"entity_name": observed, "tax_year": "2025", "tax_form": "1120S", "ordinary_business_income": 40000,
                     "interest": 12000, "depreciation_and_amortization": 5000}
    analysis = SimpleNamespace(id=uuid4(), bucket_file_id=file.id, content_hash=file.content_hash, analysis_version=7,
                               classification=kind, status="completed", confidence="high", analyzed_at=file.created_at,
                               analysis={"key_facts": data})
    return profile, file, analysis


def record(profile, file, analysis, state=None, expected="Example Home Care"):
    return identity.record_for_source(profile.id, expected, file, analysis, state or {}, owner=identity.owner_context(profile))


def decide(profile, file, analysis, state=None, action="accept", **changes):
    state = state or {}
    target = record(profile, file, analysis, state)
    payload = dict(file_id=file.id, action=action, reason="Reviewed the exact source PDF", expected_revision=state.get("revision", 0),
                   source_stamp=target["source_stamp"], idempotency_key=f"identity-{action}-{state.get('revision', 0)}", actor=ACTOR)
    return identity.change(state, {str(file.id): target}, **{**payload, **changes})[0]


def cockpit(profile, file, analysis, state=None, *, debts=None, **extra):
    state = state or {}
    association = record(profile, file, analysis, state)
    rows = financial_cockpit._analysis_rows([file], {file.id: analysis}, {file.id}, {str(file.id): association})
    return financial_cockpit.build_cockpit(rows, debts or [{"id": "loan", "lender": "Funder", "monthly_payment": 1000}],
        business_entity="Example Home Care", profile_id=str(profile.id), association_owner=identity.owner_context(profile),
        metric_review_state=state, **extra)


def test_exact_orm_to_financial_row_roundtrip_confirms_denies_and_resets_without_mutating_source():
    profile, file, analysis = sources()
    original = deepcopy(analysis.analysis)
    initial = cockpit(profile, file, analysis)
    assert initial["ebitda"]["headline"] is None
    accepted = decide(profile, file, analysis)
    reviewed = cockpit(profile, file, analysis, accepted)
    assert reviewed["metric_review"]["document_associations"][0]["source_stamp"] == record(profile, file, analysis)["source_stamp"]
    assert reviewed["ebitda"]["headline"]["value"] == 57000 and reviewed["dscr"]["headline"]["value"] == 4.75
    assert reviewed["tax"]["years"][0]["entity"] == "Example HOMECARE"
    denied = decide(profile, file, analysis, accepted, "deny")
    assert cockpit(profile, file, analysis, denied)["ebitda"]["headline"] is None
    reset = decide(profile, file, analysis, denied, "reset")
    result = cockpit(profile, file, analysis, reset)
    assert result["ebitda"]["headline"] is None
    assert result["metric_review"]["document_associations"][0]["status"] == "conflict"
    assert len(result["metric_review"]["document_associations"][0]["history"]) == 3
    assert analysis.analysis == original


def test_profile_fact_only_identity_model_classification_and_version_metadata_keep_identical_stamps():
    profile, file, analysis = sources(facts={"tax_year": "2025", "ordinary_business_income": 40000})
    analysis.analysis["profile_facts"] = {"legal_entity_name": {"value": "Example HOMECARE"}}
    accepted = decide(profile, file, analysis)
    assert cockpit(profile, file, analysis, accepted)["metric_review"]["document_associations"][0]["status"] == "accepted"
    before = record(profile, file, analysis, accepted)
    rows = financial_cockpit._analysis_rows([file], {file.id: analysis}, {file.id}, {str(file.id): before})
    denied = decide(profile, file, analysis, accepted, "deny")
    updated = identity.attach_rows(rows, profile_id=profile.id, expected_entity="Example Home Care", state=denied,
                                   owner=identity.owner_context(profile))
    assert updated[0]["identity_association"]["status"] == "denied"


@pytest.mark.parametrize("change", ["bytes", "analysis", "owner", "name", "version", "replacement"])
def test_receipt_never_carries_over_changed_source_or_profile_owner(change):
    profile, file, analysis = sources()
    state = decide(profile, file, analysis)
    if change == "bytes":
        file.content_hash = analysis.content_hash = "b" * 64
    elif change == "analysis":
        analysis.id = uuid4()
    elif change == "owner":
        profile.client_id = uuid4()
    elif change == "name":
        analysis.analysis["key_facts"]["entity_name"] = "Different Business"
    elif change == "version":
        file.document_version_number += 1
    else:
        file.id = uuid4()
        file.document_version_number += 1
    reviewed = record(profile, file, analysis, state)
    assert reviewed["status"] == "stale" and not reviewed["allows_financial_use"]
    assert reviewed["current_decision"] is None and reviewed["history"][0]["stale"]


def test_denial_beats_exact_name_and_removes_bank_activity_and_provisional_contract_debts():
    profile, file, analysis = sources("bank_statement", facts={"business_name": "Example Home Care", "bank": "Bank", "account_last4": "1234",
        "statement_period": "2026-01", "total_deposits_and_credits": 10000, "total_withdrawals_and_debits": 7000,
        "transactions": [{"date": "2026-01-02", "amount": -100, "description": "Payment"}]})
    denied = decide(profile, file, analysis, action="deny")
    result = cockpit(profile, file, analysis, denied)
    assert result["bank"]["summary"]["avg_deposits"] is None and result["bank"]["months"] == []
    assert result["metric_review"]["document_associations"][0]["status"] == "denied"
    profile2, contract, contract_analysis = sources("collateral_debt_evidence", facts={"business_name": "Example Home Care",
        "debts": [{"lender": "Denied Funder", "monthly_payment": 9999, "balance": 100000}]})
    state = decide(profile2, contract, contract_analysis, action="deny")
    report = cockpit(profile2, contract, contract_analysis, state)
    assert "Denied Funder" not in str(report["debt"]["documented"])


def test_association_accept_does_not_make_personal_tax_or_incomplete_pl_eligible_or_create_aliases():
    profile, file, analysis = sources(facts={"entity_name": "Example HOMECARE", "tax_form": "1040", "tax_year": "2025", "net_income": 50000})
    state = decide(profile, file, analysis)
    assert cockpit(profile, file, analysis, state)["ebitda"]["headline"] is None
    assert set(state) == {"revision", "events"}
    profile2, pnl_file, pnl_analysis = sources("current_p_and_l", facts={"business_name": "Example HOMECARE", "basis": "accrual", "currency": "USD",
        "period_start": "2026-01-01", "period_end": "2026-12-31", "net_income": 50000})
    accepted = decide(profile2, pnl_file, pnl_analysis)
    report = cockpit(profile2, pnl_file, pnl_analysis, accepted)
    assert report["ebitda"]["headline"] is None
    assert report["ebitda"]["candidates"][0]["missing_inputs"]


def test_confirmation_waives_only_identity_for_complete_verified_pl_program_basis():
    profile, file, analysis = sources("current_p_and_l", facts={"business_name": "Example HOMECARE", "basis": "accrual", "currency": "USD",
        "period_start": "2026-01-01", "period_end": "2026-12-31", "net_income": 40000, "interest": 12000,
        "income_taxes": 3000, "depreciation_and_amortization": 5000})
    accepted = decide(profile, file, analysis)
    report = cockpit(profile, file, analysis, accepted)
    assert report["ebitda"]["headline"]["value"] == 60000
    association = record(profile, file, analysis, accepted)
    rows = financial_cockpit._analysis_rows([file], {file.id: analysis}, {file.id}, {str(file.id): association})
    assert annual_ebitda_fact(rows, entity="Example Home Care", state=accepted)[0] == 60000
    rows[0]["accepted"] = False
    assert annual_ebitda_fact(rows, entity="Example Home Care", state=accepted)[0] is None


def test_replay_revision_stamp_reason_and_external_audit_guards():
    profile, file, analysis = sources()
    original = record(profile, file, analysis)
    state = decide(profile, file, analysis)
    replay = decide(profile, file, analysis, state, expected_revision=0, idempotency_key="identity-accept-0")
    assert replay == state
    for changes in [{"expected_revision": 0}, {"source_stamp": "b" * 64}, {"reason": " "}]:
        with pytest.raises(HTTPException):
            decide(profile, file, analysis, state, "deny", **changes)
    assert original["status"] == "conflict"
    state["events"][0].update(reason="PRIVATE-REASON", actor_name="PRIVATE-ACTOR")
    report = cockpit(profile, file, analysis, state)
    safe = financial_metric_review.redact_for_external(report, SimpleNamespace(role=Role.DEALER_PARTNER))
    assert safe["metric_review"]["document_associations"] == []
    assert "PRIVATE-REASON" not in str(safe) and "PRIVATE-ACTOR" not in str(safe)
    assert "PRIVATE-REASON" not in str(metric_brief(report))
    assert "PRIVATE-REASON" not in str(identity.display_record(record(profile, file, analysis, state)))


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [Role.CLIENT, Role.DEALER_PARTNER, Role.PROFESSIONAL_REFERRAL_PARTNER])
async def test_external_read_and_write_are_blocked_before_profile_lookup(role):
    with patch("app.routers.document_associations.application_profiles.load_profile", AsyncMock()) as load:
        with pytest.raises(HTTPException) as read:
            await read_associations(uuid4(), SimpleNamespace(role=role), AsyncMock())
        with pytest.raises(HTTPException) as write:
            await review_association(uuid4(), uuid4(), DocumentAssociationWrite(action="accept", reason="Reviewed", expected_revision=0,
                source_stamp="a" * 64, idempotency_key="identity-review"), SimpleNamespace(role=role), AsyncMock())
    assert read.value.status_code == write.value.status_code == 403
    load.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["revision", "client", "intake", "name", "unchanged"])
async def test_inflight_model_review_cannot_publish_after_association_revision_or_owner_changes(changed):
    profile, _file, _analysis = sources()
    captured = identity.review_context_stamp(profile, "Example Home Care")
    if changed == "revision":
        profile.financial_metric_review = {"revision": 1, "events": [{"action": "deny"}]}
    elif changed == "client":
        profile.client_id = uuid4()
    elif changed == "intake":
        profile.intake_id = uuid4()
    with patch.object(identity, "expected_business", AsyncMock(return_value="Different Owner" if changed == "name" else "Example Home Care")):
        assert await identity.review_context_current(AsyncMock(), profile, captured) == (changed == "unchanged")


def test_denied_immutable_version_is_retained_without_exporting_its_financial_figures():
    item = {"kind": "debt_schedule", "version_id": "v2", "source_file_id": "generated", "original_source_file_id": "original",
            "key_facts": {"monthly_payment": 9999}, "authoritative": True, "debt_service_for_dscr": 9999, "debt_rows": [{"balance": 1000}]}
    before = deepcopy(item)
    result = identity.version_projection(item, {"original": {"status": "denied"}})
    assert result["version_id"] == "v2" and result["key_facts"] == {} and not result["authoritative"]
    assert "debt_service_for_dscr" not in result and "debt_rows" not in result
    assert result["identity_association_conflict"] and item == before


def test_denied_financial_facts_are_not_sent_to_raw_ai_synthesis():
    from app.services.financial_prompt_payload import summary_analyses

    raw = [{"file_id": "denied", "file_name": "Retained.pdf", "ai_classification": "tax_return", "key_facts": {"net_income": 999999},
            "summary": "Profit 999999", "identity_association": {"status": "denied"}}]
    assert "999999" not in str(summary_analyses(raw))


@pytest.mark.parametrize("status,decision,code", [("denied", "rejected", "entity_association_denied"), ("stale", "needs_more", "entity_association_stale")])
def test_association_denial_and_stale_decisions_override_an_automatic_exact_name_acceptance(status, decision, code):
    from app.services.application_programs import _automatic_evidence_decision
    from app.tests.test_ai_accepts_validated_evidence import _analysis, _file, _requirement

    args = dict(requirement=_requirement(), file=_file(), analysis=_analysis(), expected_entity="Body Good Studio LLC", duplicate_content=False)
    assert _automatic_evidence_decision(**args)[0] == "accepted"
    assert _automatic_evidence_decision(**args, identity_association={"status": status})[:2] == (decision, code)


@pytest.mark.parametrize("issue,expected", [("unreadable", "unreadable"), ("wrong_type", "wrong_document"), ("missing_period", "wrong_period")])
def test_association_confirmation_does_not_waive_unrelated_evidence_validation(issue, expected):
    from app.services.application_programs import _automatic_evidence_decision
    from app.tests.test_ai_accepts_validated_evidence import _analysis, _file, _requirement

    file, analysis = _file(), _analysis()
    if issue == "unreadable":
        analysis.classification = "unreadable"
    elif issue == "wrong_type":
        analysis.classification = "insurance"
    else:
        analysis.analysis["key_facts"].pop("statement_period")
        file.statement_period = None
    assert _automatic_evidence_decision(requirement=_requirement(), file=file, analysis=analysis, expected_entity="Different name",
        duplicate_content=False, identity_association={"status": "accepted"})[1] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("issue", ["save_and_replay", "foreign_scope", "newer_version", "stale_revision"])
async def test_save_boundary_scopes_before_locks_then_guards_latest_source_and_shared_revision(issue):
    profile, file, analysis = sources()
    query = Mock(scalar_one=Mock(return_value=profile))
    db = SimpleNamespace(no_autoflush=nullcontext(), get=AsyncMock(return_value=file), refresh=AsyncMock(),
                         execute=AsyncMock(return_value=query))
    events = []
    command = dict(file_id=file.id, action="accept", reason="Confirmed source ownership", expected_revision=0,
                   source_stamp=record(profile, file, analysis)["source_stamp"], idempotency_key="identity-save-boundary")
    if issue == "stale_revision":
        profile.financial_metric_review = {"revision": 1, "events": []}

    async def current(*_args):
        return {"documents": [record(profile, file, analysis, profile.financial_metric_review)],
                "revision": profile.financial_metric_review.get("revision", 0)}

    async def persist(_db, saved, _actor, state, **_kwargs):
        saved.financial_metric_review = state
        events.append("persist")

    async def document_lock(*_args):
        events.append("document")

    async def profile_lock(*_args):
        events.append("profile")

    with (patch("app.services.application_profiles._profile_evidence_file_ids", AsyncMock(return_value=set() if issue == "foreign_scope" else {file.id})),
          patch("app.services.document_version_selection.lock_document_roots", AsyncMock(side_effect=document_lock)),
          patch("app.services.document_version_selection.document_file_ids", AsyncMock(return_value=set() if issue == "newer_version" else {file.id})),
          patch("app.services.financial_workspace.lock_financial_profile", AsyncMock(side_effect=profile_lock)),
          patch.object(identity, "read", AsyncMock(side_effect=current)),
          patch("app.services.financial_metric_review._persist_review", AsyncMock(side_effect=persist)),
          patch("app.services.document_versions.complete_analysis", AsyncMock()) as promote,
          patch("app.services.document_versions.mark_received", AsyncMock()) as reject):
        if issue == "save_and_replay":
            result = await identity.save(db, profile, ACTOR, **command)
            replay = await identity.save(db, profile, ACTOR, **command)
            assert result["revision"] == replay["revision"] == 1
            assert events == ["document", "profile", "persist", "document", "profile"]
        else:
            with pytest.raises(HTTPException) as caught:
                await identity.save(db, profile, ACTOR, **command)
            assert caught.value.status_code == (404 if issue == "foreign_scope" else 409)
            assert "persist" not in events
        promote.assert_not_awaited()
        reject.assert_not_awaited()
    assert file.document_version_status == "current" and file.is_current_document_version
