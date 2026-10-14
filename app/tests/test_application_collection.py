from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.application_profile import ApplicationCollectionTask, ApplicationRequirementState
from app.schemas.funding_program import FundingProgramRequirementWrite
from app.services.application_programs import _requested_document
from app.services.business_statements import _slot_matches
from app.services.collection_tasks import (
    canonical_identity,
    collects_file,
    disclosure_hash,
    materialize_response_state,
    merged_task_config,
    sync_collection_tasks,
    task_read,
    validate_answer,
)


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


def state(key="revenue", **metadata):
    return ApplicationRequirementState(
        id=uuid4(),
        profile_id=uuid4(),
        requirement_key=key,
        label="Annual revenue",
        category="financials",
        required_level="required",
        status="missing",
        verification_required=False,
        source_program_keys=[],
        source_policy_keys=[],
        provenance={
            "requirement_kind": "field",
            "canonical_key": "annual_revenue",
            "catalog_approved": True,
            "client_visible": True,
            "definition_version": "1",
            "definition_id": "published-v1",
            "collection_config": {"field_key": "annual_revenue", "value_type": "number", "min": 0},
            **metadata,
        },
    )


def response(row, *, value=None, review_state="accepted", version="1", provenance=None):
    return SimpleNamespace(
        id=uuid4(),
        requirement_state_id=row.id,
        value={"answer": value or 120000},
        source="borrower_room",
        source_version=version,
        review_state=review_state,
        reviewed_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        reviewed_by_user_id=None,
        provenance=provenance or {"definition_id": "published-v1"},
    )


def test_existing_catalog_requirements_default_to_documents():
    requirement = FundingProgramRequirementWrite(requirement_key="tax_return", label="Tax return")
    assert requirement.requirement_kind == "document"
    assert requirement.collection_config == {}


def test_identity_deduplicates_only_same_subject_period_and_disclosure():
    assert canonical_identity(
        "bank_statements", "document", {"subject": "company", "period": "last_6_months"}
    ) == canonical_identity(
        "bank_statements",
        "document",
        {"subject": "company", "period": "last_6_months", "label": "Another label"},
    )
    assert canonical_identity(
        "bank_statements", "document", {"subject": "owner"}
    ) != canonical_identity("bank_statements", "document", {"subject": "company"})
    assert canonical_identity(
        "sharing", "consent", {"disclosure_version": "1"}
    ) != canonical_identity("sharing", "consent", {"disclosure_version": "2"})


def test_shared_collection_uses_strictest_period_and_freshness():
    first = state("three_months", collection_config={"required_months": 3}, expiration_days=90)
    second = state(
        "six_months",
        collection_config={"required_months": 6, "minimum_count": 2},
        expiration_days=30,
    )
    second.label = "Six months business bank statements"
    representative, config = merged_task_config([first, second])
    assert representative is second
    assert config["required_months"] == 6
    assert config["minimum_count"] == 2
    assert config["expiration_days"] == 30


@pytest.mark.parametrize("value", [True, "nan", "inf", -1, "not a number"])
def test_numeric_answers_reject_invalid_or_out_of_range_values(value):
    with pytest.raises(HTTPException):
        validate_answer("field", {"value_type": "number", "min": 0}, value)


def test_fields_validate_false_zero_and_enumeration():
    assert validate_answer("field", {"value_type": "boolean"}, False) is False
    assert validate_answer("field", {"value_type": "integer", "min": 0}, "0") == 0
    with pytest.raises(HTTPException):
        validate_answer("field", {"value_type": "enum", "choices": ["LLC"]}, "Corporation")


def test_consent_needs_configured_disclosure_explicit_agreement_and_name():
    config = {
        "disclosure_text": "Share with Bank A",
        "disclosure_version": "1",
        "scope": ["bank-a"],
    }
    assert validate_answer(
        "consent", config, {"granted": True, "consenter_name": " Pat Owner "}
    ) == {"granted": True, "consenter_name": "Pat Owner"}
    for value in ({"granted": False, "consenter_name": "Pat"}, {"granted": True}, "yes"):
        with pytest.raises(HTTPException):
            validate_answer("consent", config, value)
    with pytest.raises(HTTPException):
        validate_answer("consent", {}, {"granted": True, "consenter_name": "Pat"})
    assert disclosure_hash(config) != disclosure_hash({**config, "scope": ["bank-b"]})


def test_form_without_structured_schema_requires_file_and_cannot_self_attest():
    form = SimpleNamespace(
        requirement_kind="form", collection_config={"form_url": "https://forms.example/application"}
    )
    assert collects_file(form)
    with pytest.raises(HTTPException):
        validate_answer("form", form.collection_config, "done")
    form.collection_config = {"fields": [{"key": "years", "value_type": "integer", "min": 0}]}
    assert not collects_file(form)
    assert validate_answer("form", form.collection_config, {"years": "2"}) == {"years": 2}


