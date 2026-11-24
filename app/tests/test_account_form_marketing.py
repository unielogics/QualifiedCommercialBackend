from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.enums import Role
from app.routers.prequal import _create_request
from app.schemas.prequal import PrequalRequestCreate
from app.services import email_marketing
from app.services.signup_marketing import enroll_verified_signup


def borrower(role=Role.CLIENT):
    return SimpleNamespace(
        id=uuid4(), role=role, email="Owner@example.test",
        client=SimpleNamespace(id=uuid4()), ui_locale="es",
    )


def clerk_profile(choice=True, original="OWNER@example.test", verified=True, primary="main"):
    return {
        "unsafe_metadata": {"qc_lending_updates_v1": choice, "qc_lending_updates_email_v1": original},
        "primary_email_address_id": primary,
        "email_addresses": [
            {"id": "main", "email_address": "owner@example.test", "verification": {"status": "verified" if verified else "unverified"}},
            {"id": "secondary", "email_address": "other@example.test", "verification": {"status": "verified"}},
        ],
    }


@pytest.fixture
def enroll(monkeypatch):
    recorder = AsyncMock()
    monkeypatch.setattr(email_marketing, "enroll", recorder)
    return recorder


@pytest.mark.asyncio
async def test_signup_uses_verified_account_and_optional_grant(enroll):
    user = borrower()
    db = object()
    await enroll_verified_signup(db, user, clerk_profile())
    enroll.assert_awaited_once_with(
        db, email=user.email, consent=True, source="account_signup", outcome="account_created",
        client_id=user.client.id, locale="es",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("profile", "role"), [
    (None, Role.CLIENT), ({}, Role.CLIENT), (clerk_profile(choice=False), Role.CLIENT),
    (clerk_profile(choice="true"), Role.CLIENT), (clerk_profile(verified=False), Role.CLIENT),
    (clerk_profile(primary="secondary"), Role.CLIENT), (clerk_profile(original="previous@example.test"), Role.CLIENT),
    (clerk_profile(original=None), Role.CLIENT), (clerk_profile(), Role.BROKER),
])
async def test_signup_never_converts_historical_or_unverified_contacts(enroll, profile, role):
    await enroll_verified_signup(object(), borrower(role), profile)
    enroll.assert_not_awaited()


def prequal_payload(**extra):
    return PrequalRequestCreate(
        target_property_address="12 Example Ave", purchase_price=250000, requested_loan_amount=150000,
        loan_type="bridge", **extra,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("role", "fields", "expected"), [
    (Role.CLIENT, {}, False), (Role.CLIENT, {"email_marketing_consent": True}, True),
    (Role.BROKER, {"email_marketing_consent": True}, False),
])
async def test_prequal_only_enrolls_the_authenticated_borrower(enroll, role, fields, expected):
    user = borrower(role)
    db = SimpleNamespace(add=MagicMock(), flush=AsyncMock(), refresh=AsyncMock())
    await _create_request(None, prequal_payload(**fields), user, db)
    if expected:
        enroll.assert_awaited_once_with(
            db, email=user.email, consent=True, source="borrower_prequalification", program="bridge",
            outcome="prequalification_requested", client_id=user.client.id, locale="es",
        )
    else:
        enroll.assert_not_awaited()


def test_prequal_rejects_string_as_consent():
    with pytest.raises(ValidationError):
        prequal_payload(email_marketing_consent="true")
