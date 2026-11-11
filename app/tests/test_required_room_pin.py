"""A generated invitation must become a client-chosen room PIN before filing."""
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.dealer_os.services import client_room
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.routers import buckets
from app.routers import dealer_ai_intake as core
from app.routers.dealer_ai_intake_routes import client, funding_review, mca_refinance, public_intake
from app.schemas.bucket import BucketFileUploadInit
from app.tests._business_profile_fixture import BUSINESS_PROFILE

NOW = datetime.now(UTC)
PIN = "729518"


def request():
    return Request({"type": "http", "headers": [], "client": ("192.0.2.81", 1400)})


def link(**changes):
    row = SimpleNamespace(id=uuid4(), bucket_id=uuid4(), token="existing-room", status="active",
        expires_at=None, completed_at=None, allow_multiple_sessions=True,
        recipient_name="Example Owner", recipient_email="owner@example.com",
        passcode_set_by_client_at=None, passcode_hash=None, encrypted_passcode=None,
        passcode_encryption_provider=None)
    row.__dict__.update(changes)
    return row


def scalar(value):
    return SimpleNamespace(scalar_one_or_none=lambda: value)


@pytest.mark.parametrize("state", [None, link(status="revoked"), link(expires_at=NOW - timedelta(days=1))])
def test_missing_or_unusable_rooms_are_not_reported_as_configured(state):
    assert client_room.passcode_needs_setup(state) is True
    with pytest.raises(HTTPException) as error:
        client_room.require_passcode_setup(state)
    assert error.value.status_code == 409 and error.value.detail["code"] == "room_access_unavailable"


def test_generated_and_client_chosen_credentials_have_distinct_completion_states():
    invitation = link()
    with pytest.raises(HTTPException) as error:
        client_room.require_passcode_setup(invitation)
    assert error.value.detail["code"] == "room_pin_setup_required"
    invitation.passcode_set_by_client_at = NOW
    assert client_room.passcode_needs_setup(invitation) is False
    client_room.require_passcode_setup(invitation)


@pytest.mark.parametrize(("model", "handler", "builder", "equipment"), [
    (core.DealerIntakeStart, public_intake.start_dealer_intake, "_create_bucket_for_intake", False),
    (core.DealerIntakeStart, public_intake.start_dealer_intake, "_create_bucket_for_main_street", True),
    (core.FundingReviewStart, funding_review.start_funding_review, "_create_bucket_for_funding_review", False),
    (core.McaRefiStart, mca_refinance.start_mca_refinance, "_create_bucket_for_mca_refi", False),
])
async def test_public_starts_store_client_choice_before_commit_without_recording_pin(model, handler, builder, equipment):
    created, checkpoints = [], []
    owner = SimpleNamespace(id=uuid4(), email="owner@example.com")
    bucket = SimpleNamespace(id=uuid4(), bucket_type="application", ai_context={}, files=[], requested_documents=[])
    room = link(bucket_id=bucket.id)

    def add(row):
        if isinstance(row, PublicUnderwritingIntake):
            row.id = uuid4()
            created.append(row)

    db = SimpleNamespace(add=add, flush=AsyncMock(), commit=AsyncMock(side_effect=lambda: checkpoints.append(room.passcode_set_by_client_at)))
    payload = model(**BUSINESS_PROFILE, full_name="Example Owner", email=owner.email, phone="2025550123", secure_room_pin=PIN,
        terms_accepted=True, privacy_accepted=True)
    with ExitStack() as stack:
        for name in ("_find_or_create_client", "_find_or_create_funding_client", "_find_or_create_mca_client"):
            stack.enter_context(patch.object(core, name, AsyncMock(return_value=owner)))
        build = stack.enter_context(patch.object(core, builder, AsyncMock(return_value=(bucket, room))))
        for name in ("_latest_active_intake_by_email", "_record_resume_email", "_record_super_admin_intake_notification"):
            stack.enter_context(patch.object(core, name, AsyncMock(return_value=None if name == "_latest_active_intake_by_email" else {"ok": True})))
        stack.enter_context(patch.object(core, "_load_public_intake", AsyncMock(side_effect=lambda *_: created[0])))
        stack.enter_context(patch.object(core, "_response", AsyncMock(return_value="created")))
        stack.enter_context(patch.object(core, "_throttle_or_429", Mock()))
        stack.enter_context(patch.object(core, "_is_business_equipment_application", return_value=equipment))
        stack.enter_context(patch("app.services.onboarding_consent.record_for_start", AsyncMock()))
        stack.enter_context(patch.object(core.profiles_service, "provision_profile_for_intake", AsyncMock()))
        assert await handler(payload, request(), db) == "created"
    assert build.await_args.kwargs["room_pin"] == PIN
    assert checkpoints and all(checkpoints)
    assert PIN not in repr(created[0].intake_state)
    assert PIN not in repr(payload)


