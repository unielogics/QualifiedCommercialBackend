from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.models.application_profile import ApplicationProfile
from app.routers.application_profiles import (
    _apply_reviewed_profile_fact,
    search_application_taxonomy,
)
from app.services.application_profiles import (
    _apply_extracted_taxonomy,
    capture_extracted_profile_facts,
)


def taxonomy_path():
    industry = SimpleNamespace(id=uuid4(), parent_id=None, code="44-45", label="Retail Trade", level=2, status="official")
    group = SimpleNamespace(id=uuid4(), parent_id=industry.id, code="441", label="Motor Vehicle and Parts Dealers", level=3, status="official")
    activity = SimpleNamespace(id=uuid4(), parent_id=group.id, code="441120", label="Used Car Dealers", level=6, status="official")
    return activity, group, industry


def test_extraction_fills_one_complete_canonical_classification():
    profile = SimpleNamespace(naics_label="Untrusted label from a document")
    activity, group, industry = taxonomy_path()
    assert _apply_extracted_taxonomy(profile, activity, group, industry)
    assert profile.naics_code == "441120"
    assert profile.naics_label == "Used Car Dealers"
    assert profile.industry_entry_id == industry.id
    assert profile.subindustry_entry_id == group.id
    assert profile.activity_entry_id == activity.id


@pytest.mark.parametrize("saved", [
    {"naics_code": "722511"}, {"industry_entry_id": uuid4()},
    {"subindustry_entry_id": uuid4()}, {"activity_entry_id": uuid4()},
    {"industry": "Manufacturing"}, {"classified_by_user_id": uuid4()},
])
def test_extraction_never_mixes_or_overwrites_saved_classifications(saved):
    profile = SimpleNamespace(**saved)
    before = vars(profile).copy()
    assert not _apply_extracted_taxonomy(profile, *taxonomy_path())
    assert vars(profile) == before


def test_incomplete_or_invalid_parent_paths_cannot_be_adopted():
    activity, group, industry = taxonomy_path()
    profile = SimpleNamespace()
    assert not _apply_extracted_taxonomy(profile, activity, None, industry)
    group.parent_id = uuid4()
    assert not _apply_extracted_taxonomy(profile, activity, group, industry)
    assert vars(profile) == {}


@pytest.mark.asyncio
async def test_unrecognized_extracted_code_stays_suggested_not_authoritative():
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4())
    file = SimpleNamespace(id=uuid4(), bucket_id=profile.primary_bucket_id, statement_period=None)
    analysis = SimpleNamespace(id=uuid4(), classification="tax_return", analysis={"profile_facts": {"naics_code": {"value": "123456"}, "naics_label": {"value": "Made up industry"}}})
    empty = SimpleNamespace(scalar_one_or_none=lambda: None, scalars=lambda: SimpleNamespace(all=lambda: []))
    added = []
    db = SimpleNamespace(execute=AsyncMock(return_value=empty), add=added.append, flush=AsyncMock())
    with patch("app.services.application_profiles.affected_profiles_for_file", AsyncMock(return_value=[profile])):
        await capture_extracted_profile_facts(db, file=file, analysis=analysis)
    assert [fact.field_key for fact in added] == ["naics_code", "naics_label"]
    assert not getattr(profile, "naics_code", None)
    assert not getattr(profile, "activity_entry_id", None)


@pytest.mark.asyncio
async def test_multiword_taxonomy_search_matches_words_and_returns_full_paths():
    statements = []
    async def execute(stmt):
        statements.append(stmt)
        return SimpleNamespace(scalar_one=lambda: 0, scalars=lambda: SimpleNamespace(all=lambda: []))
    db = SimpleNamespace(execute=execute)
    result = await search_application_taxonomy(SimpleNamespace(), db, q="auto dealer", level=6)
    compiled = statements[-1].compile(dialect=postgresql.dialect())
    assert "%auto%" in compiled.params.values()
    assert "%dealer%" in compiled.params.values()
    assert "%auto dealer%" not in compiled.params.values()
    assert result.total == 0


@pytest.mark.asyncio
async def test_taxonomy_search_treats_wildcards_as_literal_input():
    statements = []
    async def execute(stmt):
        statements.append(stmt)
        return SimpleNamespace(scalar_one=lambda: 0, scalars=lambda: SimpleNamespace(all=lambda: []))
    await search_application_taxonomy(SimpleNamespace(), SimpleNamespace(execute=execute), q="100% _test", level=6)
    values = statements[-1].compile(dialect=postgresql.dialect()).params.values()
    assert "%100\\%%" in values
    assert "%\\_test%" in values


@pytest.mark.asyncio
async def test_accepting_activity_replaces_the_whole_classification():
    activity, group, sector = taxonomy_path()
    profile = ApplicationProfile(naics_code="722511", activity_entry_id=uuid4(), industry="Restaurants", classification_revision=2)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: activity)), get=AsyncMock(side_effect=[group, sector]))
    user = SimpleNamespace(id=uuid4())
    await _apply_reviewed_profile_fact(db, profile, user, "naics_code", "441120")
    assert profile.naics_code == "441120"
    assert profile.naics_label == "Used Car Dealers"
    assert profile.activity_entry_id == activity.id
    assert profile.industry_entry_id == sector.id
    assert profile.subindustry_entry_id == group.id
    assert profile.classification_revision == 3
    assert profile.classified_by_user_id == user.id


@pytest.mark.asyncio
async def test_unknown_code_acceptance_is_rejected_before_any_profile_change():
    profile = ApplicationProfile(naics_code="441120")
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)))
    with pytest.raises(HTTPException) as error:
        await _apply_reviewed_profile_fact(db, profile, SimpleNamespace(id=uuid4()), "naics_code", "123456")
    assert error.value.status_code == 422
    assert profile.naics_code == "441120"


@pytest.mark.asyncio
async def test_conflicting_label_cannot_corrupt_selected_activity():
    activity, _, _ = taxonomy_path()
    profile = ApplicationProfile(activity_entry_id=activity.id, naics_label=activity.label)
    db = SimpleNamespace(get=AsyncMock(return_value=activity))
    with pytest.raises(HTTPException):
        await _apply_reviewed_profile_fact(db, profile, SimpleNamespace(id=uuid4()), "naics_label", "Full-Service Restaurants")
    assert profile.naics_label == activity.label
