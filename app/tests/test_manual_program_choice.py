from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.schemas.application_profile import ApplicationProgramsPatch, ProgramFitCandidate
from app.services import application_programs as programs


def candidate(**changes):
    return ProgramFitCandidate(**{"program_key": "published_product", "program_name": "Published product",
        "catalog_id": uuid4(), "public_slug": "published-product", "eligible": False, "recommendation_status": "criteria_unavailable",
        "criteria_status": "not_published", "reasons": ["No approved published criteria version is available"], **changes})


@pytest.mark.asyncio
@pytest.mark.parametrize("published_criteria", [False, True])
async def test_manual_catalog_choice_does_not_require_fit_or_a_reason_and_does_not_claim_qualification(published_criteria):
    item = candidate(**({"playbook_id": uuid4(), "playbook_version": 3, "criteria_status": "invalid"} if published_criteria else {}))
    profile = SimpleNamespace(id=uuid4())
    added = []
    db = SimpleNamespace(add=added.append, flush=AsyncMock())
    with patch.object(programs, "active_selections", AsyncMock(return_value=[])), \
         patch.object(programs, "published_candidates", AsyncMock(return_value=[item])):
        await programs.set_programs(db, profile, ApplicationProgramsPatch(program_keys=[item.program_key], confirmed=True), SimpleNamespace(id=uuid4()))
    assert len(added) == 1
    selection = added[0]
    assert selection.source == "operator" and selection.needs_scope_review
    assert selection.playbook_id == item.playbook_id and selection.playbook_version == item.playbook_version
    assert selection.fit_reasons[:len(item.reasons)] == item.reasons and profile.program_selection_mode == "manual"
    assert programs._selection_criteria_blockers(selection, item)
    later = candidate(playbook_id=uuid4(), playbook_version=4, eligible=True,
        recommendation_status="recommended", criteria_status="published")
    assert programs._selection_criteria_blockers(selection, later)


@pytest.mark.asyncio
async def test_manual_choice_can_explicitly_adopt_first_real_criteria_without_upgrading_other_pins():
    item = candidate(playbook_id=uuid4(), playbook_version=3, recommendation_status="not_eligible", criteria_status="published")
    selection = SimpleNamespace(program_key=item.program_key, playbook_id=None, playbook_version=None)
    db = SimpleNamespace(add=lambda row: pytest.fail("Existing choice must remain the same row"), flush=AsyncMock())
    with patch.object(programs, "active_selections", AsyncMock(return_value=[selection])), \
         patch.object(programs, "published_candidates", AsyncMock(return_value=[item])):
        await programs.set_programs(db, SimpleNamespace(id=uuid4()), ApplicationProgramsPatch(program_keys=[item.program_key], confirmed=True), SimpleNamespace(id=uuid4()))
    assert selection.playbook_id == item.playbook_id and selection.playbook_version == 3 and selection.needs_scope_review
    assert not programs._selection_criteria_blockers(selection, item)


@pytest.mark.asyncio
async def test_unknown_or_unpublished_catalog_keys_are_rejected_before_changes():
    db = SimpleNamespace(add=lambda row: pytest.fail("No selection may be created"), flush=AsyncMock())
    with patch.object(programs, "active_selections", AsyncMock(return_value=[])), \
         patch.object(programs, "published_candidates", AsyncMock(return_value=[])), pytest.raises(HTTPException) as error:
        await programs.set_programs(db, SimpleNamespace(id=uuid4()), ApplicationProgramsPatch(program_keys=["unpublished"], confirmed=True), SimpleNamespace(id=uuid4()))
    assert error.value.status_code == 422


def test_later_bad_catalog_version_does_not_replace_or_invalidate_a_real_immutable_pin():
    selection = SimpleNamespace(playbook_id=uuid4(), playbook_version=2)
    latest = candidate(playbook_id=uuid4(), playbook_version=3, criteria_status="invalid")
    assert not programs._selection_criteria_blockers(selection, latest)
