"""Program-scoped exemptions use the database contract and shared projections."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.enums import Role
from app.models.ai_playbook import AICollectionRequirement, AIPlaybookTemplate
from app.models.application_profile import (
    ApplicationCollectionTask,
    ApplicationProfile,
    ApplicationProgramRequirementOverride,
    ApplicationProgramSelection,
    ApplicationRequirementState,
)
from app.models.user import User
from app.routers.application_profiles import update_application_requirement
from app.schemas.application_profile import ApplicationRequirementPatch
from app.services import application_programs
from app.tests.test_requirement_library import db as db
from app.tests.test_requirement_library import template_database as template_database

KEY = "ytd_p_and_l_balance_sheet"
REASON = "Reviewed current tax returns; no interim statement needed for this program"


async def seed(db, monkeypatch, *, policy=False, blocked=False):
    user = User(clerk_id=f"waiver-{uuid4()}", email=f"waiver-{uuid4()}@example.com", name="Staff", role=Role.LOAN_EXEC)
    profile = ApplicationProfile(vertical="main_street", funding_category="general", program_selection_mode="manual",
        underwriting_status="collecting_docs", missing_item_email_requirement_key=KEY,
        missing_item_email_next_send_at=datetime.now(UTC) + timedelta(days=1))
    playbooks = [AIPlaybookTemplate(owner_type="funding", playbook_type="loan_product", product_key=key, name=key,
                  version=1, status="published", is_active=True) for key in ("program_a", "program_b")]
    db.add_all([user, profile, *playbooks])
    await db.flush()
    selections = []
    for index, playbook in enumerate(playbooks):
        db.add(AICollectionRequirement(playbook_id=playbook.id, requirement_key=KEY, label="Year-to-date P&L and balance sheet",
            category="financials", required_level="required", visibility=["borrower", "underwriter"],
            can_underwriter_waive=not (blocked and index == 1)))
        item = ApplicationProgramSelection(profile_id=profile.id, playbook_id=playbook.id, playbook_version=1,
            program_key=playbook.product_key, program_name=playbook.name, source="operator", selected_by_user_id=user.id)
        selections.append(item)
        db.add(item)
    monkeypatch.setattr(application_programs, "profile_fit_context", AsyncMock(return_value={"vertical": "main_street", "intent_kind": "lending"}))
    monkeypatch.setattr(application_programs, "published_candidates", AsyncMock(return_value=[]))
    monkeypatch.setattr(application_programs, "_auto_start_underwriting_if_loaded", AsyncMock(return_value=False))
    if policy:
        from app.models.funding_program import ApplicationEvidencePolicySelection

        baseline = AIPlaybookTemplate(owner_type="platform", playbook_type="evidence_policy", product_key="business_baseline",
                                     name="Independent baseline", version=1, status="published", is_active=True)
        db.add(baseline)
        await db.flush()
        db.add(AICollectionRequirement(playbook_id=baseline.id, requirement_key=KEY, label="Baseline interim financials",
            category="financials", required_level="required", visibility=["borrower", "underwriter"], can_underwriter_waive=False))
        policy_row = ApplicationEvidencePolicySelection(profile_id=profile.id, playbook_id=baseline.id, playbook_version=1,
            policy_key="business_baseline", policy_name=baseline.name)
        db.add(policy_row)
        monkeypatch.setattr(application_programs, "ensure_evidence_policy", AsyncMock(return_value=[policy_row]))
    else:
        monkeypatch.setattr(application_programs, "ensure_evidence_policy", AsyncMock(return_value=[]))
    await db.flush()
    return SimpleNamespace(profile=profile, user=user, selections=selections)


async def overrides(db, profile):
    return list((await db.execute(select(ApplicationProgramRequirementOverride).join(ApplicationProgramSelection).where(
        ApplicationProgramSelection.profile_id == profile.id, ApplicationProgramRequirementOverride.restored_at.is_(None)))).scalars().all())


async def task(db, profile):
    return (await db.execute(select(ApplicationCollectionTask).where(ApplicationCollectionTask.profile_id == profile.id))).scalars().one()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_program_waiver_persists_database_disposition_for_new_and_existing_rows(db, monkeypatch, existing):
    case = await seed(db, monkeypatch)
    if existing:
        db.add(ApplicationProgramRequirementOverride(selection_id=case.selections[0].id, requirement_key=KEY,
            disposition="not_applicable", reason="Earlier scope decision", created_by_user_id=case.user.id))
        await db.flush()
    await application_programs.patch_requirement(db, case.profile, KEY,
        ApplicationRequirementPatch(action="waive", program_keys=["program_a"], reason=REASON, confirmed=True), case.user)
    persisted = await overrides(db, case.profile)
    assert len(persisted) == 1 and persisted[0].disposition == "waived"
    assert persisted[0].reason == REASON and persisted[0].created_by_user_id == case.user.id


@pytest.mark.asyncio
@pytest.mark.parametrize("action,expected_status", [("waive", "waived"), ("not_applicable", "not_applicable")])
async def test_scoped_waiver_keeps_other_program_required_then_all_scopes_complete_and_restore_reopens(db, monkeypatch, action, expected_status):
    case = await seed(db, monkeypatch)
    for program in ("program_a", "program_b"):
        await application_programs.patch_requirement(db, case.profile, KEY,
            ApplicationRequirementPatch(action=action, program_keys=[program], reason=REASON, confirmed=True), case.user)
        current = await application_programs.get_program_readiness(db, case.profile)
        programs = {row.program_key: row for row in current.programs}
        requirement = next(row for row in current.requirements if row.requirement_key == KEY)
        current_task = await task(db, case.profile)
        if program == "program_a":
            assert programs["program_a"].complete and not programs["program_b"].complete
            assert requirement.status not in {"waived", "not_applicable"} and current_task.status in {"open", "requested"}
            assert current_task.next_attempt_at is not None
        else:
            assert all(row.complete for row in programs.values())
            assert requirement.status == expected_status and requirement.state_reason == REASON
            assert current_task.status == "completed" and current_task.next_attempt_at is None
            assert current.automation.next_requirement_key is None and current.automation.next_send_at is None
            assert case.profile.missing_item_email_next_send_at is None
            from app.services.application_profiles import client_evidence_banking_summary

            summary = await client_evidence_banking_summary(db, case.profile, readiness=current,
                verification=SimpleNamespace(business_banking_complete=False),
                banking=SimpleNamespace(items=[], evidence_summary=current.evidence_summary))
            assert summary.completed_required_count == 1 and summary.missing_required_count == 0
    state = (await db.execute(select(ApplicationRequirementState).where(ApplicationRequirementState.profile_id == case.profile.id))).scalars().one()
    assert state.status not in {"waived", "not_applicable"}  # Scopes remain independently recoverable.
    await application_programs.patch_requirement(db, case.profile, KEY, ApplicationRequirementPatch(action="restore", confirmed=True), case.user)
    restored = await application_programs.get_program_readiness(db, case.profile)
    assert not any(row.complete for row in restored.programs)
    assert (await task(db, case.profile)).status in {"open", "requested"}


@pytest.mark.asyncio
async def test_all_program_exemptions_do_not_remove_an_independent_policy_obligation(db, monkeypatch):
    case = await seed(db, monkeypatch, policy=True)
    await application_programs.patch_requirement(db, case.profile, KEY,
        ApplicationRequirementPatch(action="not_applicable", all_programs=True, reason=REASON, confirmed=True), case.user)
    current = await application_programs.get_program_readiness(db, case.profile)
    requirement = next(row for row in current.requirements if row.requirement_key == KEY)
    assert requirement.status not in {"waived", "not_applicable"}
    assert all(not row.complete for row in current.programs)
    assert (await task(db, case.profile)).status in {"open", "requested"}


@pytest.mark.asyncio
async def test_a_new_selected_program_without_an_exemption_reopens_the_same_task(db, monkeypatch):
    case = await seed(db, monkeypatch)
    await application_programs.patch_requirement(db, case.profile, KEY,
        ApplicationRequirementPatch(action="not_applicable", all_programs=True, reason=REASON, confirmed=True), case.user)
    await application_programs.get_program_readiness(db, case.profile)
    prior = await task(db, case.profile)
    assert prior.status == "completed"
    playbook = AIPlaybookTemplate(owner_type="funding", playbook_type="loan_product", product_key="program_c",
        name="New program", version=1, status="published", is_active=True)
    db.add(playbook)
    await db.flush()
    db.add_all([AICollectionRequirement(playbook_id=playbook.id, requirement_key=KEY, label="Interim financials",
        category="financials", required_level="required", visibility=["borrower", "underwriter"], can_underwriter_waive=True),
        ApplicationProgramSelection(profile_id=case.profile.id, playbook_id=playbook.id, playbook_version=1,
            program_key="program_c", program_name=playbook.name, source="operator", selected_by_user_id=case.user.id)])
    await db.flush()
    current = await application_programs.get_program_readiness(db, case.profile)
    reopened = await task(db, case.profile)
    assert reopened.id == prior.id and reopened.status in {"open", "requested"} and reopened.next_attempt_at is not None
    assert next(row for row in current.requirements if row.requirement_key == KEY).status not in {"waived", "not_applicable"}
    assert not next(row for row in current.programs if row.program_key == "program_c").complete


@pytest.mark.asyncio
async def test_existing_sole_program_exemption_completes_requested_task_without_creating_a_new_waiver(db, monkeypatch):
    case = await seed(db, monkeypatch)
    case.selections[1].removed_at = datetime.now(UTC)
    await db.flush()
    await application_programs.get_program_readiness(db, case.profile)
    prior = await task(db, case.profile)
    prior.status, prior.attempts = "requested", 2
    prior.next_attempt_at = datetime.now(UTC) + timedelta(days=1)
    exemption = ApplicationProgramRequirementOverride(selection_id=case.selections[0].id, requirement_key=KEY,
        disposition="not_applicable", reason="Existing reviewed program exemption", created_by_user_id=case.user.id)
    db.add(exemption)
    await db.flush()
    current = await application_programs.get_program_readiness(db, case.profile)
    completed = await task(db, case.profile)
    assert completed.id == prior.id and completed.status == "completed" and completed.next_attempt_at is None
    assert completed.attempts == 2 and len(await overrides(db, case.profile)) == 1
    assert current.requirements[0].status == "not_applicable"
    assert current.requirements[0].state_reason == exemption.reason


def test_program_override_cannot_exempt_an_independent_lender_condition():
    selection = SimpleNamespace(id=uuid4(), program_key="program_a")
    state = SimpleNamespace(requirement_key=KEY, status="requested", source_program_keys=["program_a"], source_policy_keys=[], provenance={"source_kind": "lender"})
    override = SimpleNamespace(disposition="waived", reason=REASON)
    assert application_programs.effective_requirement_dispositions([state], [selection], {(selection.id, KEY): override}) == {}
    assert not application_programs._requirement_is_effectively_accepted(state, override)


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["unknown_program", "nonwaivable"])
async def test_invalid_scope_or_nonwaivable_criteria_return_422_without_partial_overrides(db, monkeypatch, problem):
    case = await seed(db, monkeypatch, blocked=problem == "nonwaivable")
    payload = ApplicationRequirementPatch(action="waive", reason=REASON, confirmed=True,
        all_programs=problem == "nonwaivable", program_keys=["not_selected"] if problem == "unknown_program" else [])
    with pytest.raises(HTTPException) as caught:
        await application_programs.patch_requirement(db, case.profile, KEY, payload, case.user)
    assert caught.value.status_code == 422 and await overrides(db, case.profile) == []


@pytest.mark.asyncio
async def test_route_records_reason_and_scopes_and_repeated_save_updates_the_same_override(db, monkeypatch):
    case = await seed(db, monkeypatch)
    payload = ApplicationRequirementPatch(action="waive", all_programs=True, reason=REASON, confirmed=True)
    with (patch("app.routers.application_profiles.profiles.load_profile", AsyncMock(return_value=case.profile)),
          patch("app.routers.application_profiles.profiles.log_profile_action", AsyncMock()) as audit):
        first = await update_application_requirement(case.profile.id, KEY, payload, case.user, db)
        ids = {row.id for row in await overrides(db, case.profile)}
        second = await update_application_requirement(case.profile.id, KEY, payload, case.user, db)
    assert len(ids) == 2 and {row.id for row in await overrides(db, case.profile)} == ids
    assert first.requirements[0].program_overrides == second.requirements[0].program_overrides == {"program_a": "waived", "program_b": "waived"}
    assert audit.await_args.kwargs["metadata"]["reason"] == REASON
    assert audit.await_args.kwargs["metadata"]["all_programs"]


@pytest.mark.asyncio
async def test_route_requires_underwriting_role_before_profile_lookup():
    payload = ApplicationRequirementPatch(action="waive", all_programs=True, reason=REASON, confirmed=True)
    with patch("app.routers.application_profiles.profiles.load_profile", AsyncMock()) as load:
        with pytest.raises(HTTPException) as caught:
            await update_application_requirement(uuid4(), KEY, payload, SimpleNamespace(role=Role.BROKER), AsyncMock())
    assert caught.value.status_code == 403
    load.assert_not_called()