@pytest.mark.parametrize("model", [core.DealerIntakeStart, core.FundingReviewStart, core.McaRefiStart])
@pytest.mark.parametrize("pin", ["123456", "111111", "121212", "12345", "١٢٣٤٥٦"])
def test_every_public_start_rejects_weak_or_invalid_pin(model, pin):
    with pytest.raises(ValidationError) as error:
        model(**BUSINESS_PROFILE, full_name="Owner", email="owner@example.com", phone="2025550123", secure_room_pin=pin)
    assert {item["loc"] for item in error.value.errors()} == {("secure_room_pin",)}


async def handoff(room, pin):
    intake = SimpleNamespace(id=uuid4(), bucket_id=room.bucket_id if room else uuid4(), bucket_upload_link=room,
        full_name="Owner", email="owner@example.test")
    db = SimpleNamespace(execute=AsyncMock(return_value=scalar(room)), flush=AsyncMock(), commit=AsyncMock())
    core._ROOM_HANDOFF_LAST_BY_TOKEN.clear()
    with patch.object(core.profiles_service, "provision_profile_for_intake", AsyncMock()), patch.object(core, "_log", AsyncMock()):
        answer = await core._secure_room_handoff(db, intake, request(), tab="todo", new_passcode=pin)
    return answer, db


async def test_verified_legacy_setup_stores_the_new_pin_once_and_returns_no_client_secret():
    room = link()
    client_room._store_passcode(room, "104293")
    answer, db = await handoff(room, PIN)
    assert room.passcode_set_by_client_at is not None
    assert client_room.verify_passcode(PIN, room.passcode_hash)
    assert not client_room.verify_passcode("104293", room.passcode_hash)
    assert answer.room_pin_setup_required is False and answer.room_code is None
    assert answer.code_source == "client_chosen"
    assert "FOR UPDATE" in str(db.execute.await_args.args[0])
    db.commit.assert_awaited_once()


async def test_an_intake_token_cannot_replace_an_already_configured_room_pin():
    room = link()
    client_room._store_passcode(room, "104293")
    room.passcode_set_by_client_at = NOW
    original = room.passcode_hash
    with pytest.raises(HTTPException) as error:
        await handoff(room, PIN)
    assert error.value.status_code == 409 and error.value.detail["code"] == "room_pin_already_configured"
    assert room.passcode_hash == original


async def test_lost_successful_setup_response_can_be_retried_without_changing_the_pin_or_choice_date():
    room = link()
    client_room._store_passcode(room, PIN)
    room.passcode_set_by_client_at = NOW
    original = room.passcode_hash
    answer, _db = await handoff(room, PIN)
    assert answer.room_pin_setup_required is False and answer.room_code is None
    assert room.passcode_set_by_client_at == NOW and room.passcode_hash == original


def test_staff_replacement_of_a_chosen_pin_rearms_client_setup():
    room = link(passcode_set_by_client_at=NOW)
    client_room._store_passcode(room, "104293")
    assert room.passcode_set_by_client_at is None


async def test_room_setup_retry_and_another_tab_never_overwrite_the_first_choice():
    from app.schemas.bucket import BucketRequestPasscodeChange

    room = link()
    client_room._store_passcode(room, PIN)
    room.passcode_set_by_client_at = NOW
    original = room.passcode_hash
    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())
    with patch.object(buckets, "_load_upload_link_or_404", AsyncMock(return_value=room)), \
         patch.object(buckets, "_client_ip", return_value="192.0.2.81"), \
         patch.object(buckets, "_log", AsyncMock()):
        answer = await buckets.request_link_choose_passcode("existing-room",
            BucketRequestPasscodeChange(passcode="104293", new_passcode=PIN), request(), db)
        assert answer.passcode_needs_setup is False and room.passcode_hash == original
        with pytest.raises(HTTPException) as error:
            await buckets.request_link_choose_passcode("existing-room",
                BucketRequestPasscodeChange(passcode="104293", new_passcode="638274"), request(), db)
    assert error.value.status_code == 403
    assert room.passcode_set_by_client_at == NOW and room.passcode_hash == original
    assert db.refresh.await_args.kwargs == {"with_for_update": True}


@pytest.mark.parametrize("changes", [{"status": "revoked"}, {"expires_at": NOW - timedelta(days=1)}])
async def test_legacy_setup_cannot_revive_a_revoked_or_expired_link(changes):
    room = link(**changes)
    with pytest.raises(HTTPException) as error:
        await handoff(room, PIN)
    assert error.value.status_code == 409 and error.value.detail["code"] == "room_access_unavailable"
    assert room.passcode_hash is None


