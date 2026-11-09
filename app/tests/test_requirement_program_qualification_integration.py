from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.ai_playbook import AICollectionRequirement
from app.models.application_profile import ApplicationRequirementState
from app.services import application_programs
from app.tests.test_requirement_library import db as db
from app.tests.test_requirement_library import template_database as template_database
from app.tests.test_requirement_waivers import KEY, seed

CHECK = {"key": "custom_positive_earnings", "label": "Positive earnings", "instructions": "Verify positive earnings", "severity": "block"}


async def shared_collected_state(db, case, monkeypatch, *, optional_second=False):
    definitions = list((await db.scalars(select(AICollectionRequirement).where(
        AICollectionRequirement.playbook_id.in_([row.playbook_id for row in case.selections])))).all())
    strict = next(row for row in definitions if row.playbook_id == case.selections[0].playbook_id)
    other = next(row for row in definitions if row.playbook_id == case.selections[1].playbook_id)
    strict.review_checks = [CHECK]
    if optional_second:
        other.required_level = "optional"
    state = ApplicationRequirementState(profile_id=case.profile.id, requirement_key=KEY,
        label="Shared financial statements", category="financials", required_level="required", status="verified",
        source_program_keys=[row.program_key for row in case.selections], source_policy_keys=[],
        verification_required=True, provenance={"review_checks": [CHECK], "qualification_review": {
            "status": "pending", "blocking": True, "reasons": ["Program A earnings review is pending"], "checks": [CHECK]}})
    db.add(state)
    await db.flush()
    monkeypatch.setattr(application_programs, "_materialize_requirements", AsyncMock(return_value=(
        [state], {row.id: [KEY] for row in case.selections})))
    return state


@pytest.mark.asyncio
async def test_shared_collection_keeps_only_the_applicable_program_condition_blocking(db, monkeypatch):
    case = await seed(db, monkeypatch)
    await shared_collected_state(db, case, monkeypatch)
    result = await application_programs.get_program_readiness(db, case.profile)
    programs = {row.program_key: row for row in result.programs}
    assert programs["program_a"].collection_complete and programs["program_b"].collection_complete
    assert not programs["program_a"].complete and programs["program_b"].complete
    assert programs["program_a"].qualification_blocking_requirement_keys == [KEY]
    assert programs["program_b"].qualification_blocking_requirement_keys == []
    application_programs._auto_start_underwriting_if_loaded.assert_awaited_once_with(db, case.profile, ["program_b"])


@pytest.mark.asyncio
async def test_optional_condition_does_not_inherit_another_programs_required_level(db, monkeypatch):
    case = await seed(db, monkeypatch)
    await shared_collected_state(db, case, monkeypatch, optional_second=True)
    result = await application_programs.get_program_readiness(db, case.profile)
    programs = {row.program_key: row for row in result.programs}
    assert programs["program_a"].required_count == 1
    assert programs["program_b"].required_count == 0
