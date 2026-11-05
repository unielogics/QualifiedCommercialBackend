from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError

from app.db import get_db
from app.enums import Role
from app.models.activity import Activity
from app.models.client import Client
from app.routers import clients, mca_leads


def request_body(**changes):
    return {
        "idempotency_key": str(uuid4()),
        "full_name": "Business Owner",
        "business_name": "Test Business LLC",
        "email": "owner@example.com",
        "phone": "(212) 555-0123",
        "active_mca_count": 2,
        "requested_amount": "50k_100k",
        "monthly_revenue": "25k_50k",
        "time_in_business": "2_plus_years",
        "consent": True,
        "locale": "en",
        "attribution": {"source": "public_site", "page": "/merchant-cash-advance", "utm_source": "google", "gclid": "test-click-id"},
        "website": "",
        **changes,
    }


def application(db):
    mca_leads._LAST_SUBMIT.clear()
    app = FastAPI()
    app.include_router(mca_leads.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"active_mca_count": 3}, {"active_mca_count": 12}, {"active_mca_count": -1},
    {"active_mca_count": True}, {"active_mca_count": "2"}, {"active_mca_count": 1.5},
    {"consent": False}, {"website": "https://spam.example"}, {"full_name": "  "},
    {"business_name": "  "}, {"phone": "123"}, {"monthly_revenue": "made_up"},
    {"credit_score": 800}, {"attribution": {"page": "https://other.example"}},
])
async def test_invalid_or_ineligible_request_never_creates_a_lead(changes):
    db = Mock()
    async with AsyncClient(transport=ASGITransport(app=application(db)), base_url="http://test") as client:
        response = await client.post("/api/v1/public/mca/leads", json=request_body(**changes))
    assert response.status_code == 422
    db.add.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("active_count", [0, 1, 2])
async def test_eligible_request_is_persisted_as_crm_lead_with_consent_and_attribution(active_count):
    db = Mock(get=AsyncMock(return_value=None), flush=AsyncMock(), commit=AsyncMock())
    owner = SimpleNamespace(id=uuid4())
    with patch.object(mca_leads, "primary_super_admin", AsyncMock(return_value=owner)):
        async with AsyncClient(transport=ASGITransport(app=application(db)), base_url="http://test") as client:
            response = await client.post("/api/v1/public/mca/leads", json=request_body(active_mca_count=active_count))
    assert response.status_code == 201
    assert response.json() == {"status": "received"}
    saved = [call.args[0] for call in db.add.call_args_list]
    lead = next(row for row in saved if isinstance(row, Client))
    activity = next(row for row in saved if isinstance(row, Activity))
    assert lead.phone == "+12125550123"
    assert lead.current_agent_id == owner.id
    assert lead.source_channel == "mca_funding"
    assert lead.stage == "lead"
    assert lead.contact_permission == "agent_will_introduce_first"
    assert lead.lead_intake["active_mca_count"] == active_count
    assert lead.lead_intake["attribution"]["gclid"] == "test-click-id"
    assert lead.lead_intake["consent_version"] == mca_leads.CONSENT_VERSION
    assert lead.lead_intake["consented_at"]
    assert lead.lead_intake["review_status"] == "pending_review"
    assert lead.lead_intake["allowed_contact_channels"] == ["phone", "email"]
    assert "idempotency_key" not in lead.lead_intake
    assert activity.client_id == lead.id
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_is_idempotent_and_cannot_overwrite_another_email():
    existing = SimpleNamespace(source_channel="mca_funding", email="owner@example.com")
    db = Mock(get=AsyncMock(return_value=existing), commit=AsyncMock())
    body = request_body()
    async with AsyncClient(transport=ASGITransport(app=application(db)), base_url="http://test") as client:
        response = await client.post("/api/v1/public/mca/leads", json=body)
        conflict = await client.post("/api/v1/public/mca/leads", json=body | {"email": "different@example.com"})
    assert response.status_code == 201
    assert conflict.status_code == 409
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_simultaneous_duplicate_recovers_only_after_a_saved_record_exists():
    existing = SimpleNamespace(source_channel="mca_funding", email="owner@example.com")
    db = Mock(get=AsyncMock(side_effect=[None, existing]), flush=AsyncMock(), commit=AsyncMock(side_effect=IntegrityError("duplicate", {}, None)), rollback=AsyncMock())
    with patch.object(mca_leads, "primary_super_admin", AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=application(db)), base_url="http://test") as client:
            response = await client.post("/api/v1/public/mca/leads", json=request_body())
    assert response.status_code == 201
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_throttled_request_does_not_create_another_lead():
    db = Mock(get=AsyncMock(return_value=None), flush=AsyncMock(), commit=AsyncMock())
    with patch.object(mca_leads, "primary_super_admin", AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=application(db)), base_url="http://test") as client:
            first = await client.post("/api/v1/public/mca/leads", json=request_body())
            second = await client.post("/api/v1/public/mca/leads", json=request_body())
    assert first.status_code == 201
    assert second.status_code == 429
    assert db.add.call_count == 2  # one Client and its audit event