async def test_room_upload_requires_chosen_pin_before_storage_or_file_mutation():
    room = link(passcode_hash="verified-invite")
    db = SimpleNamespace(commit=AsyncMock(), execute=AsyncMock(), get=AsyncMock())
    with patch.object(buckets, "_load_upload_link_or_404", AsyncMock(return_value=room)), \
         patch.object(buckets, "_verify_passcode", return_value=True), \
         patch("app.services.arf_channel.require_bucket_hosted_permission", AsyncMock()), \
         patch.object(buckets, "_client_ip", return_value="192.0.2.81"), \
         patch.object(buckets, "_upload_url") as storage:
        with pytest.raises(HTTPException) as error:
            await buckets.request_upload_init("existing-room", BucketFileUploadInit(
                file_name="statement.pdf", content_type="application/pdf", size_bytes=100,
                uploader_name="Owner", passcode="104293"), request(), db)
    assert error.value.detail["code"] == "room_pin_setup_required"
    storage.assert_not_called()
    db.get.assert_not_awaited()


@pytest.mark.parametrize(("handler", "client_account"), [
    (public_intake.dealer_upload_init, False), (funding_review.funding_review_upload_init, False),
    (mca_refinance.mca_refinance_upload_init, False), (client.my_dealer_upload_init, True),
    (public_intake.dealer_submit_pfs, False), (public_intake.dealer_submit_debt_schedule, False),
    (client.my_dealer_submit_pfs, True), (client.my_dealer_submit_debt_schedule, True),
    (mca_refinance.submit_mca_refinance_terms, False),
])
async def test_customer_upload_and_inline_form_completion_cannot_skip_setup(handler, client_account):
    intake = SimpleNamespace(bucket_upload_link=link(), variant=core.DEALER_VARIANT)
    db = SimpleNamespace()
    with patch.object(core, "_load_public_intake", AsyncMock(return_value=intake)), \
         patch.object(core, "_load_client_intake", AsyncMock(return_value=intake)), \
         patch.object(core, "_require_dealer_intake"), patch.object(core, "_require_funding_intake"), \
         patch.object(core, "_require_mca_intake"):
        args = {"intake_id": uuid4(), "user": SimpleNamespace()} if client_account else {"token": "intake-session"}
        with pytest.raises(HTTPException) as error:
            await handler(payload=SimpleNamespace(), request=request(), db=db, **args)
    assert error.value.detail["code"] == "room_pin_setup_required"


async def test_logged_in_metadata_is_owner_scoped_and_never_reveals_a_pin():
    room = link()
    intake = SimpleNamespace(bucket_upload_link=room)
    user, db, intake_id = SimpleNamespace(), SimpleNamespace(), uuid4()
    with patch.object(core, "_load_client_intake", AsyncMock(return_value=intake)) as load:
        result = await client.my_dealer_room_status(intake_id, user, db)
    load.assert_awaited_once_with(db, user, intake_id, allow_pending_signing=True)
    assert set(result.model_dump()) == {"room_pin_setup_required", "room_url"}
    assert result.room_pin_setup_required is True


async def test_logged_in_legacy_setup_never_skips_the_existing_client_ownership_check():
    user, db, intake_id = SimpleNamespace(), SimpleNamespace(), uuid4()
    with patch.object(core, "_load_client_intake", AsyncMock(side_effect=HTTPException(404, "not found"))) as load, \
         patch.object(core, "_secure_room_handoff", AsyncMock()) as setup:
        with pytest.raises(HTTPException) as error:
            await client.my_dealer_secure_room(intake_id, request(), user, db, core.SecureRoomHandoffRequest(new_passcode=PIN))
    assert error.value.status_code == 404
    load.assert_awaited_once_with(db, user, intake_id, allow_pending_signing=True)
    setup.assert_not_awaited()


@pytest.mark.parametrize("handler", [public_intake.router_secure_room,
    funding_review.funding_router_secure_room, mca_refinance.mca_router_secure_room])
async def test_room_setup_remains_reachable_when_a_dealer_owes_a_signature(handler):
    intake, db = SimpleNamespace(), SimpleNamespace()
    with patch.object(core, "_load_public_intake", AsyncMock(return_value=intake)) as load, \
         patch.object(core, "_secure_room_handoff", AsyncMock(return_value="room")):
        assert await handler("existing-session", request(), db) == "room"
    load.assert_awaited_once_with(db, "existing-session", allow_pending_signing=True)
