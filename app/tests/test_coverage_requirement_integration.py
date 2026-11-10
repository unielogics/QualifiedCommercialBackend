from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.application_profile import ApplicationRequirementState
from app.services import application_programs, released_cash_flow
from app.tests.test_released_cash_flow import cockpit
from app.tests.test_requirement_library import db as db
from app.tests.test_requirement_library import template_database as template_database
from app.tests.test_requirement_waivers import KEY, seed


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["zero", "removed", "inactive"])
async def test_saved_positive_relief_derives_one_operating_requirement_and_obsolete_relief_retires_it(db, monkeypatch, change):
    case = await seed(db, monkeypatch)
    current = cockpit()
    case.profile.financial_plan = current["plan"]["record"]
    monkeypatch.setattr("app.services.financial_cockpit.load_cockpit", AsyncMock(side_effect=lambda *_args: current))
    first = await application_programs.get_program_readiness(db, case.profile)
    second = await application_programs.get_program_readiness(db, case.profile)
    first_row = next(row for row in first.requirements if row.requirement_key == released_cash_flow.REQUIREMENT_KEY)
    second_row = next(row for row in second.requirements if row.requirement_key == released_cash_flow.REQUIREMENT_KEY)
    assert first_row.requirement_key == second_row.requirement_key
    assert first_row.collection_completion["fingerprint"] == second_row.collection_completion["fingerprint"]
    stored = (await db.scalars(select(ApplicationRequirementState).where(
        ApplicationRequirementState.profile_id == case.profile.id,
        ApplicationRequirementState.requirement_key == released_cash_flow.REQUIREMENT_KEY))).all()
    assert len(stored) == 1
    original_id = stored[0].id
    assert "coverage_plan" in second_row.source_policy_keys and not second_row.collection_completion["complete"]
    assert not second_row.collection_completion["can_complete"]
    assert any(released_cash_flow.REQUIREMENT_KEY in row.blocking_requirement_keys for row in second.programs)
    if change == "zero":
        current["plan"]["evaluation"]["debt_service_after_regular_monthly"] = 4000
    elif change == "removed":
        case.profile.financial_plan = {}
        current["plan"]["record"] = {}
    else:
        current["plan"]["evaluation"]["active"] = False
    latest = await application_programs.get_program_readiness(db, case.profile)
    assert all(row.requirement_key != released_cash_flow.REQUIREMENT_KEY for row in latest.requirements)
    retained = (await db.scalars(select(ApplicationRequirementState).where(
        ApplicationRequirementState.profile_id == case.profile.id,
        ApplicationRequirementState.requirement_key == released_cash_flow.REQUIREMENT_KEY))).one()
    assert retained.provenance["inactive_definition"] and retained.id == original_id


@pytest.mark.asyncio
async def test_choice_without_criteria_cannot_be_fully_loaded_or_auto_start_underwriting(db, monkeypatch):
    case = await seed(db, monkeypatch)
    unbound = case.selections[0]
    unbound.playbook_id = unbound.playbook_version = None
    case.selections[1].removed_at = datetime.now(UTC)
    state = ApplicationRequirementState(profile_id=case.profile.id, requirement_key=KEY, label="Collected statement",
        category="financials", required_level="required", status="verified", source_program_keys=[unbound.program_key],
        source_policy_keys=[], verification_required=True, provenance={})
    db.add(state)
    await db.flush()
    monkeypatch.setattr(application_programs, "_materialize_requirements", AsyncMock(return_value=([state], {unbound.id: [KEY]})))
    result = await application_programs.get_program_readiness(db, case.profile)
    program = result.programs[0]
    assert program.collection_complete and not program.complete
    assert program.qualification_blocking_requirement_keys == ["published_program_criteria"]
    assert program.qualification_warnings and result.selections[0].playbook_id is None
    application_programs._auto_start_underwriting_if_loaded.assert_awaited_once_with(db, case.profile, [])
