from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.db import get_db
from app.models.client import Client
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.routers import application_profiles as rooms
from app.routers import dealer_ai_intake as core
from app.routers import mca_leads
from app.services import mca_public_intake as service
from app.tests._business_profile_fixture import APPLICATION_START_DETAILS, BUSINESS_PROFILE
from app.tests.test_mca_leads import request_body


def body(**changes):
    return {**request_body(), **BUSINESS_PROFILE, **APPLICATION_START_DETAILS, "secure_room_pin": "627493", "terms_accepted": True, "privacy_accepted": True,
            "consent_version": "mca-ai-intake-v3", **changes}


def scalar(value):
    return SimpleNamespace(scalar_one_or_none=lambda: value)


def request():
    from starlette.requests import Request

    return Request({"type": "http", "headers": [], "client": ("198.51.100.15", 1000)})


@pytest.fixture(autouse=True)
def reset_limits():
    service._STARTS.clear()
    service._RESUMES.clear()
    service._CHATS.clear()


def application(db):
    app = FastAPI()
    app.include_router(mca_leads.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    return app


@pytest.mark.parametrize("changes", [{"active_mca_count": 3}, {"has_modified_mca": True},
    {"last_year_tax_earnings": "negative"}, {"terms_accepted": False}, {"privacy_accepted": False},
    {"secure_room_pin": "12345"}, {"has_modified_mca": "false"}, {"active_mca_count": True}])
async def test_invalid_or_known_ineligible_start_cannot_write(changes):
    db = Mock()
    async with AsyncClient(transport=ASGITransport(app=application(db)), base_url="http://test") as client:
        response = await client.post("/api/v1/public/mca/intake/start", json=body(**changes))
    assert response.status_code == 422
    db.execute.assert_not_called()
    db.add.assert_not_called()


async def test_unknowns_are_accepted_for_document_review_not_preapproved(monkeypatch):
    start = AsyncMock(return_value={"intake_id": uuid4(), "room_url": "https://app.example/buckets/request/secret?tab=ai-review",
                                  "resume_url": "https://app.example/buckets/request/secret?tab=ai-review"})
    monkeypatch.setattr(service, "start", start)
    db = Mock()
    async with AsyncClient(transport=ASGITransport(app=application(db)), base_url="http://test") as client:
        response = await client.post("/api/v1/public/mca/intake/start", json=body(active_mca_count=None,
            has_modified_mca=None, last_year_tax_earnings="unknown"))
    assert response.status_code == 201
    assert response.json()["funding_intent"] == "merchant_cash_advance"
    assert "token" not in response.json() and "secure_room_pin" not in response.json()
    payload = start.await_args.args[1]
    assert payload.active_mca_count is None and payload.has_modified_mca is None
    assert payload.last_year_tax_earnings == "unknown"


async def test_actual_start_constructs_one_main_street_file_and_defers_full_application(monkeypatch):
    payload = mca_leads.McaIntakeStart.model_validate(body(active_mca_count=None, has_modified_mca=None, last_year_tax_earnings="unknown"))
    bucket = SimpleNamespace(id=uuid4())
    link = SimpleNamespace(id=uuid4(), token="room-capability", passcode_set_by_client_at=None)
    builder = AsyncMock(return_value=(bucket, link))
    monkeypatch.setattr(core, "_create_bucket_for_main_street", builder)
    monkeypatch.setattr(core.profiles_service, "provision_profile_for_intake", AsyncMock())
    monkeypatch.setattr("app.services.payment_authorization.primary_super_admin", AsyncMock(return_value=None))
    mail = Mock()
    monkeypatch.setattr(core, "send_email", mail)
    db = Mock(get=AsyncMock(side_effect=[None, None]), execute=AsyncMock(side_effect=[scalar(None), scalar(None)]),
              flush=AsyncMock(), commit=AsyncMock())
    record_consent = AsyncMock()
    monkeypatch.setattr("app.services.onboarding_consent.record_for_start", record_consent)
    result = await service.start(db, payload, request())
    saved = [call.args[0] for call in db.add.call_args_list]
    identity = next(row for row in saved if isinstance(row, Client))
    intake = next(row for row in saved if isinstance(row, PublicUnderwritingIntake))
    record_consent.assert_awaited_once()
    assert record_consent.await_args.args[:3] == (db, intake, payload)
    assert record_consent.await_args.kwargs == {"entry": "mca_funding"}
    assert intake.client_id == identity.id
    assert intake.variant == "main_street_v1" and intake.intake_state["funding_intent"] == "merchant_cash_advance"
    assert intake.intake_state["application_completion"] == "client"
    assert intake.intake_state["mca_product_policy"]["verification_status"] == "pending_document_review"
    assert intake.intake_state["mca_funding_declaration"]["active_mca_count"] is None
    assert intake.intake_state["legal_acceptance"]["credit_pull_authorized"] is False
    assert intake.intake_state["main_street_details"]["industry"] is None
    assert "secure_room_pin" not in str(intake.intake_state)
    assert builder.await_args.kwargs == {"intent": "merchant_cash_advance", "industry": "other", "room_pin": payload.secure_room_pin}
    assert result["room_url"].endswith("/buckets/request/room-capability?tab=ai-review")
    assert link.passcode_set_by_client_at is not None
    mail.assert_not_called()


async def test_existing_email_cannot_adopt_or_duplicate_a_client(monkeypatch):
    payload = mca_leads.McaIntakeStart.model_validate(body())
    db = Mock(get=AsyncMock(side_effect=[None, None]), execute=AsyncMock(side_effect=[scalar(None), scalar(uuid4())]))
    with pytest.raises(HTTPException) as error:
        await service.start(db, payload, request())
    assert error.value.status_code == 409
    db.add.assert_not_called()
    assert "lower(clients.email)" in str(db.execute.await_args_list[-1].args[0])


@pytest.mark.parametrize("pin_ok,changed,expected", [(True, False, None), (False, False, 403), (True, True, 409)])
async def test_retry_returns_existing_room_only_for_exact_answers_and_valid_pin(monkeypatch, pin_ok, changed, expected):
    payload = mca_leads.McaIntakeStart.model_validate(body())
    link = SimpleNamespace(id=uuid4(), token="saved-room", passcode_hash="hash", status="active")
    row = SimpleNamespace(id=service.intake_id(payload.idempotency_key), variant="main_street_v1", bucket_upload_link=link,
        intake_state={"funding_intent": "merchant_cash_advance", "mca_public_request_stamp": "changed" if changed else service.request_stamp(payload)})
    monkeypatch.setattr(core, "_load_admin_dealer_lead", AsyncMock(return_value=row))
    monkeypatch.setattr("app.routers.buckets._verify_passcode", Mock(return_value=pin_ok))
    db = Mock(get=AsyncMock(return_value=row), execute=AsyncMock())
    if expected:
        with pytest.raises(HTTPException) as error:
            await service.start(db, payload, request())
        assert error.value.status_code == expected
    else:
        result = await service.start(db, payload, request())
        assert result["room_url"].endswith("/saved-room?tab=ai-review")
    db.add.assert_not_called()


@pytest.mark.parametrize("kind", ["wrong_product", "collaborator", "wrong_bucket", "disabled"])
async def test_pin_alone_never_crosses_product_or_client_room_scope(monkeypatch, kind):
    link = SimpleNamespace(id=uuid4(), bucket_id=uuid4(), can_use_ai_chat=kind != "disabled")
    profile = SimpleNamespace(intake_id=uuid4(), funding_category="merchant_cash_advance")
    intake = SimpleNamespace(variant="main_street_v1", bucket_id=link.bucket_id,
        bucket_upload_link_id=link.id, intake_state={"funding_intent": "merchant_cash_advance"})
    if kind == "wrong_product":
        intake.intake_state["funding_intent"] = "equipment"
    elif kind == "collaborator":
        intake.bucket_upload_link_id = uuid4()
    elif kind == "wrong_bucket":
        intake.bucket_id = uuid4()
    monkeypatch.setattr(rooms, "_public_application_room", AsyncMock(return_value=(link, profile)))
    monkeypatch.setattr(core, "_load_admin_dealer_lead", AsyncMock(return_value=intake))
    with pytest.raises(HTTPException) as error:
        await service.authorize_room(Mock(), "room", "627493", request())
    assert error.value.status_code in {403, 404}


async def test_room_state_keeps_internal_review_and_provider_details_private(monkeypatch):
    from app.services import application_programs

    requirement = SimpleNamespace(requirement_key="business_bank_statements_6_months", label="Six months statements",
        status="received_unverified", coverage={"current": 4, "required": 6, "unit": "months"})
    monkeypatch.setattr(application_programs, "get_program_readiness", AsyncMock(return_value=SimpleNamespace(requirements=[requirement])))
    monkeypatch.setattr(application_programs, "profile_fit_context", AsyncMock(return_value={"mca_funding_policy": {"recommendation_status": "needs_information"}, "private_dscr": 99}))
    review = SimpleNamespace(status="error", progress={"stage": "error", "label": "private provider secret", "internal": "secret"},
                             result={"private_dscr": 99, "reviewer_email": "private@example.test"})
    db = SimpleNamespace(execute=AsyncMock(return_value=scalar(review)), scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [])))
    result = await service.room_state(db, SimpleNamespace(business_name="Example"), SimpleNamespace(id=uuid4(), bucket_id=uuid4()), SimpleNamespace())
    assert result["status"] == "failed" and result["requirements"][0]["received"] == 4
    assert result["requirements"][0]["required"] == 6 and result["requirements"][0]["unit"] == "months"
    assert "secret" not in str(result) and "private_dscr" not in str(result) and "reviewer_email" not in str(result)
    assert result["policy_status"] == "needs_information"