@pytest.mark.asyncio
async def test_proxy_uses_trusted_final_forwarded_address_for_throttle():
    db = Mock(get=AsyncMock(return_value=None), flush=AsyncMock(), commit=AsyncMock())
    with patch.object(mca_leads, "primary_super_admin", AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=application(db)), base_url="http://test") as client:
            first = await client.post("/api/v1/public/mca/leads", json=request_body(), headers={"x-forwarded-for": "spoofed-address, 203.0.113.1"})
            second = await client.post("/api/v1/public/mca/leads", json=request_body(), headers={"x-forwarded-for": "spoofed-address, 203.0.113.2"})
            same_client = await client.post("/api/v1/public/mca/leads", json=request_body(), headers={"x-forwarded-for": "different-spoof, 203.0.113.1"})
    assert first.status_code == 201
    assert second.status_code == 201
    assert same_client.status_code == 429
    assert set(mca_leads._LAST_SUBMIT) == {"203.0.113.1", "203.0.113.2"}


def outreach_context():
    client = SimpleNamespace(id=uuid4(), name="Business Owner", email="owner@example.com", phone="+12125550123", source_channel="mca_funding", contact_permission="agent_will_introduce_first")
    user = SimpleNamespace(id=uuid4(), name="Funding Advisor", email="advisor@example.com", role=Role.SUPER_ADMIN)
    query_result = Mock()
    query_result.scalar_one_or_none.return_value = client
    db = Mock(execute=AsyncMock(return_value=query_result), flush=AsyncMock())
    return client, user, db


@pytest.mark.asyncio
async def test_operator_can_email_mca_lead_without_sms_or_an_outreach_enable_step():
    lead, user, db = outreach_context()
    mail = AsyncMock(return_value=SimpleNamespace(ok=True))
    with patch("app.services.email.user_mailer.send_as_user", mail), patch("app.services.sms.send_sms_checked", AsyncMock()) as sms:
        result = await clients.send_intake_link(lead.id, clients.SendIntakeLinkRequest(), user, db)
    assert result.sent_via == "email"
    assert mail.await_args.kwargs["to_emails"] == [lead.email]
    sms.assert_not_awaited()


@pytest.mark.asyncio
async def test_operator_cannot_use_phone_email_consent_to_text_mca_lead():
    lead, user, db = outreach_context()
    with patch("app.services.email.user_mailer.send_as_user", AsyncMock()) as mail, patch("app.services.sms.send_sms_checked", AsyncMock()) as sms:
        with pytest.raises(HTTPException) as error:
            await clients.send_intake_link(lead.id, clients.SendIntakeLinkRequest(channel="sms"), user, db)
    assert error.value.status_code == 400
    mail.assert_not_awaited()
    sms.assert_not_awaited()
