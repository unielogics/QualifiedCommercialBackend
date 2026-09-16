from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
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
    assert ("/application-profiles/{profile_id}/communications/sms-preference", "PATCH") in routes
    assert ("/application-profiles/{profile_id}/communications/email/threads", "GET") in routes
    assert ("/application-profiles/{profile_id}/communications/email/threads", "POST") in routes
    assert ("/application-profiles/{profile_id}/communications/email/attachments", "GET") in routes
    assert (
        "/application-profiles/{profile_id}/communications/email/threads/{thread_id}/messages",
        "POST",
    ) in routes
    assert ("/application-profiles/{profile_id}/communications/links", "POST") in routes


def test_email_attachment_refs_are_typed_and_versioned() -> None:
    offer_id = uuid4()
    sheet_id = uuid4()
    file_id = uuid4()

    payload = communications.ApplicationEmailCreate.model_validate(
        {
            "to_contact_id": "client:1",
            "subject": "Your reviewed options",
            "body": "Please review the attached documents.",
            "attachments": [
                {
                    "kind": "merchant_offer",
                    "offer_id": str(offer_id),
                    "expected_version": 2,
                },
                {
                    "kind": "production_term_sheet",
                    "term_sheet_id": str(sheet_id),
                    "expected_version": 5,
                },
                {"kind": "evidence_file", "file_id": str(file_id)},
            ],
        }
    )

    assert isinstance(payload.attachments[0], communications.MerchantOfferEmailAttachmentRef)
    assert payload.attachments[0].offer_id == offer_id
    assert isinstance(payload.attachments[1], communications.ProductionTermSheetEmailAttachmentRef)
    assert payload.attachments[1].term_sheet_id == sheet_id
    assert isinstance(payload.attachments[2], communications.EvidenceFileEmailAttachmentRef)
    assert payload.attachments[2].file_id == file_id


@pytest.mark.asyncio
async def test_direct_contact_suppression_blocks_email_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = SimpleNamespace(id=uuid4())
    monkeypatch.setattr(
        communications.file_contacts,
        "load_sources",
        AsyncMock(
            return_value=SimpleNamespace(intake=SimpleNamespace(client_contact_suppressed=True))
        ),
    )

    with pytest.raises(HTTPException) as exc:
        await communications._enforce_direct_client_contact(SimpleNamespace(), profile)

    assert exc.value.status_code == 409
    assert "suppressed" in str(exc.value.detail).lower()