async def test_room_state_preserves_human_speaker_locale_and_exact_profile_payment_scope(monkeypatch):
    from app.services import application_programs, mca_room_capabilities

    profile = SimpleNamespace(id=uuid4(), communication_locale="es")
    allowed = AsyncMock(return_value=True)
    monkeypatch.setattr(mca_room_capabilities, "payment_methods_available", allowed)
    monkeypatch.setattr(application_programs, "get_program_readiness", AsyncMock(return_value=SimpleNamespace(requirements=[])))
    monkeypatch.setattr(application_programs, "profile_fit_context", AsyncMock(return_value={}))
    message = SimpleNamespace(id=uuid4(), role="user", sender_kind="operator", author_name="Funding advisor",
        content="We are checking the statement dates.", created_at=datetime.now(UTC), metadata_json={"secret": "private"})
    db = SimpleNamespace(execute=AsyncMock(return_value=scalar(None)),
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [message])))
    result = await service.room_state(db, SimpleNamespace(business_name="Example", intake_state={}),
        SimpleNamespace(id=uuid4(), bucket_id=uuid4()), profile)
    allowed.assert_awaited_once_with(db, profile.id)
    assert result["preferred_language"] == "es" and result["payment_methods_enabled"] is True
    assert result["messages"][0]["sender_kind"] == "operator" and result["messages"][0]["author_name"] == "Funding advisor"
    assert "metadata_json" not in str(result)
    assert mca_leads.McaReviewState.model_validate(result).summary is not None
    assert all(row["kind"] != "application" for row in result["actions"])


