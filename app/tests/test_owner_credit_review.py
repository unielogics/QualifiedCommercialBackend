from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.enums import Role
from app.routers import application_profiles as routes
from app.schemas.application_profile import (
    ApplicationRequirementPatch,
    FileOwnerReview,
    OwnerCreditRequirementReview,
)
from app.services import application_programs
from app.services import credit_requirement_sync as sync
from app.services import owner_credit_review as service
from app.services.funding_program_baselines import CATALOG
from app.tests.test_credit_requirement_sync import (
    NOW,
    Rows,
    owner,
    profile,
    pull,
    requirement,
    state,
)


def quidity():
    raw = next(row for row in CATALOG["quidity_10_year_term"]["docs"] if row["requirement_key"] == "guarantor_credit_review")
    return requirement(**{key: deepcopy(value) for key, value in raw.items() if key not in {"id"}})


def exact_report(person, score=700, **changes):
    return pull(person, fico=score, bureau_response={"parsed_report": {"scores": [{"model": "FICO 9", "bureau": "Equifax", "score": score}]}}, **changes)


def collected(person, report, req=None):
    return sync.collection_snapshot(profile(), [person], {str(report.id): report}, req or quidity(), now=NOW)


def db_mock():
    return SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), flush=AsyncMock())


def patch_services(monkeypatch, owners):
    monkeypatch.setattr(service, "lock_review_sources", AsyncMock(return_value=owners))
    monkeypatch.setattr(service, "lock_credit_policy", AsyncMock())
    audit = AsyncMock()
    monkeypatch.setattr(service.profiles, "log_profile_action", audit)
    monkeypatch.setattr(service.profiles, "owner_read", lambda row: row)
    return audit


def test_real_quidity_baseline_allows_only_confirmed_exact_bureau_model_score():
    person = owner()
    result = collected(person, exact_report(person))
    assert result["can_mark_met"] is True and result["review_blockers"] == []
    assert result["required_minimum_score"] == 680 and result["required_score_model"] == "Equifax FICO 9"
    assert result["owners"][0]["required_score"] == 700
    person.backfill_needs_review = True
    assert collected(person, exact_report(person))["can_mark_met"] is False


def test_body_good_style_fico4_unknown_bureau_is_collected_but_not_qualifying():
    person = owner(backfill_needs_review=True)
    report = pull(person, bureau_response={"parsed_report": {"scores": [{"model": "FICO 4", "score": 661}]}})
    result = collected(person, report)
    assert result["complete"] is True and result["client_action_required"] is False
    assert result["can_mark_met"] is False and result["owners"][0]["required_score"] is None
    assert len(result["review_blockers"]) >= 2


@pytest.mark.parametrize("change", [
    {"collection_config": {"credit_bureau": "Experian", "credit_model": "FICO 8"}},
    {"completion_criteria": "Require Experian FICO 8 at 680 and Equifax FICO 9 at 680"},
    {"review_checks": [{"key": "custom_credit", "label": "TransUnion FICO 9 at 680", "instructions": "Also verify this model", "severity": "block"}]},
    {"collection_config": {"minimum_credit_score": 720}},
])
def test_conflicting_published_model_or_floor_cannot_be_marked_met(change):
    person, req = owner(), quidity()
    for key, value in change.items():
        setattr(req, key, value)
    assert collected(person, exact_report(person, 800), req)["can_mark_met"] is False


@pytest.mark.parametrize("score,allowed", [(679, False), (680, True), (681, True)])
def test_actual_quidity_floor_is_inclusive(score, allowed):
    person = owner()
    assert collected(person, exact_report(person, score))["can_mark_met"] is allowed


@pytest.mark.parametrize("model", [FileOwnerReview, OwnerCreditRequirementReview])
def test_review_requires_explicit_confirmation_reason_and_current_fingerprint(model):
    valid = {"expected_fingerprint": "a" * 64, "reason": "Reviewed current source", "confirmed": True}
    if model is OwnerCreditRequirementReview:
        valid["decision"] = "incomplete"
    for key, value in [("reason", "   "), ("confirmed", False), ("expected_fingerprint", "old")]:
        with pytest.raises(ValidationError):
            model(**{**valid, key: value})


