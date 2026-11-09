"""File-level SMS and portal pictures share the authorized message evidence."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.routers import dealer_ai_intake as core
from app.routers.dealer_ai_intake_routes import admin
from app.services import communication_images, inline_images


def setup(monkeypatch):
    user = SimpleNamespace(id=uuid4(), name="Staff", email="staff@example.test")
    link = SimpleNamespace(id=uuid4())
    intake = SimpleNamespace(id=uuid4(), client_id=uuid4(), bucket_id=uuid4(), bucket=SimpleNamespace(),
                             bucket_upload_link=link, bucket_upload_link_id=link.id, phone="+15555550100")
    message = SimpleNamespace(id=uuid4(), content="")
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: uuid4())), commit=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr(core, "_require_super_admin", Mock())
    monkeypatch.setattr(core, "_load_admin_dealer_lead", AsyncMock(return_value=intake))
    monkeypatch.setattr(core, "create_human_message", AsyncMock(return_value=message))
    monkeypatch.setattr(core, "send_sms_checked", AsyncMock())
    monkeypatch.setattr(core, "_log", AsyncMock())
    monkeypatch.setattr(core, "_client_thread_response", AsyncMock(return_value="detail"))
    monkeypatch.setattr(core.engagement, "pause", lambda _: SimpleNamespace(isoformat=lambda: "later"))
    images = [SimpleNamespace(row=SimpleNamespace(id=uuid4()), data=b"picture")]
    monkeypatch.setattr(communication_images, "load", AsyncMock(return_value=images))
    monkeypatch.setattr(communication_images, "bind", AsyncMock())
    monkeypatch.setattr(communication_images, "load_bound", AsyncMock(return_value=images))
    return user, intake, message, db, images


@pytest.mark.asyncio
async def test_file_image_only_reply_binds_portal_and_passes_prepared_sms_media(monkeypatch):
    user, intake, message, db, images = setup(monkeypatch)
    image_id = uuid4()
    result = await admin.reply_dealer_ai_client_thread(intake.id, core.ClientThreadReplyRequest(image_ids=[image_id], also_sms=True), None, user, db)
    assert result == "detail"
    communication_images.load.assert_awaited_once_with(db, [image_id], user.id)
    communication_images.bind.assert_awaited_once_with(db, images, subject_kind="bucket_message", subject_id=str(message.id))
    assert core.create_human_message.await_args.kwargs["message"] == ""
    sent = core.send_sms_checked.await_args.kwargs
    assert sent["images"] == images and sent["portal_message_id"] == message.id
    assert sent["require_consent_kind"] == "transactional"


@pytest.mark.asyncio
async def test_portal_only_image_does_not_text_client(monkeypatch):
    user, intake, _, db, _ = setup(monkeypatch)
    await admin.reply_dealer_ai_client_thread(intake.id, core.ClientThreadReplyRequest(image_ids=[uuid4()]), None, user, db)
    communication_images.bind.assert_awaited_once()
    core.send_sms_checked.assert_not_awaited()


@pytest.mark.asyncio
async def test_unauthorized_upload_cannot_create_message_or_send(monkeypatch):
    user, intake, _, db, _ = setup(monkeypatch)
    communication_images.load.side_effect = inline_images.InlineImageError("Image is not yours")
    with pytest.raises(HTTPException) as error:
        await admin.reply_dealer_ai_client_thread(intake.id, core.ClientThreadReplyRequest(image_ids=[uuid4()], also_sms=True), None, user, db)
    assert error.value.status_code == 400
    core.create_human_message.assert_not_awaited()
    core.send_sms_checked.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_file_reply_cannot_send(monkeypatch):
    user, intake, _, db, _ = setup(monkeypatch)
    with pytest.raises(HTTPException) as error:
        await admin.reply_dealer_ai_client_thread(intake.id, core.ClientThreadReplyRequest(message=" "), None, user, db)
    assert error.value.status_code == 400
    core.create_human_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_sms_retry_reuses_authorized_portal_images_without_duplicate_portal_message(monkeypatch):
    user, intake, message, db, images = setup(monkeypatch)
    results = [message, None, uuid4()]
    db.execute.side_effect = [SimpleNamespace(scalar_one_or_none=lambda value=value: value) for value in results]
    await admin.retry_dealer_ai_client_thread_sms(intake.id, message.id, None, user, db)
    communication_images.load_bound.assert_awaited_once_with(db, subject_kind="bucket_message", subject_id=str(message.id))
    assert core.send_sms_checked.await_args.kwargs["images"] == images
    core.create_human_message.assert_not_awaited()
    query = str(db.execute.call_args_list[0].args[0])
    assert "upload_link_id" in query and "audience" in query


@pytest.mark.asyncio
async def test_successful_sms_is_not_resent(monkeypatch):
    user, intake, message, db, _ = setup(monkeypatch)
    results = [message, SimpleNamespace(status="sent")]
    db.execute.side_effect = [SimpleNamespace(scalar_one_or_none=lambda value=value: value) for value in results]
    with pytest.raises(HTTPException) as error:
        await admin.retry_dealer_ai_client_thread_sms(intake.id, message.id, None, user, db)
    assert error.value.status_code == 409
    core.send_sms_checked.assert_not_awaited()


@pytest.mark.asyncio
async def test_sent_file_sms_is_reported_as_sent_even_when_history_commit_fails(monkeypatch):
    user, intake, _, db, _ = setup(monkeypatch)
    core.send_sms_checked.return_value = SimpleNamespace(ok=True, detail="accepted")
    db.commit.side_effect = RuntimeError("history unavailable")
    result = await admin.reply_dealer_ai_client_thread(intake.id, core.ClientThreadReplyRequest(image_ids=[uuid4()], also_sms=True), None, user, db)
    assert "text was sent" in result.delivery_warning and "Do not send it again" in result.delivery_warning
    assert core.send_sms_checked.await_count == 1
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_sent_file_sms_retry_preserves_image_link_failure_warning(monkeypatch):
    user, intake, message, db, _ = setup(monkeypatch)
    results = [message, None, uuid4()]
    db.execute.side_effect = [SimpleNamespace(scalar_one_or_none=lambda value=value: value) for value in results]
    core.send_sms_checked.return_value = SimpleNamespace(ok=True, detail="sent_but_image_links_unavailable")
    db.commit.side_effect = RuntimeError("history unavailable")
    result = await admin.retry_dealer_ai_client_thread_sms(intake.id, message.id, None, user, db)
    assert "text was sent" in result.delivery_warning and "image links are unavailable" in result.delivery_warning
    core.send_sms_checked.assert_awaited_once()
