from __future__ import annotations

import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.dealer_os import crm_router, prospect_router
from app.dealer_os import router as dealer_router
from app.dealer_os.models import DealerRepContact
from app.dealer_os.prospect_schemas import (
    ProspectDuplicateMatchRead,
    ProspectOutcomeApply,
    ProspectReassignmentRequestCreate,
)
from app.dealer_os.services import prospects
from app.enums import Role
from app.models.dealer_prospect import DealerProspect
from app.models.user import User


def _user(role: Role = Role.FIELD_REP):
    return SimpleNamespace(
        id=uuid4(),
        role=role,
        account_access_types=[],
        account_status="active",
        deleted_at=None,
        dealer_prospect_pipeline_enabled=True,
        name="Pipeline User",
        email="agent@example.com",
    )


def _identity_row(*, email: str, phone: str, archived: bool = False):
    return SimpleNamespace(
        id=uuid4(),
        owner_user_id=uuid4(),
        primary_contact_id=uuid4(),
        email_normalized=email,
        phone_normalized=phone,
        archived_at=datetime.now(UTC) if archived else None,
    )


def test_duplicate_query_is_global_not_dealer_scoped() -> None:
    statement = prospects.find_duplicates.__doc__
    assert statement and "globally by email OR phone" in statement
    indexes = {index.name: index for index in DealerProspect.__table__.indexes}
    assert [column.name for column in indexes["ix_dealer_prospect_email_identity"].columns] == [
        "email_normalized"
    ]
    assert [column.name for column in indexes["ix_dealer_prospect_phone_identity"].columns] == [
        "phone_normalized"
    ]
    contact_indexes = {index.name: index for index in DealerRepContact.__table__.indexes}
    assert "lower(email)" in str(contact_indexes["ix_dos_rep_contacts_email_identity"].expressions[0])
    assert [column.name for column in contact_indexes["ix_dos_rep_contacts_phone_identity"].columns] == [
        "phone_e164"
    ]


def test_split_email_and_phone_identity_is_a_blocking_conflict() -> None:
    email_row = _identity_row(email="rocio@example.com", phone="+19735550001")
    phone_row = _identity_row(email="other@example.com", phone="+19735550148")

    assert (
        prospects.duplicate_state(
            [email_row, phone_row],
            email_normalized="rocio@example.com",
            phone_normalized="+19735550148",
        )
        == "identity_conflict"
    )


@pytest.mark.asyncio
async def test_central_contact_resolver_reuses_one_visible_contact(monkeypatch) -> None:
    row = SimpleNamespace(
        id=uuid4(),
        owner_user_id=uuid4(),
        email="Rocio@Example.com",
        phone_e164="+19735550148",
    )
    monkeypatch.setattr(prospects, "acquire_identity_locks", AsyncMock())
    monkeypatch.setattr(
        prospects,
        "find_contact_identity_matches",
        AsyncMock(return_value=[row]),
    )

    resolved, email, phone = await prospects.resolve_contact_identity(
        AsyncMock(spec=AsyncSession),
        actor_user=_user(Role.SUPER_ADMIN),
        owner_user_id=uuid4(),
        email=" ROCIO@example.com ",
        phone="(973) 555-0148",
    )

    assert resolved is row
    assert email == "rocio@example.com"
    assert phone == "+19735550148"