@pytest.mark.parametrize("role", [Role.DEALER_PARTNER, Role.FIELD_REP, Role.BROKER])
@pytest.mark.asyncio
async def test_review_routes_reject_non_underwriting_roles_before_loading_data(role):
    actor = SimpleNamespace(role=role)
    for action, body in [(routes.review_imported_owner, FileOwnerReview(expected_fingerprint="a" * 64, reason="Reviewed current source", confirmed=True)),
                         (routes.review_linked_owner_credit, OwnerCreditRequirementReview(expected_fingerprint="a" * 64, reason="Reviewed current source", confirmed=True, decision="incomplete"))]:
        with pytest.raises(HTTPException) as error:
            await action(uuid4(), uuid4(), body, actor, db_mock())
        assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_owner_confirmation_is_explicit_audited_idempotent_and_never_approves_credit(monkeypatch):
    person = owner(backfill_needs_review=True)
    person.full_name = "Pat Owner"
    audit = patch_services(monkeypatch, [person])
    readiness = AsyncMock()
    monkeypatch.setattr(application_programs, "get_program_readiness", readiness)
    body = FileOwnerReview(expected_fingerprint=sync.owner_review_fingerprint(person), reason="Confirmed actual owner and percentage", confirmed=True)
    db, actor = db_mock(), SimpleNamespace(id=uuid4())
    await service.confirm_owner(db, profile(), person.id, body, actor)
    assert person.backfill_needs_review is False and audit.await_count == 1
    assert audit.call_args.kwargs["metadata"]["credit_qualification_approved"] is False
    assert sync.owner_review_fingerprint(person) == body.expected_fingerprint
    await service.confirm_owner(db, profile(), person.id, body, actor)
    assert audit.await_count == 1 and readiness.await_count == 1
    assert collected(person, pull(person))["can_mark_met"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("change,code", [({"first_name": "Changed"}, 409), ({"ownership_pct": None}, 422), ({"ownership_pct": 60}, 422)])
async def test_owner_review_rejects_stale_or_incomplete_roster(monkeypatch, change, code):
    person = owner(backfill_needs_review=True)
    body = FileOwnerReview(expected_fingerprint=sync.owner_review_fingerprint(person), reason="Reviewed current owner", confirmed=True)
    for key, value in change.items():
        setattr(person, key, value)
    if code == 422:
        body.expected_fingerprint = sync.owner_review_fingerprint(person)
    audit = patch_services(monkeypatch, [person])
    with pytest.raises(HTTPException) as error:
        await service.confirm_owner(db_mock(), profile(), person.id, body, SimpleNamespace(id=uuid4()))
    assert error.value.status_code == code and person.backfill_needs_review is True
    audit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,expected", [("met", "verified"), ("incomplete", "received_unverified"), ("failed", "failed")])
async def test_credit_decisions_are_audited_bound_and_idempotent(monkeypatch, decision, expected):
    prof, person, actor = profile(), owner(), SimpleNamespace(id=uuid4())
    req, report = quidity(), exact_report(person)
    receipt = sync.collection_snapshot(prof, [person], {str(report.id): report}, req, now=NOW)
    row = state(prof, provenance={"credit_collection": receipt})
    audit = patch_services(monkeypatch, [person])
    ready = SimpleNamespace(requirements=[SimpleNamespace(requirement_key="guarantor_credit_review", canonical_key="guarantor_credit_review")])
    monkeypatch.setattr(application_programs, "get_program_readiness", AsyncMock(return_value=ready))
    db = db_mock()
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: row)
    body = OwnerCreditRequirementReview(expected_fingerprint=receipt["fingerprint"], reason="Reviewed the exact linked bureau report", confirmed=True, decision=decision)
    assert await service.review_credit_requirement(db, prof, "guarantor_credit_review", body, actor) is ready
    assert row.status == expected and audit.await_count == 1
    assert row.provenance["credit_review"]["fingerprint"] == receipt["fingerprint"]
    assert row.verified_by_user_id == (actor.id if decision == "met" else None)
    await service.review_credit_requirement(db, prof, "guarantor_credit_review", body, actor)
    assert audit.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case,code", [("wrong_model", 422), ("stale", 409), ("removed", 409), ("inactive", 409)])
async def test_credit_cannot_verify_missing_model_stale_source_or_removed_policy(monkeypatch, case, code):
    prof, person, actor = profile(), owner(), SimpleNamespace(id=uuid4())
    receipt = collected(person, pull(person) if case == "wrong_model" else exact_report(person))
    row = state(prof, provenance={"credit_collection": receipt, "inactive_definition": case == "inactive"})
    audit = patch_services(monkeypatch, [person])
    ready = SimpleNamespace(requirements=[] if case == "removed" else [SimpleNamespace(requirement_key="guarantor_credit_review", canonical_key="guarantor_credit_review")])
    monkeypatch.setattr(application_programs, "get_program_readiness", AsyncMock(return_value=ready))
    db = db_mock()
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: row)
    body = OwnerCreditRequirementReview(expected_fingerprint="a" * 64 if case == "stale" else receipt["fingerprint"], reason="Reviewed the linked credit evidence", confirmed=True, decision="met")
    with pytest.raises(HTTPException) as error:
        await service.review_credit_requirement(db, prof, "guarantor_credit_review", body, actor)
    assert error.value.status_code == code and row.status != "verified"
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_policy_review_is_retained_then_invalidated_by_report_or_policy_change(monkeypatch):
    prof, person, req = profile(), owner(), quidity()
    report = exact_report(person)
    row = state(prof)
    monkeypatch.setattr(service.profiles, "owner_rows", AsyncMock(return_value=[person]))
    db = SimpleNamespace(scalars=AsyncMock(return_value=Rows([report])))
    await sync.materialize(db, prof, req, row, None)
    fingerprint = row.provenance["credit_collection"]["fingerprint"]
    row.status, row.verified_by_user_id = "verified", uuid4()
    row.provenance["credit_review"] = {"fingerprint": fingerprint, "decision": "met", "reason": "Exact report reviewed"}
    await sync.materialize(db, prof, req, row, None)
    assert row.status == "verified" and row.provenance["credit_collection"]["last_review"]["current"] is True
    report.bureau_response["parsed_report"]["scores"][0]["score"] = 660
    await sync.materialize(db, prof, req, row, None)
    assert row.status == "received_unverified" and row.verified_by_user_id is None
    assert row.provenance["credit_collection"]["last_review"]["current"] is False
    report.bureau_response["parsed_report"]["scores"][0]["score"] = 700
    req.review_checks[0]["instructions"] = "Require every owner's Equifax FICO 9 at 720 or above"
    await sync.materialize(db, prof, req, row, None)
    assert row.status != "verified" and row.provenance["credit_collection"]["can_mark_met"] is False