def _attachment_sources() -> SimpleNamespace:
    return SimpleNamespace(
        intake=SimpleNamespace(business_name="Mora Market"),
        dealer=None,
        client=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "offer_status",
    [
        communications.merchant_offers.STATUS_EXTRACTED,
        communications.merchant_offers.STATUS_SENT,
        communications.merchant_offers.STATUS_ACCEPTED,
        communications.merchant_offers.STATUS_DECLINED,
    ],
)
async def test_ready_merchant_offer_resolves_to_new_client_safe_pdf(
    monkeypatch: pytest.MonkeyPatch,
    offer_status: str,
) -> None:
    offer_id = uuid4()
    offer = SimpleNamespace(
        id=offer_id,
        profile_id=uuid4(),
        status=offer_status,
        terms_version=3,
        lender_id=None,
        terms={"provider_name": "Acme Processing"},
    )
    profile = SimpleNamespace(id=offer.profile_id)
    db = SimpleNamespace(get=AsyncMock(return_value=offer))
    monkeypatch.setattr(
        communications.profiles,
        "evidence_state",
        AsyncMock(return_value=SimpleNamespace(files=[])),
    )
    monkeypatch.setattr(
        communications, "_raw_merchant_offer_file_ids", AsyncMock(return_value=set())
    )
    monkeypatch.setattr(
        communications.file_contacts, "load_sources", AsyncMock(return_value=_attachment_sources())
    )
    monkeypatch.setattr(
        communications.merchant_offers, "current_offer", AsyncMock(return_value=offer)
    )
    rendered = Mock(return_value=b"%PDF-client-safe")
    monkeypatch.setattr(communications, "render_merchant_offer_pdf", rendered)

    attachments, manifest = await communications._resolve_email_attachments(
        db,
        profile=profile,
        refs=[
            communications.MerchantOfferEmailAttachmentRef(
                kind="merchant_offer",
                offer_id=offer_id,
                expected_version=3,
            )
        ],
    )

    assert attachments == [
        (
            "Mora-Market-Merchant-Processing-Offer-v3.pdf",
            b"%PDF-client-safe",
            "application/pdf",
        )
    ]
    assert manifest == [
        {
            "kind": "merchant_offer",
            "source_id": str(offer_id),
            "version": 3,
            "file_name": "Mora-Market-Merchant-Processing-Offer-v3.pdf",
            "content_type": "application/pdf",
            "size_bytes": len(b"%PDF-client-safe"),
            "sha256": hashlib.sha256(b"%PDF-client-safe").hexdigest(),
        }
    ]
    assert db.get.await_args.kwargs["with_for_update"] is True
    assert rendered.call_args.kwargs == {
        "business_name": "Mora Market",
        "partner_name": "Acme Processing",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "offer_status",
    [
        communications.merchant_offers.STATUS_UPLOADED,
        communications.merchant_offers.STATUS_UNREADABLE,
        communications.merchant_offers.STATUS_WITHDRAWN,
        communications.merchant_offers.STATUS_SUPERSEDED,
    ],
)
async def test_unready_merchant_offer_cannot_be_attached(
    monkeypatch: pytest.MonkeyPatch,
    offer_status: str,
) -> None:
    offer_id = uuid4()
    offer = SimpleNamespace(
        id=offer_id,
        status=offer_status,
        terms_version=1,
        lender_id=None,
        terms={},
    )
    profile = SimpleNamespace(id=uuid4())
    db = SimpleNamespace(get=AsyncMock(return_value=offer))
    monkeypatch.setattr(
        communications.profiles,
        "evidence_state",
        AsyncMock(return_value=SimpleNamespace(files=[])),
    )
    monkeypatch.setattr(
        communications, "_raw_merchant_offer_file_ids", AsyncMock(return_value=set())
    )
    monkeypatch.setattr(
        communications.file_contacts, "load_sources", AsyncMock(return_value=_attachment_sources())
    )
    monkeypatch.setattr(
        communications.merchant_offers, "current_offer", AsyncMock(return_value=offer)
    )
    render = Mock()
    monkeypatch.setattr(communications, "render_merchant_offer_pdf", render)

    with pytest.raises(HTTPException) as exc:
        await communications._resolve_email_attachments(
            db,
            profile=profile,
            refs=[
                communications.MerchantOfferEmailAttachmentRef(
                    kind="merchant_offer",
                    offer_id=offer_id,
                    expected_version=1,
                )
            ],
        )

    assert exc.value.status_code == 409
    render.assert_not_called()


@pytest.mark.asyncio
async def test_raw_merchant_source_is_rejected_without_reading_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_id = uuid4()
    profile = SimpleNamespace(id=uuid4())
    file = SimpleNamespace(
        id=file_id,
        file_name="partner-source.pdf",
        s3_key="private/source.pdf",
        source_detail=communications.merchant_offers.OFFER_SOURCE_DETAIL,
        status="uploaded",
        deleted_at=None,
        size_bytes=200,
        content_type="application/pdf",
    )
    db = SimpleNamespace(get=AsyncMock(return_value=file))
    monkeypatch.setattr(
        communications.profiles,
        "evidence_state",
        AsyncMock(return_value=SimpleNamespace(files=[SimpleNamespace(id=file_id)])),
    )
    monkeypatch.setattr(
        communications,
        "_raw_merchant_offer_file_ids",
        AsyncMock(return_value={file_id}),
    )
    monkeypatch.setattr(
        communications.file_contacts, "load_sources", AsyncMock(return_value=_attachment_sources())
    )
    storage_read = Mock()
    monkeypatch.setattr(communications.dealer_storage, "get_bytes", storage_read)

    with pytest.raises(HTTPException) as exc:
        await communications._resolve_email_attachments(
            db,
            profile=profile,
            refs=[
                communications.EvidenceFileEmailAttachmentRef(
                    kind="evidence_file",
                    file_id=file_id,
                )
            ],
        )

    assert exc.value.status_code == 422
    assert "internal" in str(exc.value.detail).lower()
    storage_read.assert_not_called()


