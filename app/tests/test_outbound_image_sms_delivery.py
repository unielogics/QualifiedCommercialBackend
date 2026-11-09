from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.routers import application_communications as application
from app.services import communication_images as images
from app.services import inline_images, sms
from app.services.sms import ledger


class Database:
    @asynccontextmanager
    async def begin_nested(self):
        yield


def photo(data=b"verified image"):
    return images.OutboundImage(SimpleNamespace(id=uuid4(), filename="shot.png", mime_type="image/png", size_bytes=len(data)), data)


@pytest.fixture
def sending(monkeypatch):
    mode = SimpleNamespace(value="secure_link")
    provider = SimpleNamespace(selected_provider=lambda: "android", image_delivery_mode=lambda: mode.value)
    monkeypatch.setattr(sms, "_provider", lambda: provider)
    opted_out = AsyncMock(return_value=False)
    monkeypatch.setattr(sms, "is_opted_out", opted_out)
    transport = Mock(return_value=sms.SmsResult(True, "android", "physical-message", "sent"))
    monkeypatch.setattr(sms, "send_sms", transport)
    row = SimpleNamespace(id=uuid4())
    recording = AsyncMock(return_value=row)
    monkeypatch.setattr(ledger, "record", recording)
    reserve = AsyncMock(return_value=[SimpleNamespace(id=uuid4())])
    activate, bind = AsyncMock(), AsyncMock()
    monkeypatch.setattr(images, "reserve_sms_shares", reserve)
    monkeypatch.setattr(images, "activate_sms_shares", activate)
    monkeypatch.setattr(images, "bind", bind)
    monkeypatch.setattr(images, "get_settings", lambda: SimpleNamespace(public_api_url="https://api.example.test"))
    monkeypatch.setattr(inline_images, "view_url", lambda _row: "https://storage.example.test/provider-image")
    from app.services import notifications

    monkeypatch.setattr(notifications, "notify_sms_delivery_failure", AsyncMock())
    return SimpleNamespace(db=Database(), mode=mode, opted_out=opted_out, transport=transport,
                           row=row, recording=recording, reserve=reserve, activate=activate, bind=bind)


@pytest.mark.asyncio
async def test_image_sms_keeps_suppression_gate_before_link_creation(sending):
    sending.opted_out.return_value = True
    result = await sms.send_sms_checked(sending.db, to_phone="+12125550123", body="", images=[photo()])
    assert not result.ok
    sending.reserve.assert_not_awaited()
    sending.transport.assert_not_called()
    assert sending.recording.await_args.kwargs["status"] == "blocked"


@pytest.mark.asyncio
async def test_missing_consent_blocks_images_before_any_delivery(sending, monkeypatch):
    from app.dealer_os.services import sms_consent

    monkeypatch.setattr(sms_consent, "consent_for", AsyncMock(return_value=None))
    result = await sms.send_sms_checked(sending.db, to_phone="+12125550123", body="See image", images=[photo()], require_consent_kind="transactional")
    assert not result.ok and "consent" in result.detail
    sending.transport.assert_not_called()
    sending.reserve.assert_not_awaited()


@pytest.mark.asyncio
async def test_secure_link_delivery_keeps_bearer_url_out_of_ledger(sending):
    prepared = [photo()]
    result = await sms.send_sms_checked(sending.db, to_phone="+12125550123", body="", images=prepared)
    assert result.ok
    wire = sending.transport.call_args.args[1]
    assert "/api/v1/communications/images/" in wire and "expire in 7 days" in wire
    assert sending.recording.await_args.kwargs["body"] == "Image attachment"
    sending.activate.assert_awaited_once_with(sending.reserve.return_value, sending.row.id)
    sending.bind.assert_awaited_once_with(sending.db, prepared, subject_kind="sms_message", subject_id=str(sending.row.id))


@pytest.mark.asyncio
async def test_native_mms_uses_media_parameter_without_text_link_fallback(sending):
    sending.mode.value = "mms"
    result = await sms.send_sms_checked(sending.db, to_phone="+12125550123", body="Caption", images=[photo()])
    assert result.ok
    assert sending.transport.call_args.args == ("+12125550123", "Caption")
    assert sending.transport.call_args.kwargs == {"media_urls": ["https://storage.example.test/provider-image"]}
    sending.reserve.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_mms_rejects_more_than_five_mb_before_send(sending):
    sending.mode.value = "mms"
    result = await sms.send_sms_checked(sending.db, to_phone="+12125550123", body="Caption", images=[photo(b"x" * (5 * 1024 * 1024 + 1))])
    assert not result.ok and "5 MB" in result.detail
    sending.transport.assert_not_called()


