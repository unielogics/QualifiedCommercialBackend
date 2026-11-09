from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.application_profile import ApplicationOwner
from app.routers import application_profiles as routes
from app.schemas.application_profile import (
    FileCreditInviteRead,
    FileCreditInviteRequest,
    PublicFileOwnerConsentSubmit,
)
from app.services import application_profiles as profiles
from app.services import credit_pull_core, owner_credit_review
from app.services import credit_requirement_sync as sync
from app.tests.test_credit_requirement_sync import NOW, Rows, profile, pull, requirement


def person(**changes):
    return ApplicationOwner(**{
        "id": uuid4(), "profile_id": uuid4(), "first_name": "Pat", "last_name": "Owner",
        "email": "pat@example.test", "phone": "+12025550123", "ownership_pct": 100,
        "credit_pull_id": uuid4(), "credit_pulled_at": NOW - timedelta(days=6),
        "credit_score": 720, "credit_summary": {"score_verified": True, "report_available": True},
        "backfill_needs_review": True, **changes,
    })


def db_with_reports(reports):
    return SimpleNamespace(scalars=AsyncMock(return_value=Rows(reports)), commit=AsyncMock(), execute=AsyncMock())


def stub_verification_dependencies(monkeypatch, owners):
    monkeypatch.setattr(profiles, "owner_rows", AsyncMock(return_value=owners))
    monkeypatch.setattr(profiles, "bank_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(profiles, "manual_statement_evidence", AsyncMock(return_value=SimpleNamespace(months=set(), file_count=0, pending_analysis_count=0)))
    monkeypatch.setattr(profiles, "plaid_statement_months", AsyncMock(return_value=set()))
    monkeypatch.setattr(profiles, "evidence_state", AsyncMock(return_value=SimpleNamespace(review_file_count=0, blockers=[])))


@pytest.mark.asyncio
async def test_valid_linked_fico4_collects_in_both_steps_without_passing_equifax_policy(monkeypatch):
    owner = person()
    report = pull(owner, fico=661, bureau_response={"parsed_report": {"scores": [{"model": "FICO 4", "score": 661}]}})
    db = db_with_reports([report])
    prof = profile(bank_verification_override_at=None)
    stub_verification_dependencies(monkeypatch, [owner])
    read = (await profiles.owner_reads_current(db, prof))[0]
    verification = await profiles.verification_state(db, prof)
    receipt = sync.collection_snapshot(prof, [owner], {str(report.id): report}, requirement())
    assert read.credit_complete is True and read.credit_collection_status == "collected"
    assert read.credit_score == 661 and read.backfill_needs_review is True
    assert verification.owner_credit_complete is True and verification.completed_credit_owner_count == 1
    assert receipt["complete"] is True and receipt["can_mark_met"] is False
    assert receipt["collected_owner_count"] == verification.completed_credit_owner_count
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes,status", [
    ({"expires_at": NOW - timedelta(days=1)}, "stale"),
    ({"status": "revoked"}, "needs_review"),
    ({"status": "pending"}, "processing"),
    ({"fcra_consent": False}, "needs_review"),
    ({"legal_last_name": "Someone Else"}, "needs_review"),
    ({"bureau_response": {}}, "processing"),
    ({"pulled_at": None}, "needs_review"),
])
async def test_cached_completion_cannot_hide_current_source_problem(monkeypatch, changes, status):
    owner = person()
    report = pull(owner, **changes)
    assert owner.credit_complete is True  # The legacy property is not collection authority.
    db, prof = db_with_reports([report]), profile(bank_verification_override_at=None)
    stub_verification_dependencies(monkeypatch, [owner])
    read = (await profiles.owner_reads_current(db, prof))[0]
    verification = await profiles.verification_state(db, prof)
    receipt = sync.collection_snapshot(prof, [owner], {str(report.id): report}, requirement())
    assert read.credit_complete is False and read.credit_collection_status == status
    assert verification.owner_credit_complete is False and verification.completed_credit_owner_count == 0
    assert verification.pending_credit_owner_ids == [owner.id]
    assert receipt["complete"] is False and receipt["owners"][0]["status"] == status


@pytest.mark.asyncio
@pytest.mark.parametrize("linked", [False, True])
async def test_timestamp_without_current_report_never_counts_as_collected(monkeypatch, linked):
    owner = person(**({} if linked else {"credit_pull_id": None}))
    db, prof = db_with_reports([]), profile(bank_verification_override_at=None)
    stub_verification_dependencies(monkeypatch, [owner])
    read = (await profiles.owner_reads_current(db, prof))[0]
    assert read.credit_complete is False
    assert read.credit_collection_status == ("needs_review" if linked else "missing")
    assert (await profiles.verification_state(db, prof)).completed_credit_owner_count == 0


@pytest.mark.asyncio
async def test_actual_linked_source_can_complete_without_a_copied_owner_timestamp(monkeypatch):
    owner = person(credit_pulled_at=None)
    report, prof = pull(owner), profile()
    db = db_with_reports([report])
    monkeypatch.setattr(profiles, "owner_rows", AsyncMock(return_value=[owner]))
    read = await profiles.owner_read_current(db, prof, owner)
    assert owner.credit_complete is False and read.credit_complete is True
    assert read.credit_pulled_at == report.pulled_at