@pytest.mark.asyncio
async def test_central_contact_resolver_blocks_split_identity(monkeypatch) -> None:
    email_row = SimpleNamespace(
        id=uuid4(),
        owner_user_id=uuid4(),
        email="rocio@example.com",
        phone_e164="+19735550001",
    )
    phone_row = SimpleNamespace(
        id=uuid4(),
        owner_user_id=uuid4(),
        email="other@example.com",
        phone_e164="+19735550148",
    )
    monkeypatch.setattr(prospects, "acquire_identity_locks", AsyncMock())
    monkeypatch.setattr(
        prospects,
        "find_contact_identity_matches",
        AsyncMock(return_value=[email_row, phone_row]),
    )

    with pytest.raises(HTTPException) as error:
        await prospects.resolve_contact_identity(
            AsyncMock(spec=AsyncSession),
            actor_user=_user(Role.SUPER_ADMIN),
            owner_user_id=uuid4(),
            email="rocio@example.com",
            phone="+19735550148",
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "contact_identity_conflict"


def test_every_direct_contact_creation_path_uses_central_resolver() -> None:
    assert "resolve_contact_identity" in inspect.getsource(
        crm_router._find_or_create_company_contact
    )
    assert "resolve_contact_identity" in inspect.getsource(crm_router._presentation_contact)
    assert "resolve_contact_identity" in inspect.getsource(dealer_router._ensure_rep_contact)


def test_archived_identity_match_is_not_treated_as_clear() -> None:
    row = _identity_row(
        email="rocio@example.com", phone="+19735550148", archived=True
    )
    assert (
        prospects.duplicate_state(
            [row],
            email_normalized="rocio@example.com",
            phone_normalized="+19735550148",
        )
        == "archived_match"
    )


def test_archived_duplicate_match_carries_restore_version() -> None:
    row = ProspectDuplicateMatchRead(
        prospect_id=uuid4(),
        contact_id=uuid4(),
        archived=True,
        version=7,
        matched_on=["email"],
    )
    assert row.version == 7


def test_pipeline_search_covers_name_canonical_email_and_mixed_terms() -> None:
    owner = aliased(User, name="search_owner")

    name_only = prospect_router._prospect_search_filters("Rocio", owner)
    email_only = prospect_router._prospect_search_filters("rocio@dealer.com", owner)
    combined = prospect_router._prospect_search_filters(
        "Rocio rocio@dealer.com", owner
    )

    assert len(name_only) == 1
    assert len(email_only) == 1
    assert len(combined) == 2
    rendered = " ".join(str(clause) for clause in combined)
    assert "dos_rep_contacts.full_name" in rendered
    assert "dos_rep_contacts.email" in rendered
    assert "dealer_prospects.email_normalized" in rendered
    assert "dos_rep_companies.name" in rendered


@pytest.mark.asyncio
async def test_contact_delete_archives_linked_prospect_and_clears_follow_up(
    monkeypatch,
) -> None:
    actor = _user()
    contact = SimpleNamespace(
        id=uuid4(),
        owner_user_id=actor.id,
        dealer_id=None,
        archived_at=None,
        archived_by_user_id=None,
    )
    prospect = SimpleNamespace(
        id=uuid4(),
        owner_user_id=actor.id,
        archived_at=None,
        archived_by_user_id=None,
        next_follow_up_at=datetime.now(UTC),
        version=3,
    )
    result = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: [prospect]),
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=result), commit=AsyncMock())
    monkeypatch.setattr(
        crm_router, "_load_contact", AsyncMock(return_value=contact)
    )
    activity = AsyncMock()
    monkeypatch.setattr(crm_router.prospect_service, "add_activity", activity)

    response = await crm_router.archive_contact(contact.id, actor, db)

    assert response["archived"] is True
    assert response["archived_prospect_ids"] == [str(prospect.id)]
    assert contact.archived_at is not None
    assert contact.archived_by_user_id == actor.id
    assert prospect.archived_at == contact.archived_at
    assert prospect.next_follow_up_at is None
    assert prospect.version == 4
    activity.assert_awaited_once()
    await_args = activity.await_args
    assert await_args.args[3] == "contact_archived"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_assigned_contact_viewer_cannot_delete_another_owners_contact(
    monkeypatch,
) -> None:
    actor = _user()
    contact = SimpleNamespace(
        id=uuid4(),
        owner_user_id=uuid4(),
        dealer_id=None,
        archived_at=None,
    )
    monkeypatch.setattr(
        crm_router, "_load_contact", AsyncMock(return_value=contact)
    )
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())

    with pytest.raises(HTTPException) as error:
        await crm_router.archive_contact(contact.id, actor, db)

    assert error.value.status_code == 403
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_preflight_remains_available_when_outreach_package_is_disabled(
    monkeypatch,
) -> None:
    actor = _user()
    actor.dealer_prospect_pipeline_enabled = False
    monkeypatch.setattr(
        prospect_router.service, "find_duplicates", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        prospect_router.service,
        "find_contact_identity_matches",
        AsyncMock(return_value=[]),
    )

    result = await prospect_router.check_prospect_duplicate(
        actor,
        SimpleNamespace(),
        email="rocio@example.com",
        phone=None,
        contact_id=None,
    )

    assert result.blocked is False
    assert result.state == "clear"


@pytest.mark.asyncio
async def test_prospect_list_remains_feature_gated_when_outreach_package_is_disabled() -> None:
    actor = _user()
    actor.dealer_prospect_pipeline_enabled = False

    with pytest.raises(HTTPException) as error:
        await prospect_router.list_prospects(actor, SimpleNamespace())

    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_hidden_reassignment_request_notifies_admin_without_identity_data(
    monkeypatch,
) -> None:
    actor = _user()
    actor.dealer_prospect_pipeline_enabled = False
    hidden = _identity_row(email="rocio@example.com", phone="+19735550148")

    def scalar_result(values):
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: values),
        )

    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(scalar_one_or_none=lambda: None),
                scalar_result([]),
                scalar_result([uuid4()]),
            ]
        )
    )
    monkeypatch.setattr(
        prospect_router.service, "find_duplicates", AsyncMock(return_value=[hidden])
    )
    monkeypatch.setattr(
        prospect_router.service,
        "find_contact_identity_matches",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        prospect_router.service, "visible_contact_ids", AsyncMock(return_value=set())
    )
    notify = AsyncMock(return_value=[])
    monkeypatch.setattr(prospect_router, "notify_users", notify)

    result = await prospect_router.request_prospect_reassignment(
        ProspectReassignmentRequestCreate(
            email="rocio@example.com",
            phone="+1 (973) 555-0148",
            idempotency_key="request-123",
        ),
        actor,
        db,
    )

    assert result.status == "accepted"
    call = notify.await_args.kwargs
    assert call["recipient_ids"]
    assert call["meta"]["matched_contact_ids"] == [str(hidden.primary_contact_id)]
    assert "email" not in call["meta"]
    assert "phone" not in call["meta"]