@pytest.mark.parametrize("existing,fails", [(False, False), (True, False), (True, True)])
async def test_resume_response_is_generic_and_never_emails_pin(monkeypatch, existing, fails):
    row = SimpleNamespace(id=uuid4(), email="owner@example.com", intake_state={}, bucket_upload_link=SimpleNamespace(
        token="room-only", recipient_email="owner@example.com", status="active"))
    db = SimpleNamespace(execute=AsyncMock(side_effect=[scalar(None), scalar(row if existing else None)]), commit=AsyncMock(), rollback=AsyncMock())
    deliver = AsyncMock(side_effect=RuntimeError("private delivery failure") if fails else None)
    monkeypatch.setattr(core, "_record_resume_email", deliver)
    result = await service.resume(db, "OWNER@example.com", request())
    assert result == {"status": "received", "message": "If a matching MCA file is available, a resume link will be emailed to its saved recipient. Your PIN is not included."}
    if existing:
        assert deliver.await_args.kwargs["token"] == ""
        assert "pin" not in deliver.await_args.kwargs
    else:
        deliver.assert_not_awaited()


async def test_blank_chat_rejected_before_any_database_or_model_call():
    db = Mock()
    async with AsyncClient(transport=ASGITransport(app=application(db)), base_url="http://test") as client:
        response = await client.post("/api/v1/public/mca/room/room/chat", json={"passcode": "627493", "message": "  "})
    assert response.status_code == 422
    db.execute.assert_not_called()