@pytest.mark.asyncio
async def test_attachment_options_omit_raw_offer_and_oversized_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_id = uuid4()
    large_id = uuid4()
    profile = SimpleNamespace(id=uuid4())
    raw = SimpleNamespace(
        id=raw_id,
        file_name="partner-source.pdf",
        source_detail=communications.merchant_offers.OFFER_SOURCE_DETAIL,
        status="uploaded",
        deleted_at=None,
        size_bytes=100,
        content_type="application/pdf",
    )
    large = SimpleNamespace(
        id=large_id,
        file_name="too-large.pdf",
        source_detail="Bank statement",
        status="uploaded",
        deleted_at=None,
        size_bytes=communications.MAX_EMAIL_ATTACHMENT_BYTES + 1,
        content_type="application/pdf",
    )
    files = {raw_id: raw, large_id: large}
    db = SimpleNamespace(get=AsyncMock(side_effect=lambda _model, file_id: files[file_id]))
    monkeypatch.setattr(
        communications,
        "_direct_client_contact_suppressed",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        communications.file_contacts, "load_sources", AsyncMock(return_value=_attachment_sources())
    )
    monkeypatch.setattr(
        communications.merchant_offers, "current_offer", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        communications.production_term_sheets,
        "current_sheet",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        communications.profiles,
        "evidence_state",
        AsyncMock(
            return_value=SimpleNamespace(
                files=[SimpleNamespace(id=raw_id), SimpleNamespace(id=large_id)]
            )
        ),
    )
    monkeypatch.setattr(
        communications,
        "_raw_merchant_offer_file_ids",
        AsyncMock(return_value={raw_id}),
    )

    result = await communications._email_attachment_options(db, profile)

    assert result.options == []


@pytest.mark.asyncio
async def test_suppression_blocks_create_and_reply_before_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = SimpleNamespace(id=uuid4())
    user = SimpleNamespace(id=uuid4(), email="underwriter@example.com")
    db = SimpleNamespace()
    monkeypatch.setattr(communications, "_load_profile", AsyncMock(return_value=profile))
    blocked = AsyncMock(
        side_effect=HTTPException(
            409,
            communications.DIRECT_CONTACT_SUPPRESSION_REASON,
        )
    )
    monkeypatch.setattr(communications, "_enforce_direct_client_contact", blocked)
    send = AsyncMock()
    monkeypatch.setattr(communications, "_send_thread_email", send)

    with pytest.raises(HTTPException) as create_exc:
        await communications.create_application_email_thread(
            profile.id,
            communications.ApplicationEmailCreate(
                to_contact_id="client:1",
                subject="Your offer",
                body="Please review.",
            ),
            SimpleNamespace(),
            user,
            db,
        )
    with pytest.raises(HTTPException) as reply_exc:
        await communications.reply_application_email_thread(
            profile.id,
            uuid4(),
            communications.ApplicationEmailReply(body="Following up."),
            SimpleNamespace(),
            user,
            db,
        )

    assert create_exc.value.status_code == 409
    assert reply_exc.value.status_code == 409
    assert blocked.await_count == 2
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_merchant_offer_version_is_rejected_after_row_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offer_id = uuid4()
    offer = SimpleNamespace(
        id=offer_id,
        status=communications.merchant_offers.STATUS_EXTRACTED,
        terms_version=4,
        lender_id=None,
        terms={},
    )
    profile = SimpleNamespace(id=uuid4())
    db = SimpleNamespace(get=AsyncMock(return_value=offer))
    monkeypatch.setattr(
        communications.profiles,
        "evidence_state",
        AsyncMock(return_value=SimpleNamespace(files=[])),
    )
    monkeypatch.setattr(
        communications, "_raw_merchant_offer_file_ids", AsyncMock(return_value=set())
    )
    monkeypatch.setattr(
        communications.file_contacts, "load_sources", AsyncMock(return_value=_attachment_sources())
    )
    monkeypatch.setattr(
        communications.merchant_offers, "current_offer", AsyncMock(return_value=offer)
    )
    render = Mock()
    monkeypatch.setattr(communications, "render_merchant_offer_pdf", render)

    with pytest.raises(HTTPException) as exc:
        await communications._resolve_email_attachments(
            db,
            profile=profile,
            refs=[
                communications.MerchantOfferEmailAttachmentRef(
                    kind="merchant_offer",
                    offer_id=offer_id,
                    expected_version=3,
                )
            ],
        )

    assert exc.value.status_code == 409
    assert "changed" in str(exc.value.detail).lower()
    assert db.get.await_args.kwargs["with_for_update"] is True
    render.assert_not_called()


