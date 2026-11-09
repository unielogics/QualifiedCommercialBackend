from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.dealer_os.services.consent_delivery import DeliveryResult
from app.enums import Role
from app.models.application_profile import ApplicationRoomDelivery
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.routers import dealer_ai_intake as intake_api


def _payload(**changes):
    return intake_api.AdminLeadCreate(
        **{
            "full_name": "Alex Owner",
            "application_completion": "client",
            "secure_room_pin": "123456",
            "invitation_channels": [],
            **changes,
        }
    )


def test_link_only_draft_can_defer_contact_without_changing_public_start_forms():
    payload = _payload()
    assert payload.email is None and payload.phone is None
    for schema in (intake_api.DealerIntakeStart, intake_api.FundingReviewStart, intake_api.McaRefiStart):
        with pytest.raises(ValidationError):
            schema(full_name="Alex Owner")
    with pytest.raises(ValidationError, match="phone number is required"):
        _payload(application_completion="operator")
    with pytest.raises(ValidationError):
        _payload(full_name="   ")


@pytest.mark.parametrize(
    ("channels", "contacts", "missing"),
    [
        (["email"], {}, "email address"),
        (["sms"], {}, "mobile number"),
        (["email", "sms"], {"email": "alex@example.com"}, "mobile number"),
    ],
)
def test_selected_delivery_channel_requires_its_contact(channels, contacts, missing):
    with pytest.raises(ValidationError, match=missing):
        _payload(invitation_channels=channels, **contacts)


def test_email_and_sms_can_each_be_selected_without_the_other_contact():
    assert _payload(invitation_channels=["email"], email="alex@example.com").phone is None
    assert _payload(invitation_channels=["sms"], phone="9735550148").email is None
    assert _payload(invitation_channels=["sms", "sms"], phone="9735550148").invitation_channels == ["sms"]


class _Db:
    def __init__(self):
        self.rows = []
        self.commit = AsyncMock()
        self.flush = AsyncMock()
        self.rollback = AsyncMock()
        self.refresh = AsyncMock()
        self.execute = AsyncMock()

    def add(self, row):
        if getattr(row, "id", None) is None:
            row.id = uuid4()
        self.rows.append(row)


async def _create(payload, delivery_results=()):
    db = _Db()
    user = SimpleNamespace(id=uuid4(), role=Role.SUPER_ADMIN, name="Morgan Desk", email="desk@example.com")
    client = SimpleNamespace(id=uuid4(), email=payload.email, lead_intake={})
    bucket = SimpleNamespace(id=uuid4())
    link = SimpleNamespace(id=uuid4(), token="secure-room-token")

    async def load(*_):
        return next(row for row in db.rows if isinstance(row, PublicUnderwritingIntake))

    async def deliver(*args, **kwargs):
        # Providers cannot be contacted until the file is durably created.
        assert db.commit.await_count >= 1
        result = next(results)
        if isinstance(result, Exception):
            raise result
        return result

    results = iter(delivery_results)
    with ExitStack() as stack:
        stack.enter_context(patch.object(intake_api, "_latest_active_intake_by_email", AsyncMock(return_value=None)))
        for name in ("_find_or_create_client", "_find_or_create_funding_client", "_find_or_create_mca_client"):
            stack.enter_context(patch.object(intake_api, name, AsyncMock(return_value=client)))
        for name in ("_create_bucket_for_intake", "_create_bucket_for_main_street", "_create_bucket_for_funding_review", "_create_bucket_for_mca_refi"):
            stack.enter_context(patch.object(intake_api, name, AsyncMock(return_value=(bucket, link))))
        stack.enter_context(patch.object(intake_api, "_log", AsyncMock()))
        stack.enter_context(patch.object(intake_api.profiles_service, "provision_profile_for_intake", AsyncMock()))
        stack.enter_context(patch.object(intake_api, "_load_admin_dealer_lead", AsyncMock(side_effect=load)))
        response = stack.enter_context(patch.object(intake_api, "_response", AsyncMock()))
        delivery = stack.enter_context(patch.object(intake_api.consent_delivery, "deliver_link_checked", AsyncMock(side_effect=deliver)))
        await intake_api._create_admin_ai_lead_core(payload, SimpleNamespace(), user, db)
    return db, response.await_args.kwargs, delivery.await_args_list


@pytest.mark.parametrize("variant", ["dealer", "real_estate", "main_street", "mca_refinance"])
async def test_minimal_link_only_file_works_for_every_supported_type(variant):
    db, result, calls = await _create(_payload(variant=variant))
    intake = next(row for row in db.rows if isinstance(row, PublicUnderwritingIntake))
    assert intake.email == "" and intake.phone is None
    assert intake.intake_state["application_completion"] == "client"
    assert result["secure_room_pin"] == "123456"
    assert result["room_delivery_status"] == "created"
    assert calls == []


