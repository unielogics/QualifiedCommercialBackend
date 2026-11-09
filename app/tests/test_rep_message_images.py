"""Rep contact SMS/email images retain the existing access and delivery gates."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.dealer_os import router
from app.dealer_os.schemas import RepInboxMessageCreate
from app.services import communication_images, inline_images, sms


def fixture(monkeypatch, *, channel="email", dealer=None):
    user = SimpleNamespace(id=uuid4(), email="sender@example.test", name="Sender")
    thread = SimpleNamespace(id=uuid4(), owner_user_id=user.id, contact_id=uuid4(), dealer_id=dealer.id if dealer else None,
                             channel=channel, subject="Statement")
    contact = SimpleNamespace(id=thread.contact_id, full_name="Client", company="Business", email="client@example.test",
                              phone_e164="+15555550101", sms_opted_out_at=None,
                              sms_transactional_consented_at="2026-11-01", sms_marketing_consented_at=None)
    db = SimpleNamespace(add=Mock(), commit=AsyncMock(), refresh=AsyncMock(), rollback=AsyncMock())
    row = SimpleNamespace(id=uuid4())
    image = communication_images.OutboundImage(SimpleNamespace(id=uuid4(), filename="screen.png", mime_type="image/png"), b"png-bytes")
    loader = AsyncMock(return_value=[image])
    binder = AsyncMock()
    append = AsyncMock(return_value=row)
    training = AsyncMock()
    emit = AsyncMock()
    monkeypatch.setattr(communication_images, "load", loader)
    monkeypatch.setattr(communication_images, "bind", binder)
    monkeypatch.setattr(inline_images, "serialize", lambda r: {"id": str(r.id), "filename": r.filename, "url": "https://images.example.test/screen"})
    monkeypatch.setattr(router, "_append_rep_inbox_message", append)
    monkeypatch.setattr(router, "_require_training_live_action", training)
    monkeypatch.setattr(router.file_events, "emit", emit)
    return SimpleNamespace(db=db, user=user, thread=thread, contact=contact, image=image, row=row,
                           loader=loader, binder=binder, append=append, training=training, emit=emit, dealer=dealer)


async def send(env, *, body="", ids=None):
    return await router._send_rep_inbox_message(
        env.db, thread=env.thread, contact=env.contact, dealer=env.dealer,
        payload=RepInboxMessageCreate(body=body, image_ids=ids if ids is not None else [env.image.row.id]),
        request=SimpleNamespace(), user=env.user,
    )


def test_rep_image_only_payload_is_valid_but_empty_message_is_not():
    assert RepInboxMessageCreate(image_ids=[uuid4()]).body == ""
    with pytest.raises(ValidationError):
        RepInboxMessageCreate(body="  ")
    with pytest.raises(ValidationError):
        RepInboxMessageCreate(image_ids=[uuid4() for _ in range(11)])


async def test_rep_email_sends_real_mime_attachments_and_binds_transcript(monkeypatch):
    env = fixture(monkeypatch)
    native = Mock(return_value=SimpleNamespace(ok=True, message_id="mail", detail="sent"))
    plain = Mock()
    monkeypatch.setattr(router.ses_client, "send_raw_email", native)
    monkeypatch.setattr(router.ses_client, "send_email", plain)
    result = await send(env)
    assert result is env.row
    plain.assert_not_called()
    assert native.call_args.kwargs == {"to_emails": [env.contact.email], "subject": "Statement", "body_text": "",
                                       "attachments": [("screen.png", b"png-bytes", "image/png")]}
    env.loader.assert_awaited_once_with(env.db, [env.image.row.id], env.user.id)
    env.binder.assert_awaited_once_with(env.db, [env.image], subject_kind="rep_inbox_message", subject_id=str(env.row.id))
    assert result.images[0]["id"] == str(env.image.row.id)
    env.db.commit.assert_awaited_once()


async def test_rep_sms_retains_contact_consent_and_global_guard(monkeypatch):
    env = fixture(monkeypatch, channel="sms")
    checked = AsyncMock(return_value=sms.SmsResult(True, "android", "sms", "sent"))
    monkeypatch.setattr(sms, "send_sms_checked", checked)
    monkeypatch.setattr(router.consent_delivery, "sms_sender", lambda: "+15555550100")
    result = await send(env)
    assert result is env.row
    checked.assert_awaited_once_with(env.db, to_phone=env.contact.phone_e164, body="", context="rep_inbox", images=[env.image])
    assert env.append.await_args.kwargs["delivery_status"] == "sent"
    assert env.append.await_args.kwargs["provider_message_id"] == "sms"


async def test_rep_sms_keeps_accepted_but_unavailable_image_link_warning(monkeypatch):
    env = fixture(monkeypatch, channel="sms")
    checked = AsyncMock(return_value=sms.SmsResult(True, "android", "sms", "sent_but_image_links_unavailable"))
    monkeypatch.setattr(sms, "send_sms_checked", checked)
    monkeypatch.setattr(router.consent_delivery, "sms_sender", lambda: "+15555550100")
    await send(env)
    assert env.append.await_args.kwargs["delivery_status"] == "sent"
    assert env.append.await_args.kwargs["provider_error"] == "sent_but_image_links_unavailable"


@pytest.mark.parametrize("kind", ["opted_out", "no_consent"])
async def test_rep_sms_consent_failure_does_not_read_or_send_images(monkeypatch, kind):
    env = fixture(monkeypatch, channel="sms")
    if kind == "opted_out":
        env.contact.sms_opted_out_at = "2026-11-02"
    else:
        env.contact.sms_transactional_consented_at = None
    checked = AsyncMock()
    monkeypatch.setattr(sms, "send_sms_checked", checked)
    with pytest.raises(HTTPException) as exc:
        await send(env)
    assert exc.value.status_code == 409
    env.loader.assert_not_awaited()
    checked.assert_not_awaited()


async def test_rep_training_guard_runs_before_image_upload_or_delivery(monkeypatch):
    env = fixture(monkeypatch, dealer=SimpleNamespace(id=uuid4(), name="Training"))
    env.training.side_effect = HTTPException(409, "Training live send blocked")
    native = Mock()
    monkeypatch.setattr(router.ses_client, "send_raw_email", native)
    with pytest.raises(HTTPException) as exc:
        await send(env)
    assert exc.value.status_code == 409
    env.loader.assert_not_awaited()
    native.assert_not_called()


async def test_rep_send_failure_preserves_reusable_images_and_failed_receipt(monkeypatch):
    env = fixture(monkeypatch)
    monkeypatch.setattr(router.ses_client, "send_raw_email", Mock(return_value=SimpleNamespace(ok=False, message_id=None, detail="refused")))
    await send(env)
    assert env.append.await_args.kwargs["delivery_status"] == "failed"
    assert env.append.await_args.kwargs["provider_error"] == "refused"
    assert not hasattr(env.image.row, "subject_id")  # Binding references never consumes source upload.


@pytest.mark.parametrize("failure", ["bind", "commit", "append"])
async def test_rep_physical_send_is_not_reported_failed_after_recording_error(monkeypatch, failure):
    env = fixture(monkeypatch)
    native = Mock(return_value=SimpleNamespace(ok=True, message_id="mail", detail="sent"))
    monkeypatch.setattr(router.ses_client, "send_raw_email", native)
    {"bind": env.binder, "commit": env.db.commit, "append": env.append}[failure].side_effect = RuntimeError("database unavailable")
    result = await send(env)
    assert result.delivery_status == "sent" and result.provider_error == "sent_not_recorded"
    assert result.provider_message_id == "mail"
    native.assert_called_once()
    env.db.rollback.assert_awaited_once()


async def test_rep_hydration_uses_only_authorized_message_ids(monkeypatch):
    rows = [SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())]
    hydrate = AsyncMock(return_value={str(rows[0].id): [{"id": "image"}]})
    monkeypatch.setattr(inline_images, "hydrate", hydrate)
    db = object()
    await router._hydrate_rep_message_images(db, rows)
    hydrate.assert_awaited_once_with(db, "rep_inbox_message", [str(row.id) for row in rows])
    assert rows[0].images == [{"id": "image"}] and rows[1].images == []