async def test_native_bucket_has_exact_three_keyed_requests_and_product_context(monkeypatch):
    from app.models.bucket import BucketRequestedDocument

    payload = mca_leads.McaIntakeStart.model_validate(body())
    saved = []
    async def flush():
        for row in saved:
            if getattr(row, "id", None) is None:
                row.id = uuid4()
    db = SimpleNamespace(add=saved.append, flush=flush)
    monkeypatch.setattr(core, "primary_super_admin", AsyncMock(return_value=None))
    monkeypatch.setattr(core, "_log", AsyncMock())
    monkeypatch.setattr(core.client_room, "_store_passcode", Mock())
    bucket, _link = await core._create_bucket_for_main_street(db, SimpleNamespace(email=payload.email), payload,
        request(), intent="merchant_cash_advance", industry="other", room_pin=payload.secure_room_pin)
    requests = [row for row in saved if isinstance(row, BucketRequestedDocument)]
    assert len(requests) == 3
    assert {row.requirement_key for row in requests} == {"business_bank_statements_6_months", "business_debt_schedule", "business_tax_returns_1_years"}
    assert bucket.ai_context["funding_intent"] == "merchant_cash_advance"
    assert len(bucket.ai_context["stage_1_required_items"]) == 3
    assert bucket.ai_context["baseline_document_policy"]["conditional_after_industry_known"] == []


def test_mca_context_chat_and_resume_route_cannot_fall_back_to_generic_lending():
    row = PublicUnderwritingIntake(variant="main_street_v1", full_name="Owner", business_name="Example LLC",
        preferred_language="en", intake_state={"funding_intent": "merchant_cash_advance",
            "main_street_details": {"intent": "merchant_cash_advance", "industry": "other"}})
    context = core._main_street_context(row)
    assert len(context["baseline_document_policy"]["allowed_document_categories"]) == 3
    assert context["funding_program_key"] == "merchant_cash_advance"
    assert core._dealer_route_public_path(row) == "/mca-funding-intake"
    core._merge_main_street_details(row, {"intent": "working_capital"})
    assert row.intake_state["main_street_details"]["intent"] == "merchant_cash_advance"


