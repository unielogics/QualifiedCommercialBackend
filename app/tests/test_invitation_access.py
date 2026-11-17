from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.models.application_profile import ApplicationProfile
from app.models.bucket import Bucket, BucketActivityLog, BucketUploadLink
from app.models.client import Client
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User
from app.routers import application_profiles as routes
from app.schemas.application_profile import RoomReminderCreate
from app.services import invitation_access
from app.tests._sqlite_schema import sqlite_session, sqlite_template


@pytest.fixture(scope="module")
def template_database(tmp_path_factory):
    return sqlite_template(tmp_path_factory, "invitation_access")


@pytest.mark.parametrize("access", ["never", "account", "room", "other_room", "prior_room", "pin"])
async def test_sign_in_history_uses_client_and_canonical_room(template_database, tmp_path, access):
    async with sqlite_session(template_database, tmp_path) as db:
        user = User(email="owner@example.com", name="Example Owner", role=Role.CLIENT,
                    last_seen_at=datetime.now(UTC) if access == "account" else None)
        db.add(user)
        await db.flush()
        client = Client(name="Example Owner", email=user.email, user_id=user.id)
        bucket = Bucket(name="Example room")
        db.add_all([client, bucket])
        await db.flush()
        link = BucketUploadLink(bucket_id=bucket.id, token=uuid4().hex, recipient_name="Example Owner",
                                recipient_email=user.email,
                                passcode_set_by_client_at=datetime.now(UTC) if access == "pin" else None)
        intake = PublicUnderwritingIntake(bucket_id=bucket.id, client_id=client.id, token_hash=uuid4().hex,
                                         full_name="Example Owner", email=user.email)
        db.add_all([link, intake])
        await db.flush()
        profile = ApplicationProfile(primary_bucket_id=bucket.id, intake_id=intake.id, client_id=client.id)
        db.add(profile)
        if access in {"room", "other_room", "prior_room"}:
            db.add(BucketActivityLog(bucket_id=bucket.id, action="upload_link_accessed", actor_role="uploader",
                                    target_type="upload_link", target_id=str(link.id if access == "room" else uuid4()),
                                    actor_email=user.email if access == "prior_room" else None,
                                    created_at=datetime.now(UTC)))
        await db.flush()
        assert await invitation_access.client_has_signed_in(db, profile, link) is (access in {"account", "room", "prior_room", "pin"})


@pytest.mark.parametrize(("signed_in", "recent", "payload", "expected"), [
    (True, False, {}, 409),
    (False, False, {"recipient_email": "other@example.com"}, 422),
    (False, False, {"sms_reminder": True}, 422),
    (False, True, {}, 429),
])
async def test_invitation_send_rechecks_access_and_never_contacts_wrong_recipient(monkeypatch, signed_in, recent, payload, expected):
    profile = SimpleNamespace(id=uuid4(), intake_id=None, primary_bucket_id=uuid4())
    link = SimpleNamespace(recipient_email="owner@example.com")
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: uuid4() if recent else None)))
    monkeypatch.setattr(routes.profiles, "load_profile", AsyncMock(return_value=profile))
    monkeypatch.setattr(routes, "_profile_room_link", AsyncMock(return_value=link))
    monkeypatch.setattr(invitation_access, "client_has_signed_in", AsyncMock(return_value=signed_in))
    delivery = AsyncMock()
    monkeypatch.setattr(routes, "_deliver_room_request", delivery)
    with pytest.raises(HTTPException) as error:
        await routes.send_application_room_reminder(profile.id, RoomReminderCreate(purpose="invitation", **payload),
                                                   SimpleNamespace(), SimpleNamespace(role=Role.SUPER_ADMIN), db)
    assert error.value.status_code == expected
    delivery.assert_not_awaited()
