from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.models.client import Client
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.routers import application_profiles as rooms
from app.routers import dealer_ai_intake as core
from app.routers.public_business_intake import BusinessIntakeStart
from app.services import business_public_intake as service
from app.services import resource_assessments
from app.tests._business_profile_fixture import BUSINESS_PROFILE


def body(**changes):
    return {**BUSINESS_PROFILE, "idempotency_key": str(uuid4()), "goal": "term_loan", "full_name": "Example Owner",
        "business_name": "Example Company", "email": "owner@example.com", "phone": "2125550123",
        "business_phone": "2125550123", "employee_count": 4, "secure_room_pin": "627493", "locale": "en",
        "terms_accepted": True, "privacy_accepted": True, "application_email_consent": True,
        "sms_transactional_consent": True, "resource_handoff_token": "a" * 97, **changes}


def request():
    return Request({"type": "http", "headers": [], "client": ("192.0.2.91", 1000)})


def scalar(value):
    return SimpleNamespace(scalar_one_or_none=lambda: value)


@pytest.fixture(autouse=True)
def reset_limits():
    service._STARTS.clear()
    service._RESUMES.clear()


@pytest.mark.parametrize("changes", [{"goal": "merchant_cash_advance"}, {"secure_room_pin": "12345"},
    {"secure_room_pin": None},
    {"application_email_consent": False}, {"sms_transactional_consent": False}, {"business_phone": "1"},
    {"requested_amount": -1}, {"years_in_business": -1}])
def test_start_has_its_own_consent_profile_and_product_boundary(changes):
    with pytest.raises(ValidationError):
        BusinessIntakeStart.model_validate(body(**changes))


async def test_term_start_creates_main_street_room_and_same_client_without_mca_rules(monkeypatch):
    payload = BusinessIntakeStart.model_validate(body(requested_amount=None, years_in_business=None, credit_band="660_699"))
    client = Client(id=uuid4(), name="Saved relationship", email=payload.email)
    capture = SimpleNamespace(client_id=client.id)
    bucket = SimpleNamespace(id=uuid4(), ai_context={})
    link = SimpleNamespace(id=uuid4(), token="new-term-room", passcode_set_by_client_at=None)
    builder = AsyncMock(return_value=(bucket, link))
    monkeypatch.setattr(core, "_create_bucket_for_main_street", builder)
    monkeypatch.setattr(core.profiles_service, "provision_profile_for_intake", AsyncMock())
    monkeypatch.setattr(resource_assessments, "resolve", AsyncMock(return_value=capture))
    linked = AsyncMock()
    monkeypatch.setattr(resource_assessments, "link_application", linked)
    monkeypatch.setattr("app.services.onboarding_consent.record_for_start", AsyncMock())
    mail = Mock()
    monkeypatch.setattr(core, "send_email", mail)
    db = Mock(get=AsyncMock(side_effect=[None, client]), execute=AsyncMock(side_effect=[scalar(None), scalar(None), scalar(None)]),
              flush=AsyncMock(), commit=AsyncMock())
    result = await service.start(db, payload, request())
    intake = next(call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], PublicUnderwritingIntake))
    assert intake.client_id == client.id and intake.variant == "main_street_v1"
    assert intake.intake_state["funding_goal"] == "term_loan"
    assert intake.intake_state["funding_intent"] == "working_capital"
    assert intake.intake_state["main_street_details"]["estimated_credit_band"] == "660_699"
    assert intake.estimated_credit_score is None
    assert "secure_room_pin" not in str(intake.intake_state) and "mca_product_policy" not in intake.intake_state
    assert builder.await_args.kwargs["intent"] == "working_capital"
    assert "term-loan" in bucket.ai_context["deal_type"].lower()
    assert result["room_url"].endswith("/new-term-room?tab=ai-review")
    linked.assert_awaited_once_with(db, capture, intake)
    mail.assert_not_called()


@pytest.mark.parametrize("kind", ["wrong_goal", "wrong_link", "wrong_profile", "disabled"])
async def test_room_pin_cannot_cross_product_or_link_identity(monkeypatch, kind):
    link = SimpleNamespace(id=uuid4(), bucket_id=uuid4(), can_use_ai_chat=kind != "disabled")
    profile = SimpleNamespace(intake_id=uuid4(), funding_category="equipment" if kind == "wrong_profile" else "working_capital")
    intake = SimpleNamespace(variant="main_street_v1", bucket_id=link.bucket_id,
        bucket_upload_link_id=uuid4() if kind == "wrong_link" else link.id,
        intake_state={"funding_goal": "merchant_cash_advance" if kind == "wrong_goal" else "term_loan"})
    monkeypatch.setattr(rooms, "_public_application_room", AsyncMock(return_value=(link, profile)))
    monkeypatch.setattr(core, "_load_admin_dealer_lead", AsyncMock(return_value=intake))
    with pytest.raises(HTTPException) as error:
        await service.authorize_room(Mock(), "room", "627493", request())
    assert error.value.status_code == (403 if kind == "disabled" else 404)


def test_term_chat_cannot_change_selected_product_or_turn_band_into_fico(monkeypatch):
    intake = SimpleNamespace(intake_state={"funding_goal": "term_loan", "main_street_details": {"intent": "working_capital"}})
    monkeypatch.setattr(core, "_is_main_street_equipment_intake", lambda _: False)
    monkeypatch.setattr(core, "_is_main_street_mca_intake", lambda _: False)
    result = core._merge_main_street_details(intake, {"intent": "merchant_cash_advance"})
    assert result["intent"] == "working_capital"
    assert intake.intake_state["funding_goal"] == "term_loan"
