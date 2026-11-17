"""Invitation responses must survive real server-generated timestamp updates."""
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.dealer_os.services.consent_delivery import DeliveryResult
from app.enums import Role
from app.models.bucket import Bucket, BucketUploadLink
from app.models.client import Client
from app.models.user import User
from app.routers import dealer_ai_intake as intake_api
from app.tests._sqlite_schema import sqlite_session, sqlite_template


@pytest.fixture(scope="module")
def template_database(tmp_path_factory):
    return sqlite_template(tmp_path_factory, "intake_invitation_response")


@pytest.mark.parametrize("accepted", [True, False])
async def test_file_response_reads_database_timestamp_after_invitation(template_database, tmp_path, accepted):
    async with sqlite_session(template_database, tmp_path) as db:
        user = User(email="desk@example.com", name="Example Desk", role=Role.SUPER_ADMIN)
        client = Client(name="Example Owner", email="owner@example.com")
        bucket = Bucket(name="Example room", requested_documents=[], files=[], notes=[])
        db.add_all([user, client, bucket])
        await db.flush()
        link = BucketUploadLink(bucket_id=bucket.id, token=uuid4().hex, recipient_name="Example Owner")
        db.add(link)
        await db.commit()

        async def serialize(_db, intake, **kwargs):
            # This is the first operation performed by the production response
            # serializer; a mocked timestamp cannot expose its lazy-load bug.
            result = intake_api.DealerIntakeRead.model_validate(intake)
            return result, kwargs

        payload = intake_api.AdminLeadCreate(
            full_name="Example Owner", email="owner@example.com",
            application_completion="client", secure_room_pin="234567",
            invitation_channels=["email"],
        )
        with ExitStack() as stack:
            for name in ("_find_or_create_client", "_find_or_create_funding_client", "_find_or_create_mca_client"):
                stack.enter_context(patch.object(intake_api, name, AsyncMock(return_value=client)))
            stack.enter_context(patch.object(intake_api, "_latest_active_intake_by_email", AsyncMock(return_value=None)))
            stack.enter_context(patch.object(intake_api, "_create_bucket_for_intake", AsyncMock(return_value=(bucket, link))))
            stack.enter_context(patch.object(intake_api, "_log", AsyncMock()))
            stack.enter_context(patch.object(intake_api.profiles_service, "provision_profile_for_intake", AsyncMock()))
            stack.enter_context(patch.object(intake_api, "_response", serialize))
            provider = stack.enter_context(patch.object(
                intake_api.consent_delivery, "deliver_link_checked",
                AsyncMock(return_value=DeliveryResult(accepted, "email", "Delivery result", email_ok=accepted)),
            ))
            result, metadata = await intake_api._create_admin_ai_lead_core(payload, SimpleNamespace(), user, db)

        assert result.updated_at is not None
        assert metadata["room_delivery_status"] == ("sent" if accepted else "failed")
        provider.assert_awaited_once()