@pytest.mark.asyncio
async def test_failed_send_never_activates_image_links(sending):
    sending.transport.return_value = sms.SmsResult(False, "android", detail="offline")
    result = await sms.send_sms_checked(sending.db, to_phone="+12125550123", body="Caption", images=[photo()])
    assert not result.ok and result.detail == "offline"
    sending.activate.assert_not_awaited()
    assert sending.recording.await_args.kwargs["status"] == "failed"


@pytest.mark.asyncio
async def test_post_send_history_failure_preserves_delivery_and_activates_durable_links(sending):
    sending.bind.side_effect = RuntimeError("database history unavailable")
    result = await sms.send_sms_checked(sending.db, to_phone="+12125550123", body="Caption", images=[photo()])
    assert result.ok and result.detail == "sent_not_recorded"
    sending.transport.assert_called_once()
    sending.activate.assert_awaited_once_with(sending.reserve.return_value, None)


@pytest.mark.asyncio
async def test_link_activation_failure_is_an_honest_sent_receipt_without_resend(sending):
    sending.activate.side_effect = RuntimeError("database unavailable")
    result = await sms.send_sms_checked(sending.db, to_phone="+12125550123", body="Caption", images=[photo()])
    assert result.ok and result.detail == "sent_but_image_links_unavailable"
    sending.transport.assert_called_once()


@pytest.mark.asyncio
async def test_link_preparation_failure_and_invalid_public_origin_do_not_send(sending, monkeypatch):
    sending.reserve.side_effect = RuntimeError("database unavailable")
    result = await sms.send_sms_checked(sending.db, to_phone="+12125550123", body="Caption", images=[photo()])
    assert not result.ok
    sending.transport.assert_not_called()
    monkeypatch.setattr(images, "get_settings", lambda: SimpleNamespace(public_api_url=""))
    sending.reserve.reset_mock()
    result = await sms.send_sms_checked(sending.db, to_phone="+12125550123", body="Caption", images=[photo()])
    assert not result.ok and "HTTPS" in result.detail
    sending.reserve.assert_not_awaited()


@pytest.mark.asyncio
async def test_email_images_and_existing_pdfs_share_total_limit(monkeypatch):
    monkeypatch.setattr(images, "load", AsyncMock(return_value=[photo(b"i" * (3 * 1024 * 1024))]))
    with pytest.raises(HTTPException) as error:
        await application._add_outbound_images(object(), SimpleNamespace(image_ids=[uuid4()]), SimpleNamespace(id=uuid4()),
                                               [("statement.pdf", b"p" * (4 * 1024 * 1024), "application/pdf")], [])
    assert error.value.status_code == 413


@pytest.mark.asyncio
async def test_stored_image_email_retry_reuses_exact_bytes_and_scope(monkeypatch):
    user = SimpleNamespace(id=uuid4(), name="Operator", email="operator@example.test")
    profile = SimpleNamespace(id=uuid4())
    thread = SimpleNamespace(id=uuid4(), owner_user_id=user.id, subject="Image", participant_emails=["client@example.test"],
                             last_message_at=None, unread_count=0, created_at=None)
    message = SimpleNamespace(id=uuid4(), direction="outbound", delivery_status="failed", recipient="client@example.test",
                              cc_emails=[], body="Caption", subject="Image", provider_error="provider unavailable")
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: message)))
    monkeypatch.setattr(application, "_load_profile", AsyncMock(return_value=profile))
    monkeypatch.setattr(application, "_enforce_direct_client_contact", AsyncMock())
    monkeypatch.setattr(application, "_profile_thread", AsyncMock(return_value=thread))
    monkeypatch.setattr(application, "_enforce_private_credit_recipient", AsyncMock())
    monkeypatch.setattr(application.profile_routes, "_require_training_live_action", AsyncMock())
    monkeypatch.setattr(application, "_attachment_names_for", AsyncMock(return_value=["shot.png"]))
    prepared = [photo()]
    load = AsyncMock(return_value=prepared)
    monkeypatch.setattr(images, "load_bound", load)
    send = AsyncMock(return_value=SimpleNamespace(delivery_status="sent"))
    monkeypatch.setattr(application, "_send_thread_email", send)
    monkeypatch.setattr(application, "_complete_thread_send", AsyncMock(return_value="thread detail"))
    result = await application.retry_application_email_message(profile.id, thread.id, message.id, object(), user, db)
    assert result == "thread detail"
    load.assert_awaited_once_with(db, "rep_inbox_message", str(message.id), user.id)
    assert send.await_args.kwargs["attachments"] == [prepared[0].attachment]
    assert send.await_args.kwargs["images"] == prepared
