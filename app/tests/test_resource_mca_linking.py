from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.models.client import Client
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.routers import dealer_ai_intake as core
from app.routers import mca_leads
from app.routers.dealer_ai_intake_routes import mca_refinance
from app.services import mca_public_intake, resource_assessments
from app.tests._business_profile_fixture import BUSINESS_PROFILE
from app.tests.test_mca_leads import request_body


def request():
    return Request({"type": "http", "headers": [], "client": ("192.0.2.92", 1000)})


def scalar(value):
    return SimpleNamespace(scalar_one_or_none=lambda: value)


def profile():
    return {**BUSINESS_PROFILE, "business_phone": "2125550123", "employee_count": 4,
        "sms_transactional_consent": True, "application_email_consent": True, "secure_room_pin": "627493",
        "terms_accepted": True, "privacy_accepted": True, "resource_handoff_token": "a" * 97}


async def test_new_mca_adopts_only_resource_capability_client_and_keeps_token_out_of_state(monkeypatch):
    payload = mca_leads.McaIntakeStart.model_validate({**request_body(), **profile(), "consent_version": "mca-ai-intake-v3",
        "attribution": {"page": "/business-funding-check", "campaign": "resource"}})
    client = Client(id=uuid4(), name="Saved relationship", email=payload.email, lead_intake={"private_saved_context": "preserved"})
    capture = SimpleNamespace(client_id=client.id)
    bucket, link = SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4(), token="new-mca-room")
    monkeypatch.setattr(resource_assessments, "resolve", AsyncMock(return_value=capture))
    linked = AsyncMock()
    monkeypatch.setattr(resource_assessments, "link_application", linked)
    monkeypatch.setattr(core, "_create_bucket_for_main_street", AsyncMock(return_value=(bucket, link)))
    monkeypatch.setattr(core.profiles_service, "provision_profile_for_intake", AsyncMock())
    monkeypatch.setattr("app.services.onboarding_consent.record_for_start", AsyncMock())
    mca_public_intake._STARTS.clear()
    db = Mock(get=AsyncMock(side_effect=[None, client]), execute=AsyncMock(side_effect=[scalar(None), scalar(None)]),
              flush=AsyncMock(), commit=AsyncMock())
    result = await mca_public_intake.start(db, payload, request())
    intake = next(call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], PublicUnderwritingIntake))
    assert intake.client_id == client.id
    assert not any(isinstance(call.args[0], Client) for call in db.add.call_args_list)
    assert "resource_handoff_token" not in str(intake.intake_state)
    assert client.lead_intake == {"private_saved_context": "preserved"}
    assert result["funding_intent"] == "merchant_cash_advance"
    linked.assert_awaited_once_with(db, capture, intake)


async def test_new_mca_resource_token_cannot_return_an_existing_room(monkeypatch):
    payload = mca_leads.McaIntakeStart.model_validate({**request_body(), **profile(), "consent_version": "mca-ai-intake-v3"})
    monkeypatch.setattr(resource_assessments, "resolve", AsyncMock(return_value=SimpleNamespace(client_id=uuid4())))
    mca_public_intake._STARTS.clear()
    db = Mock(get=AsyncMock(return_value=None), execute=AsyncMock(side_effect=[scalar(None), scalar(uuid4())]))
    with pytest.raises(HTTPException) as error:
        await mca_public_intake.start(db, payload, request())
    assert error.value.status_code == 409
    db.add.assert_not_called()


async def test_refinance_start_links_same_resource_client(monkeypatch):
    payload = core.McaRefiStart.model_validate({**profile(), "full_name": "Example Owner", "business_name": "Example Company",
        "email": "owner@example.com", "phone": "2125550123", "active_mca_count": 1,
        "years_in_business": 3, "requested_amount": 50000})
    client = Client(id=uuid4(), name="Saved relationship", email=payload.email)
    capture = SimpleNamespace(client_id=client.id)
    bucket = SimpleNamespace(id=uuid4(), ai_context={}, bucket_type="mca_refinance_intake")
    link = SimpleNamespace(id=uuid4(), token="new-refi-room")
    created = []
    def add(row):
        if isinstance(row, PublicUnderwritingIntake):
            row.id = uuid4()
            created.append(row)
    monkeypatch.setattr(core, "_latest_active_intake_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(core, "_create_bucket_for_mca_refi", AsyncMock(return_value=(bucket, link)))
    monkeypatch.setattr(core, "_throttle_or_429", Mock())
    monkeypatch.setattr(core, "_load_public_intake", AsyncMock(side_effect=lambda *_: created[0]))
    monkeypatch.setattr(core, "_record_resume_email", AsyncMock())
    monkeypatch.setattr(core, "_response", AsyncMock(return_value={"status": "created"}))
    monkeypatch.setattr(core.profiles_service, "provision_profile_for_intake", AsyncMock())
    monkeypatch.setattr("app.services.onboarding_consent.record_for_start", AsyncMock())
    monkeypatch.setattr(resource_assessments, "resolve", AsyncMock(return_value=capture))
    linked = AsyncMock()
    monkeypatch.setattr(resource_assessments, "link_application", linked)
    db = Mock(get=AsyncMock(return_value=client), add=add, flush=AsyncMock(), commit=AsyncMock())
    result = await mca_refinance.start_mca_refinance(payload, request(), db)
    assert result == {"status": "created"}
    assert created[0].client_id == client.id and created[0].variant == "mca_refi_v1"
    linked.assert_awaited_once_with(db, capture, created[0])


async def test_refinance_existing_active_file_still_requires_code_before_resource_link(monkeypatch):
    payload = core.McaRefiStart.model_validate({**profile(), "full_name": "Example Owner",
        "email": "owner@example.com", "phone": "2125550123"})
    monkeypatch.setattr(core, "_latest_active_intake_by_email", AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    challenge = AsyncMock()
    monkeypatch.setattr(core, "_start_login_challenge", challenge)
    monkeypatch.setattr(core, "_throttle_or_429", Mock())
    resolve = AsyncMock()
    monkeypatch.setattr(resource_assessments, "resolve", resolve)
    with pytest.raises(HTTPException) as error:
        await mca_refinance.start_mca_refinance(payload, request(), Mock(commit=AsyncMock()))
    assert error.value.status_code == 409
    challenge.assert_awaited_once()
    resolve.assert_not_awaited()
