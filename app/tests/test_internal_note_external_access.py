"""Private note channels stay out of real legacy partner and external reads."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

from app.enums import Role
from app.routers import buckets
from app.routers import dealer_ai_intake as intake_routes
from app.routers.dealer_ai_intake_routes import admin, broker
from app.schemas.bucket import BucketNoteRead, BucketShareAccessRequest


def _note(index: int, **overrides) -> SimpleNamespace:
    values = {
        "id": UUID(int=index),
        "bucket_id": UUID(int=100),
        "share_id": None,
        "vendor_access_id": None,
        "channel": "partner",
        "visibility": "admin",
        "include_in_ai_context": False,
        "author_name": "Staff member",
        "author_role": Role.BROKER.value,
        "content": f"Note {index}",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _intake(index: int, notes: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(
        id=UUID(int=index),
        bucket_id=UUID(int=100),
        bucket=SimpleNamespace(id=UUID(int=100), notes=notes),
        business_name=f"Business {index}",
        full_name="Applicant",
        status="in_progress",
        outcome_status="pending",
        last_message_at=notes[-1].created_at if notes else None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [Role.DEALER_PARTNER, Role.PROFESSIONAL_REFERRAL_PARTNER])
@pytest.mark.parametrize("selected", [True, False])
async def test_partner_note_route_excludes_private_notes_before_image_hydration(
    monkeypatch, role, selected
) -> None:
    partner_note = _note(1)
    private_note = _note(2, channel="internal", include_in_ai_context=selected)
    shared_note = _note(3, visibility="shared")
    intake = _intake(200, [private_note, shared_note, partner_note])
    user = SimpleNamespace(id=UUID(int=300), role=role)
    db = SimpleNamespace()
    authorize = AsyncMock()
    load = AsyncMock(return_value=intake)
    hydrate = AsyncMock(return_value={})
    monkeypatch.setattr(intake_routes, "_require_dealer_partner", authorize)
    monkeypatch.setattr(intake_routes, "_load_broker_dealer_lead", load)
    monkeypatch.setattr(intake_routes.inline_images, "hydrate", hydrate)

    result = await broker.list_broker_lead_notes(intake.id, user, db)

    authorize.assert_awaited_once_with(user, db)
    load.assert_awaited_once_with(db, user, intake.id)
    hydrate.assert_awaited_once_with(db, "bucket_note", [str(partner_note.id)])
    assert all(isinstance(note, BucketNoteRead) for note in result)
    assert [note.id for note in result] == [partner_note.id]
    assert private_note.content not in str([note.model_dump() for note in result])


@pytest.mark.asyncio
async def test_admin_legacy_partner_note_route_does_not_mix_private_channel(monkeypatch) -> None:
    partner_note = _note(1)
    private_note = _note(2, channel="internal", include_in_ai_context=True)
    intake = _intake(200, [private_note, partner_note])
    user = SimpleNamespace(id=UUID(int=300), role=Role.SUPER_ADMIN)
    db = SimpleNamespace()
    authorize = Mock()
    hydrate = AsyncMock(return_value={})
    monkeypatch.setattr(intake_routes, "_require_super_admin", authorize)
    monkeypatch.setattr(intake_routes, "_load_admin_dealer_lead", AsyncMock(return_value=intake))
    monkeypatch.setattr(intake_routes.inline_images, "hydrate", hydrate)

    result = await admin.list_admin_lead_notes(intake.id, user, db)

    authorize.assert_called_once_with(user)
    hydrate.assert_awaited_once_with(db, "bucket_note", [str(partner_note.id)])
    assert [note.id for note in result] == [partner_note.id]


def test_channel_preview_ignores_newer_private_note_and_internal_only_thread() -> None:
    partner_note = _note(1)
    private_note = _note(2, channel="internal", include_in_ai_context=True)

    preview = intake_routes._channel_last_message(_intake(200, [partner_note, private_note]))

    assert preview is not None
    assert preview.content == partner_note.content
    assert preview.created_at == partner_note.created_at
    assert intake_routes._channel_last_message(_intake(201, [private_note])) is None


@pytest.mark.asyncio
async def test_partner_inbox_omits_internal_only_file_and_private_preview(monkeypatch) -> None:
    partner_note = _note(1)
    private_note = _note(2, channel="internal", include_in_ai_context=True)
    partner_thread = _intake(200, [partner_note, private_note])
    internal_only = _intake(201, [private_note])
    user = SimpleNamespace(id=UUID(int=300), role=Role.DEALER_PARTNER)
    db = SimpleNamespace()
    unread = AsyncMock(return_value={partner_thread.id: 2})
    monkeypatch.setattr(intake_routes, "_channel_unread_by_intake", unread)

    result = await intake_routes._build_channel_inbox(db, user, [internal_only, partner_thread])

    unread.assert_awaited_once_with(db, user=user, intakes=[partner_thread])
    assert [item.intake_id for item in result.items] == [partner_thread.id]
    assert result.total_unread == 2
    assert result.items[0].last_message.content == partner_note.content


def _stub_external_response(monkeypatch, response_name: str) -> None:
    # Exercise the route's actual note selection and real BucketNoteRead DTO;
    # unrelated bucket/access models are outside this privacy regression.
    monkeypatch.setattr(
        buckets, "BucketRead", SimpleNamespace(model_validate=lambda row: SimpleNamespace(id=row.id))
    )
    monkeypatch.setattr(buckets, response_name, lambda **values: SimpleNamespace(**values))
    monkeypatch.setattr(buckets, "latest_review", AsyncMock(return_value=None))
    monkeypatch.setattr(buckets, "_log", AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_permission", [True, False])
async def test_vendor_bucket_excludes_private_channel_despite_legacy_permission(
    monkeypatch, legacy_permission
) -> None:
    access_id = UUID(int=400)
    shared_note = _note(1, visibility="shared")
    vendor_note = _note(2, vendor_access_id=access_id)
    legacy_note = _note(3)
    private_selected = _note(4, channel="internal", include_in_ai_context=True)
    private_unselected = _note(5, channel="internal")
    malformed_private = _note(
        6, channel="internal", visibility="shared", vendor_access_id=access_id,
        include_in_ai_context=True,
    )
    access = SimpleNamespace(
        id=access_id,
        bucket_id=UUID(int=100),
        bucket=SimpleNamespace(
            id=UUID(int=100),
            notes=[shared_note, vendor_note, legacy_note, private_selected, private_unselected, malformed_private],
        ),
        view_count=0,
        can_see_internal_notes=legacy_permission,
        can_view_ai_tasks=False,
        can_view_ai_summary=False,
    )
    db = SimpleNamespace(commit=AsyncMock())
    user = SimpleNamespace(id=UUID(int=300), role=Role.VENDOR)
    _stub_external_response(monkeypatch, "BucketVendorAccessReadResponse")
    loader = AsyncMock(return_value=access)
    monkeypatch.setattr(buckets, "_load_vendor_access_or_404", loader)
    monkeypatch.setattr(buckets, "_vendor_access_files", lambda row: [])
    monkeypatch.setattr(buckets, "_vendor_access_read", lambda row: None)

    result = await buckets.get_vendor_bucket(access.bucket_id, SimpleNamespace(), user, db)

    loader.assert_awaited_once_with(db, access.bucket_id, user)
    expected = [shared_note.id, vendor_note.id] + ([legacy_note.id] if legacy_permission else [])
    assert [note.id for note in result.notes] == expected
    assert all(isinstance(note, BucketNoteRead) for note in result.notes)
    assert access.view_count == 1
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_permission", [True, False])
async def test_share_access_excludes_private_channel_despite_legacy_permission(
    monkeypatch, legacy_permission
) -> None:
    share_id = UUID(int=500)
    operator_shared = _note(1, visibility="shared")
    recipient_shared = _note(2, visibility="shared", share_id=share_id)
    legacy_note = _note(3)
    private_selected = _note(4, channel="internal", include_in_ai_context=True)
    private_unselected = _note(5, channel="internal")
    malformed_private = _note(
        6, channel="internal", visibility="shared", share_id=share_id, include_in_ai_context=True,
    )
    share = SimpleNamespace(
        id=share_id,
        bucket_id=UUID(int=100),
        bucket=SimpleNamespace(
            id=UUID(int=100),
            notes=[operator_shared, recipient_shared, legacy_note, private_selected, private_unselected, malformed_private],
        ),
        files=[],
        recipient_name="Share recipient",
        recipient_email="recipient@example.test",
        passcode_hash="stored-hash",
        view_count=0,
        can_see_internal_notes=legacy_permission,
        can_view_ai_tasks=False,
        can_view_ai_summary=False,
    )
    db = SimpleNamespace(commit=AsyncMock())
    _stub_external_response(monkeypatch, "BucketShareAccessRead")
    loader = AsyncMock(return_value=share)
    monkeypatch.setattr(buckets, "_load_share_or_404", loader)
    monkeypatch.setattr(buckets, "_verify_passcode", lambda provided, stored: True)
    monkeypatch.setattr(buckets, "_share_read", lambda row: None)

    result = await buckets.share_access(
        "share-token", BucketShareAccessRequest(passcode="123456"), SimpleNamespace(), db
    )

    loader.assert_awaited_once_with(db, "share-token")
    expected = [operator_shared.id, recipient_shared.id] + ([legacy_note.id] if legacy_permission else [])
    assert [note.id for note in result.notes] == expected
    assert all(isinstance(note, BucketNoteRead) for note in result.notes)
    assert share.view_count == 1
    db.commit.assert_awaited_once()