async def test_resume_email_contains_only_room_url_and_escapes_public_name(monkeypatch):
    row = SimpleNamespace(id=uuid4(), full_name='<a href="https://bad.test">Name</a>', email="owner@example.test",
        variant="main_street_v1", intake_state={"funding_intent": "merchant_cash_advance"},
        bucket_upload_link=SimpleNamespace(token="only-room-token", status="active"))
    send = Mock(return_value=SimpleNamespace(ok=True, detail="sent", message_id="mocked"))
    monkeypatch.setattr(core, "send_email", send)
    await core._record_resume_email(row, token="never-leak-intake-token", request=request(), reason="test")
    kwargs = send.call_args.kwargs
    assert "/buckets/request/only-room-token?tab=ai-review" in kwargs["body_text"]
    assert "never-leak-intake-token" not in str(kwargs) and "627493" not in str(kwargs)
    assert '<a href="https://bad.test">' not in kwargs["body_html"]
    assert "&lt;a href=" in kwargs["body_html"]


async def test_durable_resume_claim_prevents_duplicate_delivery_across_processes(monkeypatch):
    row = SimpleNamespace(id=uuid4(), email="owner@example.com", intake_state={}, bucket_upload_link=SimpleNamespace(
        token="room", recipient_email="owner@example.com", status="active"))
    db = SimpleNamespace(execute=AsyncMock(side_effect=[scalar(None), scalar(row), scalar(None), scalar(row)]),
        commit=AsyncMock(), rollback=AsyncMock())
    deliver = AsyncMock()
    monkeypatch.setattr(core, "_record_resume_email", deliver)
    await service.resume(db, "owner@example.com", request())
    service._RESUMES.clear()  # another process has no in-memory rate history
    await service.resume(db, "owner@example.com", request())
    deliver.assert_awaited_once()


async def test_no_duplicate_heavy_review_is_queued_for_an_inflight_job(monkeypatch):
    link = SimpleNamespace(id=uuid4(), bucket_id=uuid4())
    profile = SimpleNamespace()
    intake = SimpleNamespace(id=uuid4())
    monkeypatch.setattr(service, "authorize_room", AsyncMock(return_value=(intake, link, profile)))
    monkeypatch.setattr(service, "room_state", AsyncMock(return_value={"status": "queued",
        "progress": {"stage": "queued", "label": "Preparing review"}, "policy_status": "needs_information"}))
    create = AsyncMock()
    monkeypatch.setattr(core, "_create_queued_review", create)
    db = SimpleNamespace(execute=AsyncMock(side_effect=[scalar(None), scalar(SimpleNamespace(id=uuid4()))]))
    async with AsyncClient(transport=ASGITransport(app=application(db)), base_url="http://test") as client:
        response = await client.post("/api/v1/public/mca/room/room/run", json={"passcode": "627493"})
    assert response.status_code == 200 and response.json()["status"] == "queued"
    create.assert_not_awaited()


@pytest.mark.parametrize("entry", ["/industries/main-street", "/es/medical-practice-financing", "/", "/es/", "/mca-funding-intake"])
def test_new_intake_keeps_safe_origin_and_allows_unknown_context_without_changing_old_leads(entry):
    values = body(monthly_revenue=None, time_in_business=None, attribution={"page": entry, "entry_page": entry,
        "campaign": "main_street_route_check", "cta": "new_mca_file"})
    payload = mca_leads.McaIntakeStart.model_validate(values)
    assert payload.monthly_revenue is None and payload.time_in_business is None
    assert payload.attribution.vertical == "main_street" and payload.attribution.program == "merchant_cash_advance"
    assert payload.attribution.profile == "main_street_v1"
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        mca_leads.McaLeadRequest.model_validate(request_body(monthly_revenue=None))


@pytest.mark.parametrize("changes", [{"page": "https://bad.test"}, {"entry_page": "/merchant-cash-advance?email=private"},
    {"entry_page": "/industries/main-street#pin=111111"}, {"funding_intent": "mca_refinance"},
    {"program": "equipment_financing"}, {"vertical": "dealer"}, {"profile": "mca_refi_v1"}])
def test_intake_attribution_cannot_redirect_product_or_store_unsafe_paths(changes):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        mca_leads.McaIntakeStart.model_validate(body(attribution={"page": "/merchant-cash-advance", **changes}))