@pytest.mark.asyncio
async def test_stale_production_term_sheet_version_is_rejected_after_row_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sheet_id = uuid4()
    sheet = SimpleNamespace(id=sheet_id, status="current", version=6)
    profile = SimpleNamespace(id=uuid4())
    db = SimpleNamespace(get=AsyncMock(return_value=sheet))
    monkeypatch.setattr(
        communications.profiles,
        "evidence_state",
        AsyncMock(return_value=SimpleNamespace(files=[])),
    )
    monkeypatch.setattr(
        communications, "_raw_merchant_offer_file_ids", AsyncMock(return_value=set())
    )
    monkeypatch.setattr(
        communications.file_contacts, "load_sources", AsyncMock(return_value=_attachment_sources())
    )
    monkeypatch.setattr(
        communications.production_term_sheets,
        "current_sheet",
        AsyncMock(return_value=sheet),
    )
    render = Mock()
    monkeypatch.setattr(communications, "render_term_sheet_pdf", render)

    with pytest.raises(HTTPException) as exc:
        await communications._resolve_email_attachments(
            db,
            profile=profile,
            refs=[
                communications.ProductionTermSheetEmailAttachmentRef(
                    kind="production_term_sheet",
                    term_sheet_id=sheet_id,
                    expected_version=5,
                )
            ],
        )

    assert exc.value.status_code == 409
    assert "newer" in str(exc.value.detail).lower()
    assert db.get.await_args.kwargs["with_for_update"] is True
    render.assert_not_called()


@pytest.mark.asyncio
async def test_thread_email_passes_exact_attachments_to_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row_id = uuid4()
    outcome = SimpleNamespace(
        ok=True,
        detail="sent_gmail",
        message_id="gmail-1",
        provider_thread_id="gmail-thread-1",
        row=SimpleNamespace(id=row_id, provider="gmail"),
    )
    deliver = AsyncMock(return_value=outcome)
    monkeypatch.setattr(communications.outbox, "deliver_email", deliver)
    db = SimpleNamespace(add=Mock(), flush=AsyncMock())
    profile = SimpleNamespace(
        id=uuid4(),
        client_id=uuid4(),
        intake_id=uuid4(),
    )
    thread = SimpleNamespace(
        id=uuid4(),
        owner_user_id=uuid4(),
        dealer_id=uuid4(),
        provider_thread_id=None,
        last_message_at=None,
    )
    user = SimpleNamespace(id=uuid4(), email="underwriter@example.com")
    attachments = [
        ("Offer.pdf", b"%PDF-offer", "application/pdf"),
        ("Statement.pdf", b"%PDF-statement", "application/pdf"),
    ]

    message = await communications._send_thread_email(
        db,
        profile=profile,
        thread=thread,
        user=user,
        to_email="client@example.com",
        cc_emails=["owner@example.com"],
        subject="Your reviewed options",
        body="Please review the attached files.",
        attachments=attachments,
    )

    draft = deliver.await_args.args[1]
    assert draft.attachments == attachments
    assert draft.to == "client@example.com"
    assert draft.cc == ["owner@example.com"]
    assert thread.provider_thread_id == "gmail-thread-1"
    assert message.message_send_id == row_id


