"""Image replies preserve mailbox isolation, delivery truth and real MIME."""
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.routers import inbox
from app.schemas.inbox import InboxReplyRequest
from app.services import communication_images, inline_images
from app.services.email import ses_client


def fixtures(monkeypatch, *, success=True):
    user = SimpleNamespace(id=uuid4(), email="sender@example.test")
    source = SimpleNamespace(id=uuid4(), direction="inbound", from_email="client@example.test", mailbox=user.email,
                             gmail_thread_id="thread", subject="Loan documents", loan_id=uuid4(), client_id=uuid4(),
                             matched_party_role="client", is_read=False)
    db = SimpleNamespace(add=Mock(), commit=AsyncMock(), rollback=AsyncMock(), flush=AsyncMock())
    monkeypatch.setattr(inbox, "_load_thread_rows", AsyncMock(return_value=[source]))
    monkeypatch.setattr(inbox, "_encrypt_reply_body", lambda body: (f"encrypted:{body}", "test"))
    monkeypatch.setattr(inbox, "send_as_user", AsyncMock(return_value=SimpleNamespace(ok=success, detail="sent" if success else "failed", message_id="sent-id", thread_id="thread")))
    image = communication_images.OutboundImage(SimpleNamespace(filename="screenshot.png", mime_type="image/png"), b"picture-bytes")
    monkeypatch.setattr(communication_images, "load", AsyncMock(return_value=[image]))
    monkeypatch.setattr(communication_images, "bind", AsyncMock())
    return user, source, db, image


@pytest.mark.asyncio
async def test_image_only_reply_sends_bytes_and_records_encrypted_owner_history(monkeypatch):
    user, source, db, image = fixtures(monkeypatch)
    image_id = uuid4()
    result = await inbox.reply_to_thread("thread", InboxReplyRequest(image_ids=[image_id]), user, db)
    assert result.ok and source.is_read
    communication_images.load.assert_awaited_once_with(db, [image_id], user.id)
    sent = inbox.send_as_user.await_args.kwargs
    assert sent["body_text"] == "" and sent["attachments"] == [("screenshot.png", b"picture-bytes", "image/png")]
    assert sent["to_emails"] == [source.from_email]
    row = db.add.call_args.args[0]
    assert row.owner_user_id == user.id and row.gmail_thread_id == "thread"
    assert row.body_text_enc == "encrypted:" and row.has_attachments
    communication_images.bind.assert_awaited_once_with(db, [image], subject_kind="email_message", subject_id=str(row.id))
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_refused_image_id_never_reaches_email_provider(monkeypatch):
    user, _, db, _ = fixtures(monkeypatch)
    communication_images.load.side_effect = inline_images.InlineImageError("Another author's image")
    with pytest.raises(HTTPException) as error:
        await inbox.reply_to_thread("thread", InboxReplyRequest(body="Caption", image_ids=[uuid4()]), user, db)
    assert error.value.status_code == 400
    inbox.send_as_user.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_other_owners_thread_never_loads_images_or_sends(monkeypatch):
    user, _, db, _ = fixtures(monkeypatch)
    inbox._load_thread_rows.return_value = []
    with pytest.raises(HTTPException) as error:
        await inbox.reply_to_thread("not-owned", InboxReplyRequest(image_ids=[uuid4()]), user, db)
    assert error.value.status_code == 404
    communication_images.load.assert_not_awaited()
    inbox.send_as_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_send_retains_upload_and_unread_state(monkeypatch):
    user, source, db, _ = fixtures(monkeypatch, success=False)
    result = await inbox.reply_to_thread("thread", InboxReplyRequest(image_ids=[uuid4()]), user, db)
    assert not result.ok and not source.is_read
    db.add.assert_not_called()
    communication_images.bind.assert_not_awaited()


@pytest.mark.asyncio
async def test_sent_but_unrecorded_reply_is_not_reported_as_unsent(monkeypatch):
    user, _, db, _ = fixtures(monkeypatch)
    db.commit.side_effect = RuntimeError("database temporarily unavailable")
    result = await inbox.reply_to_thread("thread", InboxReplyRequest(image_ids=[uuid4()]), user, db)
    assert result.ok and result.detail == "sent_not_recorded" and result.message_id == "sent-id"
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_image_history_failure_cannot_turn_sent_email_into_retryable_send_failure(monkeypatch):
    user, _, db, _ = fixtures(monkeypatch)
    communication_images.bind.side_effect = RuntimeError("image history unavailable")
    result = await inbox.reply_to_thread("thread", InboxReplyRequest(image_ids=[uuid4()]), user, db)
    assert result.ok and result.detail == "sent_not_recorded"
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_text_reply_preserves_transport_contract(monkeypatch):
    user, _, db, _ = fixtures(monkeypatch)
    result = await inbox.reply_to_thread("thread", InboxReplyRequest(body="Existing text"), user, db)
    assert result.ok and "attachments" not in inbox.send_as_user.await_args.kwargs
    communication_images.load.assert_not_awaited()
    assert not db.add.call_args.args[0].has_attachments


@pytest.mark.asyncio
async def test_empty_reply_does_not_send(monkeypatch):
    user, _, db, _ = fixtures(monkeypatch)
    with pytest.raises(HTTPException) as error:
        await inbox.reply_to_thread("thread", InboxReplyRequest(body="  "), user, db)
    assert error.value.status_code == 400
    inbox.send_as_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_thread_image_hydration_occurs_only_after_owner_scope(monkeypatch):
    user, _, db, _ = fixtures(monkeypatch)
    inbox._load_thread_rows.return_value = []
    hydrate = AsyncMock()
    monkeypatch.setattr(inline_images, "hydrate", hydrate)
    with pytest.raises(HTTPException):
        await inbox.get_thread("not-owned", user, db)
    hydrate.assert_not_awaited()


def test_image_is_an_actual_email_mime_attachment(monkeypatch):
    settings = SimpleNamespace(ses_from_address="desk@example.test", ses_region="us-east-1", ses_configuration_set="")
    monkeypatch.setattr(ses_client, "get_settings", lambda: settings)
    client = Mock()
    client.send_raw_email.return_value = {"MessageId": "sent"}
    monkeypatch.setattr(ses_client, "_ses_client", lambda region: client)
    result = ses_client.send_raw_email(to_emails=["recipient@example.test"], subject="Screenshot", body_text="",
                                       attachments=[("screenshot.png", b"picture-bytes", "image/png")])
    assert result.ok
    message = BytesParser(policy=policy.default).parsebytes(client.send_raw_email.call_args.kwargs["RawMessage"]["Data"])
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_content_type() == "image/png"
    assert attachments[0].get_filename() == "screenshot.png"
    assert attachments[0].get_payload(decode=True) == b"picture-bytes"