@pytest.mark.parametrize("model", ["Equifax FICO 9 / FICO 4", "Equifax FICO 9 VantageScore", "Equifax FICO 9 Bankcard"])
def test_ambiguous_report_model_cannot_attribute_its_score_to_equifax_fico9(model):
    person = owner()
    report = pull(person, fico=800, bureau_response={"parsed_report": {"scores": [{"model": model, "bureau": "Equifax", "score": 800}]}})
    assert collected(person, report)["can_mark_met"] is False


def test_unknown_additional_credit_check_never_becomes_a_score_floor():
    person, req = owner(), quidity()
    req.review_checks.append({"key": "custom_credit_bankruptcy", "label": "Bankruptcy review", "instructions": "No bankruptcy within 680 days", "severity": "block"})
    assert collected(person, exact_report(person), req)["can_mark_met"] is False


def test_proxy_identity_score_never_satisfies_published_requirement():
    person = owner()
    report = exact_report(person, notes="fico_source=intelligence-proxy")
    result = collected(person, report)
    assert result["complete"] is True and result["owners"][0]["score_verified"] is False
    assert result["can_mark_met"] is False


@pytest.mark.asyncio
async def test_source_locks_refresh_profile_and_pull_before_locking_owners():
    prof, person = profile(primary_bucket_id=uuid4()), owner()
    report = pull(person)
    refreshed_profile = Mock(return_value=prof)
    pull_result = Rows([report])
    pull_result.all = Mock(return_value=[report])
    db = db_mock()
    db.execute.side_effect = [object(), SimpleNamespace(scalar_one=refreshed_profile), Rows([person]), pull_result, Rows([person])]
    assert await service.lock_review_sources(db, prof) == [person]
    calls = [call.args[0] for call in db.execute.await_args_list]
    assert calls[0].get_final_froms()[0].name == "buckets"
    assert calls[1].get_execution_options()["populate_existing"] is True
    assert calls[2]._for_update_arg is None
    assert calls[3].get_final_froms()[0].name == "credit_pulls" and calls[3]._for_update_arg is not None
    assert calls[4]._for_update_arg is not None
    refreshed_profile.assert_called_once()
    pull_result.all.assert_called_once()


@pytest.mark.asyncio
async def test_changed_pull_link_during_refresh_requires_reload():
    prof, person = profile(primary_bucket_id=None), owner()
    changed = deepcopy(person)
    changed.credit_pull_id = uuid4()
    db = db_mock()
    db.execute.side_effect = [SimpleNamespace(scalar_one=lambda: prof), Rows([person]), Rows([]), Rows([changed])]
    with pytest.raises(HTTPException) as error:
        await service.lock_review_sources(db, prof)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_policy_rows_are_refreshed_and_locked_with_nowait():
    prof, playbook = profile(), uuid4()
    db = db_mock()
    db.execute.side_effect = [Rows([SimpleNamespace(playbook_id=playbook)]), Rows([]), Rows([]), Rows([])]
    await service.lock_credit_policy(db, prof)
    calls = [call.args[0] for call in db.execute.await_args_list]
    assert all(call.get_execution_options()["populate_existing"] for call in calls)
    assert calls[2]._for_update_arg.nowait is True and calls[3]._for_update_arg.nowait is True


@pytest.mark.asyncio
async def test_generic_verify_cannot_bypass_credit_guard_even_with_bucket_file(monkeypatch):
    prof, person = profile(), owner()
    receipt = collected(person, pull(person))
    row = state(prof, provenance={"credit_collection": receipt})
    file_id = uuid4()
    ready = SimpleNamespace(requirements=[SimpleNamespace(requirement_key="guarantor_credit_review", canonical_key="guarantor_credit_review")])
    monkeypatch.setattr(application_programs, "get_program_readiness", AsyncMock(return_value=ready))
    db = db_mock()
    db.execute.side_effect = [SimpleNamespace(scalar_one_or_none=lambda: row), Rows([SimpleNamespace(file_id=file_id)])]
    body = ApplicationRequirementPatch(action="verify", evidence_file_id=file_id, confirmed=True)
    with pytest.raises(HTTPException) as error:
        await application_programs.patch_requirement(db, prof, "guarantor_credit_review", body, SimpleNamespace(id=uuid4()))
    assert error.value.status_code == 409 and "linked-owner credit review" in error.value.detail
    assert row.status != "verified"