@pytest.mark.asyncio
async def test_thread_detail_exposes_outbox_attachment_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    user = SimpleNamespace(id=uuid4(), name="Underwriter", email="uw@example.com")
    thread = SimpleNamespace(
        id=uuid4(),
        subject="Your reviewed options",
        owner_user_id=user.id,
        participant_emails=["client@example.com"],
        last_message_at=now,
        unread_count=0,
        created_at=now,
    )
    send_id = uuid4()
    message = SimpleNamespace(
        id=uuid4(),
        thread_id=thread.id,
        message_send_id=send_id,
        direction="outbound",
        subject=thread.subject,
        body="Attached.",
        sender=user.email,
        recipient="client@example.com",
        cc_emails=None,
        provider="gmail",
        delivery_status="sent",
        provider_error=None,
        created_at=now,
    )
    send = SimpleNamespace(
        id=send_id,
        status="sent",
        detail="sent_gmail",
        attachment_names=["Offer.pdf", "Statement.pdf"],
    )

    def result(rows: list[object]) -> SimpleNamespace:
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: rows),
        )

    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[result([message]), result([send])]),
        get=AsyncMock(return_value=user),
    )
    monkeypatch.setattr(communications, "_contacts", AsyncMock(return_value=([], [])))

    detail = await communications._thread_detail(
        db,
        SimpleNamespace(id=uuid4()),
        thread,
        user,
    )

    assert detail.messages[0].attachment_names == ["Offer.pdf", "Statement.pdf"]


@pytest.mark.asyncio
async def test_sms_delivery_preference_persists_on_the_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = SimpleNamespace(id=uuid4(), client_sms_delivery_enabled=False)
    expected = communications.ApplicationSmsState(delivery_enabled=True)
    db = SimpleNamespace(commit=AsyncMock())
    user = SimpleNamespace(id=uuid4(), name="Underwriter")
    monkeypatch.setattr(communications, "_load_profile", AsyncMock(return_value=profile))
    monkeypatch.setattr(communications, "_sms_state", AsyncMock(return_value=expected))
    audit = AsyncMock()
    monkeypatch.setattr(communications.profiles, "log_profile_action", audit)

    result = await communications.update_application_sms_preference(
        profile.id,
        communications.ApplicationSmsPreferencePatch(enabled=True),
        user,
        db,
    )

    assert profile.client_sms_delivery_enabled is True
    assert result.delivery_enabled is True
    db.commit.assert_awaited_once()
    assert audit.await_args.args[3] == "sms.delivery_enabled"


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
    assert profile_routes._hash_token(token) in str(
        statement.compile(compile_kwargs={"literal_binds": True})
    )

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
async def test_inbound_gmail_reply_prefers_profile_provider_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    monkeypatch.setattr(
        sms_service, "_provider", lambda: SimpleNamespace(selected_provider=lambda: "test")
    )
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


@pytest.mark.asyncio
async def test_failed_sms_creates_operator_notification(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import notifications
    from app.services.sms import ledger

    sms_message_id = uuid4()
    recorded = AsyncMock(return_value=SimpleNamespace(id=sms_message_id))
    notified = AsyncMock()
    monkeypatch.setattr(ledger, "record", recorded)
    monkeypatch.setattr(notifications, "notify_sms_delivery_failure", notified)
    monkeypatch.setattr(
        sms_service, "_provider", lambda: SimpleNamespace(selected_provider=lambda: "android")
    )
    monkeypatch.setattr(sms_service, "is_opted_out", AsyncMock(return_value=False))
    monkeypatch.setattr(
        sms_service,
        "send_sms",
        lambda _phone, _body: sms_service.SmsResult(
            False,
            "android",
            detail="Tablet gateway unreachable.",
        ),
    )
    intake_id = uuid4()

    result = await sms_service.send_sms_checked(
        SimpleNamespace(),
        to_phone="862-384-1951",
        body="Test",
        intake_id=intake_id,
        context="intake_client_reply",
    )

    assert result.ok is False
    notified.assert_awaited_once_with(
        SimpleNamespace(),
        sms_message_id=sms_message_id,
        phone_e164="+18623841951",
        provider="android",
        detail="Tablet gateway unreachable.",
        client_id=None,
        profile_id=None,
        intake_id=intake_id,
    )