@pytest.mark.asyncio
async def test_duplicate_required_owner_report_is_not_collected_in_either_step():
    first, second = person(ownership_pct=50), person(ownership_pct=50)
    second.credit_pull_id = first.credit_pull_id
    report = pull(first)
    reads = await profiles.owner_reads_current(db_with_reports([report]), profile(), owners=[first, second])
    receipt = sync.collection_snapshot(profile(), [first, second], {str(report.id): report}, requirement())
    assert all(not row.credit_complete and row.credit_collection_status == "needs_review" for row in reads)
    assert receipt["complete"] is False and receipt["collected_owner_count"] == 0


@pytest.mark.asyncio
async def test_source_updates_are_bulk_refreshed_and_cached_score_does_not_win():
    first, second = person(ownership_pct=50), person(ownership_pct=50)
    reports = [pull(first, fico=661), pull(second, fico=700)]
    db = db_with_reports(reports)
    reads = await profiles.owner_reads_current(db, profile(), owners=[first, second])
    assert [row.credit_score for row in reads] == [661, 700]
    assert db.scalars.await_count == 1
    assert db.scalars.call_args.args[0].get_execution_options()["populate_existing"] is True
    reports[0].fcra_consent = False
    reads = await profiles.owner_reads_current(db, profile(), owners=[first, second])
    assert reads[0].credit_complete is False and reads[1].credit_complete is True


@pytest.mark.asyncio
async def test_program_specific_expiry_stays_on_authoritative_selected_program_receipt():
    owner = person()
    report = pull(owner, pulled_at=NOW - timedelta(days=50), expires_at=NOW + timedelta(days=40))
    read = (await profiles.owner_reads_current(db_with_reports([report]), profile(), owners=[owner]))[0]
    selected = sync.collection_snapshot(profile(), [owner], {str(report.id): report}, requirement(expiration_days=30))
    assert read.credit_complete is True  # Generic fallback receipt uses base/provider expiry.
    assert selected["complete"] is False and selected["owners"][0]["status"] == "stale"
    # Covered-owner Step 1 rendering uses this SAME selected receipt as Step 2.


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["collected", "processing", "stale", "needs_review"])
async def test_single_invite_never_reissues_for_an_already_linked_report(monkeypatch, status):
    owner, prof = person(credit_pulled_at=None), profile()
    monkeypatch.setattr(owner_credit_review, "lock_review_sources", AsyncMock(return_value=[owner]))
    deliver = AsyncMock()
    monkeypatch.setattr(routes.consent_delivery, "deliver_link_checked", deliver)
    with pytest.raises(HTTPException) as error:
        await routes._mint_credit_invite(db_with_reports([]), prof, owner, SimpleNamespace(), "email", None)
    assert error.value.status_code == 409
    deliver.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("restricted", [False, True])
async def test_manual_batch_invites_only_canonically_missing_unlinked_owners(monkeypatch, restricted):
    missing, linked = person(ownership_pct=50, credit_pull_id=None), person(ownership_pct=50, credit_pulled_at=None)
    prof, db = profile(), db_with_reports([pull(linked, status="pending")])
    monkeypatch.setattr(profiles, "load_profile", AsyncMock(return_value=prof))
    monkeypatch.setattr(profiles, "owner_rows", AsyncMock(return_value=[missing, linked]))
    monkeypatch.setattr(profiles, "verification_state", AsyncMock(return_value=SimpleNamespace(ownership_complete=True, owner_contact_complete=True)))
    mint = AsyncMock(return_value=FileCreditInviteRead(owner_id=missing.id, owner_name=missing.full_name, token="synthetic", path="/synthetic", delivered=False, channel="none", detail="Test only"))
    monkeypatch.setattr(routes, "_mint_credit_invite", mint)
    body = FileCreditInviteRequest(channel="none", owner_ids=[linked.id] if restricted else None)
    answer = await routes.create_pending_owner_credit_invites(prof.id, body, None, SimpleNamespace(), db)
    assert len(answer.items) == (0 if restricted else 1)
    if not restricted:
        assert mint.call_args.args[2].id == missing.id


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True])
async def test_old_consent_token_never_runs_another_pull_for_linked_source(monkeypatch, stale):
    owner = person()
    prof = profile()
    report = pull(owner, **({"expires_at": NOW - timedelta(days=1)} if stale else {}))
    db = db_with_reports([report])
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: owner.id)
    db.get = AsyncMock(side_effect=[owner, prof])
    run = AsyncMock()
    monkeypatch.setattr(credit_pull_core, "run_soft_pull", run)
    body = PublicFileOwnerConsentSubmit(fcra_consent=True, first_name="Pat", last_name="Owner", email="pat@example.com", phone="2025550123")
    if stale:
        with pytest.raises(HTTPException) as error:
            await routes.submit_public_application_credit_consent("app.synthetic", body, db)
        assert error.value.status_code == 409
    else:
        result = await routes.submit_public_application_credit_consent("app.synthetic", body, db)
        assert result.completed is True and result.credit_score_band == "650-699"
    run.assert_not_awaited()