def test_business_follow_up_skips_weekend_and_uses_firm_timezone() -> None:
    # Friday afternoon UTC is Friday morning in New York; one business day is
    # Monday at the fixed 10 AM work block (14:00 UTC during DST).
    current = datetime(2026, 9, 18, 15, 0, tzinfo=UTC)
    assert prospects.business_follow_up_at(
        business_days=1,
        timezone_name="America/New_York",
        current_time=current,
    ) == datetime(2026, 9, 21, 14, 0, tzinfo=UTC)
    assert prospects.business_follow_up_at(
        business_days=2,
        timezone_name="America/New_York",
        current_time=current,
    ) == datetime(2026, 9, 22, 14, 0, tzinfo=UTC)


def test_custom_follow_up_must_be_weekday_business_hours() -> None:
    current = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    with pytest.raises(HTTPException) as error:
        prospects.normalize_custom_follow_up(
            datetime(2026, 9, 21, 9, 30),
            timezone_name="America/New_York",
            current_time=current,
        )
    assert error.value.detail["code"] == "follow_up_outside_business_hours"

    assert prospects.normalize_custom_follow_up(
        datetime(2026, 9, 21, 10, 30),
        timezone_name="America/New_York",
        current_time=current,
    ) == datetime(2026, 9, 21, 14, 30, tzinfo=UTC)


def test_custom_follow_up_choice_requires_a_timestamp() -> None:
    with pytest.raises(ValueError, match="next_follow_up_at is required"):
        ProspectOutcomeApply(
            outcome_key="call_back",
            expected_version=1,
            follow_up_choice="custom",
        )


@pytest.mark.asyncio
async def test_same_stage_without_effects_is_a_true_no_op() -> None:
    stage = SimpleNamespace(id=uuid4(), key="emailed")
    result = SimpleNamespace(scalar_one_or_none=lambda: stage)
    db = SimpleNamespace(
        get=AsyncMock(return_value=stage),
        execute=AsyncMock(return_value=result),
        flush=AsyncMock(),
    )
    prospect = SimpleNamespace(
        id=uuid4(),
        stage_definition_id=stage.id,
        primary_contact_id=uuid4(),
        appointment_id=None,
        converted_intake_id=None,
        converted_application_id=None,
        next_follow_up_at=None,
        do_not_contact=False,
        do_not_contact_reason=None,
        last_activity_at=None,
        version=7,
    )

    result_prospect = await prospects.move_stage(
        db,
        _user(),
        prospect,
        stage_key="emailed",
        expected_version=7,
        note=None,
        next_follow_up_at=None,
        action="none",
        appointment_id=None,
        confirm_do_not_contact=False,
    )

    assert result_prospect is prospect
    assert prospect.version == 7
    db.flush.assert_not_awaited()


def test_router_exposes_identity_call_follow_up_and_timeline_contracts() -> None:
    paths = {
        (route.path, method) for route in prospect_router.router.routes for method in route.methods
    }
    assert ("/dealer-os/prospects/duplicate-check", "GET") in paths
    assert ("/dealer-os/prospects/reassignment-requests", "POST") in paths
    assert ("/dealer-os/prospects/{prospect_id}/restore", "POST") in paths
    assert ("/dealer-os/prospects/{prospect_id}/call-attempts", "POST") in paths
    assert ("/dealer-os/prospects/{prospect_id}/follow-up-suggestion", "GET") in paths
    assert ("/dealer-os/prospects/{prospect_id}/timeline", "GET") in paths


def test_timeline_cursor_round_trip_is_stable() -> None:
    at = datetime(2026, 9, 18, 14, 30, tzinfo=UTC)
    cursor = prospects._timeline_cursor(at, "message:123:delivered")
    assert prospects._decode_timeline_cursor(cursor) == (
        at,
        "message:123:delivered",
    )


def test_timeline_keeps_provider_acceptance_next_to_terminal_delivery() -> None:
    assert (
        prospects._timeline_email_activity_kind(
            "email.sent",
            draft_id="draft-1",
            message_draft_ids={"draft-1"},
        )
        == "email.provider_accepted"
    )
    assert (
        prospects._timeline_email_activity_kind(
            "email.delivered",
            draft_id="draft-1",
            message_draft_ids={"draft-1"},
        )
        is None
    )


def test_timeline_suppresses_only_mirrored_appointment_created_event() -> None:
    appointment_id = uuid4()
    assert not prospects._timeline_include_appointment_activity(
        event_type="appointment_created",
        appointment_id=appointment_id,
        prospect_appointment_ids={str(appointment_id)},
    )
    assert prospects._timeline_include_appointment_activity(
        event_type="appointment_cancelled",
        appointment_id=appointment_id,
        prospect_appointment_ids={str(appointment_id)},
    )
