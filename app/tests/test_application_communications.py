from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.routers import application_communications as communications
from app.routers import application_profiles as profile_routes
from app.services import sms as sms_service
from app.services.email import user_inbox_sync


def _contracts() -> set[tuple[str, str]]:
    return {
        (route.path, method)
        for route in communications.router.routes
        for method in getattr(route, "methods", set())
    }


def test_application_communication_routes_are_registered() -> None:
    routes = _contracts()
    assert ("/application-profiles/{profile_id}/communications/contacts", "GET") in routes
    assert ("/application-profiles/{profile_id}/communications/sms-consent", "GET") in routes
    assert ("/application-profiles/{profile_id}/communications/sms-consent", "POST") in routes
    assert ("/application-profiles/{profile_id}/communications/email/threads", "GET") in routes
    assert ("/application-profiles/{profile_id}/communications/email/threads", "POST") in routes
    assert ("/application-profiles/{profile_id}/communications/email/threads/{thread_id}/messages", "POST") in routes
    assert ("/application-profiles/{profile_id}/communications/links", "POST") in routes


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Documents needed", "documents needed"),
        (" Re: RE:   Documents needed ", "documents needed"),
        ("Fwd: Re: Bank connection", "bank connection"),
    ],
)
def test_subject_keys_ignore_reply_prefixes(subject: str, expected: str) -> None:
    assert communications._subject_key(subject) == expected


@pytest.mark.asyncio
async def test_private_credit_link_requires_matching_sole_recipient() -> None:
    profile_id = uuid4()
    profile = SimpleNamespace(id=profile_id)
    token = "app.private-token"
    owner = SimpleNamespace(email="owner@example.com")
    result = SimpleNamespace(scalar_one_or_none=lambda: owner)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    await communications._enforce_private_credit_recipient(
        db,
        profile,
        body=f"Please complete this: https://app.test/credit-consent#t={token}",
        to_email="owner@example.com",
        cc_emails=[],
    )

    statement = db.execute.await_args.args[0]
    assert profile_routes._hash_token(token) in str(statement.compile(compile_kwargs={"literal_binds": True}))

    with pytest.raises(HTTPException) as exc:
        await communications._enforce_private_credit_recipient(
            db,
            profile,
            body=f"https://app.test/credit-consent#t={token}",
            to_email="someone-else@example.com",
            cc_emails=[],
        )
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as exc:
        await communications._enforce_private_credit_recipient(
            db,
            profile,
            body=f"https://app.test/credit-consent#t={token}",
            to_email="owner@example.com",
            cc_emails=["other-owner@example.com"],
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_non_credit_email_has_no_recipient_lock() -> None:
    db = AsyncMock()
    await communications._enforce_private_credit_recipient(
        db,
        SimpleNamespace(id=uuid4()),
        body="Here is the updated application status.",
        to_email="client@example.com",
        cc_emails=["owner@example.com"],
    )
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_inbound_gmail_reply_prefers_profile_provider_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    thread = SimpleNamespace(provider_thread_id=None, participant_emails=["client@example.com"])
    scalar_result = SimpleNamespace(first=lambda: thread)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(scalars=lambda: scalar_result))
    append = AsyncMock()
    monkeypatch.setattr("app.dealer_os.router._append_rep_inbox_message", append)
    owner_user_id = uuid4()

    await user_inbox_sync._mirror_file_email_reply(
        db,
        from_email="client@example.com",
        mailbox="underwriter@example.com",
        subject="Re: Documents needed",
        body="Attached.",
        gmail_id="gmail-message-1",
        gmail_thread_id="gmail-thread-1",
        owner_user_id=owner_user_id,
    )

    assert thread.provider_thread_id == "gmail-thread-1"
    append.assert_awaited_once()
    assert append.await_args.kwargs["thread"] is thread
    assert append.await_args.kwargs["direction"] == "inbound"


@pytest.mark.asyncio
async def test_sms_ledger_keeps_profile_and_portal_linkage(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.sms import ledger

    recorded = AsyncMock()
    monkeypatch.setattr(ledger, "record", recorded)
    monkeypatch.setattr(sms_service, "_provider", lambda: SimpleNamespace(selected_provider=lambda: "test"))
    monkeypatch.setattr(sms_service, "is_opted_out", AsyncMock(return_value=False))
    monkeypatch.setattr(
        sms_service,
        "send_sms",
        lambda _phone, _body: sms_service.SmsResult(True, "test", "provider-1", "accepted"),
    )
    profile_id = uuid4()
    intake_id = uuid4()
    portal_message_id = uuid4()

    result = await sms_service.send_sms_checked(
        SimpleNamespace(),
        to_phone="212-555-0199",
        body="Your underwriter replied.",
        profile_id=profile_id,
        intake_id=intake_id,
        portal_message_id=portal_message_id,
        context="intake_client_reply",
    )

    assert result.ok is True
    assert recorded.await_args.kwargs["profile_id"] == profile_id
    assert recorded.await_args.kwargs["intake_id"] == intake_id
    assert recorded.await_args.kwargs["portal_message_id"] == portal_message_id
