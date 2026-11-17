from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from app.db import get_db
from app.models.activity import Activity
from app.models.client import Client
from app.models.resource_assessment import ResourceAssessment
from app.routers import public_resources
from app.services import resource_assessments as service


def body(**changes):
    return {"idempotency_key": str(uuid4()), "resource_id": "business_funding_check", "goal": "term_loan",
        "locale": "en", "answers": {"requested_amount": 50000, "debts": None},
        "contact": {"name": "Example Owner", "business_name": "Example Company", "email": "Owner@Example.com", "phone": "2125550123"},
        "email_marketing_consent": True, "privacy_acknowledged": True,
        "attribution": {"page": "/business-funding-check", "utm_source": "google"}, **changes}


def scalar(value):
    return SimpleNamespace(scalar_one_or_none=lambda: value)


def request():
    return Request({"type": "http", "headers": [], "client": ("192.0.2.90", 1000)})


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    service._LAST_CAPTURE.clear()
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(resource_token_secret="fictional-test-resource-secret",
        clerk_secret_key="", provider_secrets_encryption_key=""))


def application(db):
    app = FastAPI()
    app.include_router(public_resources.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    return app


@pytest.mark.parametrize("change", [{"privacy_acknowledged": False}, {"goal": "dealer"},
    {"resource_id": "unknown"}, {"website": "spam"}, {"email_marketing_consent": "yes"},
    {"contact": {"name": "Owner", "business_name": "Company", "email": "bad", "phone": "1"}}])
async def test_invalid_capture_never_writes(change):
    db = Mock()
    async with AsyncClient(transport=ASGITransport(app=application(db)), base_url="http://test") as client:
        response = await client.post("/api/v1/public/resources/leads", json=body(**change))
    assert response.status_code == 422
    db.add.assert_not_called()


async def test_capture_persists_result_then_retries_same_idempotent_capability(monkeypatch):
    monkeypatch.setattr("app.services.payment_authorization.primary_super_admin", AsyncMock(return_value=None))
    db = Mock(get=AsyncMock(return_value=None), execute=AsyncMock(side_effect=[scalar(None), scalar(None), scalar(None)]),
              flush=AsyncMock(), commit=AsyncMock())
    payload = public_resources.ResourceLead.model_validate(body())
    result = await service.capture(db, payload, request())
    rows = [call.args[0] for call in db.add.call_args_list]
    assessment = next(row for row in rows if isinstance(row, ResourceAssessment))
    contact = next(row for row in rows if isinstance(row, Client))
    event = next(row for row in rows if isinstance(row, Activity))
    assert result["status"] == "received" and result["report"] == assessment.report
    assert assessment.client_id == contact.id and assessment.contact["email"] == "owner@example.com"
    assert assessment.answers["debts"] is None
    assert assessment.email_marketing_consent is True and event.payload["enrollment"] == "pending"
    assert result["resource_token"] not in str(assessment.contact) + str(event.payload)
    assert assessment.resource_token_hash == service.token_hash(result["resource_token"])
    assert timedelta(days=13) < assessment.token_expires_at - datetime.now(UTC) <= timedelta(days=14)
    db.get = AsyncMock(return_value=assessment)
    db.execute = AsyncMock(return_value=scalar(None))
    db.add.reset_mock()
    retry = await service.capture(db, payload, request())
    assert retry == result
    db.add.assert_not_called()
    changed = payload.model_copy(update={"goal": "merchant_cash_advance"})
    with pytest.raises(HTTPException) as error:
        await service.capture(db, changed, request())
    assert error.value.status_code == 409


async def test_known_client_is_reused_without_private_contact_or_ownership_overwrite(monkeypatch):
    known = Client(id=uuid4(), name="Private saved name", email="owner@example.com", phone="2025550140", current_agent_id=uuid4())
    owner = known.current_agent_id
    db = Mock(get=AsyncMock(return_value=None), execute=AsyncMock(side_effect=[scalar(None), scalar(None), scalar(known)]),
              flush=AsyncMock(), commit=AsyncMock())
    await service.capture(db, public_resources.ResourceLead.model_validate(body()), request())
    assert known.name == "Private saved name" and known.phone == "2025550140" and known.current_agent_id == owner
    assert not any(isinstance(call.args[0], Client) for call in db.add.call_args_list)


async def test_capability_scope_expiry_and_email_are_enforced():
    identifier = uuid4()
    token = service.token_for(identifier)
    row = ResourceAssessment(id=identifier, goal="term_loan", contact={"email": "owner@example.com"},
        resource_token_hash=service.token_hash(token), token_expires_at=datetime.now(UTC) + timedelta(days=1))
    db = Mock(get=AsyncMock(return_value=row))
    assert await service.resolve(db, token, goal="term_loan", email="OWNER@example.com") is row
    for changes in [{"goal": "mca_refinance"}, {"email": "someone@example.com"}]:
        with pytest.raises(HTTPException) as error:
            await service.resolve(db, token, **changes)
        assert error.value.status_code == 422
    with pytest.raises(HTTPException) as error:
        await service.resolve(db, token[:-1] + ("0" if token[-1] != "0" else "1"))
    assert error.value.status_code == 404
    row.token_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(HTTPException) as error:
        await service.resolve(db, token)
    assert error.value.status_code == 404


async def test_handoff_exposes_only_capture_data_and_report_retains_adjustments(monkeypatch):
    row = SimpleNamespace(goal="term_loan", locale="es", contact={"name": "Submitted"}, answers={}, attribution={},
                          assumptions={"term_months": 72}, report={})
    monkeypatch.setattr(service, "resolve", AsyncMock(return_value=row))
    db = Mock(commit=AsyncMock())
    async with AsyncClient(transport=ASGITransport(app=application(db)), base_url="http://test") as client:
        result = await client.post("/api/v1/public/resources/handoff", json={"resource_token": "a" * 97})
        assert set(result.json()) == {"goal", "locale", "contact", "answers", "attribution"}
        assert result.headers["cache-control"] == "no-store"
        report = await client.post("/api/v1/public/resources/report", json={"resource_token": "a" * 97})
    assert report.status_code == 200
    assert report.json()["report"]["assumptions"]["term_months"] == 72


async def test_actual_start_stops_every_pending_same_client_assessment():
    client_id = uuid4()
    first = ResourceAssessment(id=uuid4(), client_id=client_id, goal="term_loan", answers={}, report={}, attribution={})
    second = ResourceAssessment(id=uuid4(), client_id=client_id, goal="mca_refinance")
    db = Mock(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [first, second])))
    intake = SimpleNamespace(id=uuid4(), client_id=client_id, intake_state={})
    await service.link_application(db, first, intake)
    assert first.application_intake_id == second.application_intake_id == intake.id
    assert first.application_started_at == second.application_started_at
    assert intake.intake_state["resource_assessment"]["verification_status"] == "self_reported_unverified"
    assert len(db.add.call_args_list) == 2