async def test_both_channels_have_independent_receipts_and_partial_failure_keeps_link():
    db, result, calls = await _create(
        _payload(email="alex@example.com", phone="9735550148", invitation_channels=["email", "sms"]),
        [DeliveryResult(True, "email", "Emailed.", email_ok=True), DeliveryResult(False, "sms", "SMS unavailable")],
    )
    assert result["room_delivery_status"] == "partial"
    assert [(row.channel, row.status) for row in result["room_deliveries"]] == [("email", "sent"), ("sms", "failed")]
    assert calls[0].kwargs["to_phone"] is None
    assert calls[1].kwargs["to_email"] is None  # No second implicit email.
    assert all(call.kwargs["path"] == "/buckets/request/secure-room-token?tab=application" for call in calls)
    assert len([row for row in db.rows if isinstance(row, ApplicationRoomDelivery)]) == 2
    assert "123456" not in result["room_delivery_detail"]


async def test_sms_only_has_no_email_leg_and_reports_provider_acceptance():
    _, result, calls = await _create(
        _payload(phone="9735550148", invitation_channels=["sms"]),
        [DeliveryResult(True, "sms", "Texted", sms_ok=True)],
    )
    assert result["room_delivery_status"] == "sent"
    assert len(calls) == 1 and calls[0].kwargs["to_email"] is None
    assert result["room_deliveries"][0].provider_accepted is True
    assert result["room_deliveries"][0].recipient_masked == "***-***-0148"


async def test_provider_exception_keeps_created_file_and_other_delivery():
    db, result, calls = await _create(
        _payload(email="alex@example.com", phone="9735550148", invitation_channels=["email", "sms"]),
        [RuntimeError("provider offline"), DeliveryResult(True, "sms", "Texted", sms_ok=True)],
    )
    assert result["room_delivery_status"] == "partial"
    assert len(calls) == 2
    assert len([row for row in db.rows if isinstance(row, PublicUnderwritingIntake)]) == 1
    assert result["room_deliveries"][0].status == "failed"
    db.rollback.assert_awaited_once()


async def test_legacy_notify_and_explicit_link_only_are_compatible():
    payload = _payload(email="alex@example.com", invitation_channels=None, notify_client=True)
    _, result, calls = await _create(payload, [DeliveryResult(True, "email", "Emailed.", email_ok=True)])
    assert result["room_delivery_status"] == "sent" and len(calls) == 1
    _, result, calls = await _create(_payload(email="alex@example.com", notify_client=True))
    assert result["room_delivery_status"] == "created" and calls == []


async def test_nested_transaction_cannot_send_invitation():
    db = _Db()
    with pytest.raises(HTTPException, match="commit the file"):
        await intake_api._create_admin_ai_lead_core(
            _payload(invitation_channels=["email"], email="alex@example.com"),
            SimpleNamespace(), SimpleNamespace(), db, commit=False,
        )
    assert db.rows == []


@pytest.mark.parametrize(
    ("helper", "schema"),
    [
        (intake_api._find_or_create_client, intake_api._AdminDealerIntakeStart),
        (intake_api._find_or_create_funding_client, intake_api._AdminFundingReviewStart),
        (intake_api._find_or_create_mca_client, intake_api._AdminMcaRefiStart),
    ],
)
async def test_missing_email_never_matches_an_unrelated_client(helper, schema):
    db = _Db()
    with patch.object(intake_api, "primary_super_admin", AsyncMock(return_value=None)):
        first = await helper(db, intake_api.business_profile._legacy_adapter(schema, full_name="First Owner"))
        second = await helper(db, intake_api.business_profile._legacy_adapter(schema, full_name="Second Owner"))
    assert first.id != second.id and first.email is None and second.email is None
    db.execute.assert_not_awaited()


@pytest.mark.parametrize(
    ("helper", "schema"),
    [
        (intake_api._find_or_create_client, intake_api._AdminDealerIntakeStart),
        (intake_api._find_or_create_funding_client, intake_api._AdminFundingReviewStart),
        (intake_api._find_or_create_mca_client, intake_api._AdminMcaRefiStart),
    ],
)
async def test_long_invitation_name_fits_the_crm_without_shortening_intake(helper, schema):
    name = "A" * 85 + " " + "B" * 94
    payload = intake_api.business_profile._legacy_adapter(schema, full_name=name)
    db = _Db()
    with patch.object(intake_api, "primary_super_admin", AsyncMock(return_value=None)):
        client = await helper(db, payload)
    assert len(client.name) == 160
    assert payload.full_name == name
    created, _, _ = await _create(_payload(full_name=name))
    intake = next(row for row in created.rows if isinstance(row, PublicUnderwritingIntake))
    assert intake.full_name == name


@pytest.mark.parametrize("name", ["A" * 180, "A" * 85 + " " + "B" * 94])
async def test_seeded_owner_name_fits_owner_columns_and_preserves_full_intake_name(name):
    from app.models.application_profile import ApplicationOwner

    db = _Db()
    db.execute.return_value = SimpleNamespace(scalar_one=lambda: 0)
    row = PublicUnderwritingIntake(id=uuid4(), full_name=name, email="", phone=None)
    profile = SimpleNamespace(id=uuid4(), dealer_id=None, client_id=None, intake_id=row.id)
    await intake_api.profiles_service._seed_primary_owner(db, profile, intake=row)
    owner = next(value for value in db.rows if isinstance(value, ApplicationOwner))
    assert len(owner.first_name) <= 80 and len(owner.last_name) <= 80
    assert row.full_name == name