@pytest.mark.asyncio
async def test_form_request_is_created_in_existing_form_pipeline_slot():
    requirement = SimpleNamespace(
        requirement_key="lender_custom_pfs",
        requirement_kind="form",
        canonical_key="personal_financial_statement",
        label="Lender personal financial statement",
        category="financials",
        playbook_id=uuid4(),
        visibility=["borrower", "underwriter"],
        collection_config={"form_kind": "pfs"},
        objective_text="Complete the requested statement",
        ai_request_message_template=None,
        required_level="required",
    )
    empty = SimpleNamespace(scalar_one_or_none=lambda: None)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[empty, empty]), flush=AsyncMock(), add=Mock()
    )
    with patch("app.routers.application_profiles._requested_slot", AsyncMock(return_value=None)):
        slot = await _requested_document(
            db, SimpleNamespace(primary_bucket_id=uuid4()), requirement, [], []
        )
    assert slot.category == "Personal Financials"
    assert slot.requirement_key == "lender_custom_pfs"
    assert slot.requirement_source["form_kind"] == "pfs"


def test_generated_statement_matches_explicit_form_slot_without_filename():
    slot = SimpleNamespace(
        name="Lender custom financial form",
        category="Financials",
        requirement_source={"form_kind": "balance_sheet"},
    )
    assert _slot_matches(slot, "balance_sheet") == (True, True)


@pytest.mark.asyncio
async def test_response_stops_borrower_collection_without_automatic_staff_verification():
    row = state()
    await materialize_response_state(
        AsyncMock(), row, {row.id: response(row, review_state="submitted")}
    )
    assert row.status == "received_unverified"
    assert row.provenance["coverage"]["complete"] is True
    assert row.provenance["verified_coverage"]["complete"] is False


@pytest.mark.asyncio
async def test_published_version_change_requires_reconfirmation():
    row = state(definition_version="2")
    await materialize_response_state(AsyncMock(), row, {row.id: response(row)})
    assert row.status == "stale"
    assert row.provenance["coverage"]["complete"] is False


@pytest.mark.asyncio
async def test_requested_clarification_becomes_actionable_again():
    row = state()
    await materialize_response_state(
        AsyncMock(), row, {row.id: response(row, review_state="needs_more")}
    )
    assert row.status == "requested"
    assert row.provenance["coverage"]["complete"] is False


@pytest.mark.asyncio
async def test_staff_internal_review_survives_materialization():
    row = state(requirement_kind="human_review")
    row.status, row.verified_by_user_id = "verified", uuid4()
    await materialize_response_state(AsyncMock(), row, {})
    assert row.status == "verified"


@pytest.mark.asyncio
async def test_shared_requirements_create_one_borrower_task():
    first, second = state("revenue_lender_one"), state("revenue_lender_two")
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[Rows([]), Rows([])]), flush=AsyncMock(), add=Mock()
    )
    tasks = await sync_collection_tasks(db, SimpleNamespace(id=first.profile_id), [first, second])
    assert len(tasks) == 1
    assert tasks[0].requirement_keys == ["revenue_lender_one", "revenue_lender_two"]
    assert tasks[0].assignee == "borrower"
    assert tasks[0].automation_approved is True


@pytest.mark.asyncio
async def test_unapproved_ai_proposal_stays_internal():
    row = state(catalog_approved=False)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[Rows([]), Rows([])]), flush=AsyncMock(), add=Mock()
    )
    task = (await sync_collection_tasks(db, SimpleNamespace(id=row.profile_id), [row]))[0]
    assert task.assignee == "internal"
    assert task.automation_approved is False


@pytest.mark.asyncio
async def test_paused_task_keeps_staff_request_override():
    row = state()
    task = ApplicationCollectionTask(
        id=uuid4(),
        profile_id=row.profile_id,
        canonical_identity=canonical_identity(
            "annual_revenue", "field", row.provenance["collection_config"]
        ),
        requirement_state_ids=[],
        requirement_keys=[],
        action_type="answer_fields",
        label="Annual revenue",
        status="paused",
        assignee="borrower",
        attempts=1,
        max_attempts=3,
        cadence_hours=24,
        config={"request_message_override": "Please confirm your revenue"},
    )
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[Rows([task]), Rows([])]), flush=AsyncMock(), add=Mock()
    )
    result = (await sync_collection_tasks(db, SimpleNamespace(id=row.profile_id), [row]))[0]
    assert result.status == "paused"
    assert result.description == "Please confirm your revenue"
    assert row.provenance["collection_status"] == "paused"


@pytest.mark.asyncio
async def test_exhausted_task_escalates_without_another_borrower_message():
    row = state()
    task = ApplicationCollectionTask(
        id=uuid4(),
        profile_id=row.profile_id,
        canonical_identity=canonical_identity(
            "annual_revenue", "field", row.provenance["collection_config"]
        ),
        requirement_state_ids=[],
        requirement_keys=[],
        action_type="answer_fields",
        label="Annual revenue",
        status="requested",
        assignee="borrower",
        attempts=3,
        max_attempts=3,
        cadence_hours=24,
        config={},
    )
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[Rows([task]), Rows([])]), flush=AsyncMock(), add=Mock()
    )
    result = (await sync_collection_tasks(db, SimpleNamespace(id=row.profile_id), [row]))[0]
    assert result.status == "escalated"
    assert result.assignee == "internal"


def test_public_task_projection_hides_internal_configuration():
    task = ApplicationCollectionTask(
        id=uuid4(),
        label="Revenue",
        description="Please confirm",
        action_type="answer_fields",
        status="open",
        assignee="borrower",
        config={
            "value_type": "number",
            "source_program_keys": ["secret"],
            "underwriter_note": "private",
        },
        state_reason=None,
    )
    public = task_read(task, public=True)
    assert public["config"] == {"value_type": "number"}
    assert "requirement_keys" not in public
