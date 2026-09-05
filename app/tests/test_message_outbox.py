"""One row per message, including the ones that never left.

Eleven send paths recorded nothing at all and nine more recorded that something
went without recording what it said, so "what did we send them?" had no answer.
This is the door they all go through now, and the contract it keeps is the one
`send_sms_checked` has kept since 0169: a row either way.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app import request_context as rc
from app.services.email.ses_client import SesSendResult
from app.services.messaging import outbox
from app.services.messaging.redact import MARKER, mask_secrets


def _db():
    db = SimpleNamespace(added=[])
    db.add = db.added.append
    db.flush = AsyncMock()
    return db


def _draft(**over):
    kw = {"to": "dana@example.com", "subject": "Action needed", "body_text": "Hello."}
    kw.update(over)
    return outbox.Draft(**kw)


# --- a row either way ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_successful_send_is_recorded_with_the_provider_id():
    db = _db()
    with patch("app.services.email.ses_client.send_email", return_value=SesSendResult(True, "SES-1", "sent")):
        out = await outbox.deliver_email(db, _draft(), context="booking_confirmation")
    assert out.ok
    row = db.added[0]
    assert row.status == "sent" and row.provider_message_id == "SES-1"
    assert row.provider == "ses" and row.context == "booking_confirmation"


@pytest.mark.asyncio
async def test_a_refused_send_is_a_row_with_a_reason_not_an_absence():
    db = _db()
    refused = SesSendResult(False, None, "send_failed: address suppressed")
    with patch("app.services.email.ses_client.send_email", return_value=refused):
        out = await outbox.deliver_email(db, _draft(), context="invite")
    assert out.ok is False
    row = db.added[0]
    assert row.status == "failed" and "suppressed" in row.detail
    assert row.failed_at is not None


@pytest.mark.asyncio
async def test_a_transport_that_raises_still_leaves_the_attempt_on_record():
    """The row is written before the transport is called, on purpose."""
    db = _db()
    with patch("app.services.email.ses_client.send_email", side_effect=RuntimeError("boom")):
        out = await outbox.deliver_email(db, _draft(), context="invite")
    assert out.ok is False and "boom" in out.detail
    assert db.added[0].status == "failed"


@pytest.mark.asyncio
async def test_an_unusable_recipient_is_blocked_and_never_reaches_the_wire():
    db = _db()
    with patch("app.services.email.ses_client.send_email") as send:
        out = await outbox.deliver_email(db, _draft(to="not-an-address"), context="invite")
    send.assert_not_called()
    assert out.ok is False
    assert db.added[0].status == "blocked"


# --- the body -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_stored_body_is_ciphertext_not_the_message():
    db = _db()
    with patch("app.services.email.ses_client.send_email", return_value=SesSendResult(True, "1", "sent")):
        await outbox.deliver_email(db, _draft(body_text="Confidential terms."), context="x")
    row = db.added[0]
    assert row.body_text_enc and "Confidential terms." not in row.body_text_enc
    assert row.encryption_provider in ("fernet", "aws_kms")


@pytest.mark.asyncio
async def test_a_declared_secret_never_reaches_storage():
    db = _db()
    body = "Open this secure link:\nhttps://app.qc.com/agreement/S3cr3tOpaqueThing"
    draft = _draft(body_text=body, secrets=("S3cr3tOpaqueThing",))
    with patch("app.services.email.ses_client.send_email", return_value=SesSendResult(True, "1", "sent")):
        await outbox.deliver_email(db, draft, context="signature")
    row = db.added[0]
    assert row.secrets_masked is True
    from app.services.email.user_inbox_sync import decrypt_body

    stored = decrypt_body(row.body_text_enc, row.encryption_provider)
    assert "S3cr3tOpaqueThing" not in stored and MARKER in stored


@pytest.mark.asyncio
async def test_a_message_with_no_secret_is_not_flagged_as_masked():
    db = _db()
    with patch("app.services.email.ses_client.send_email", return_value=SesSendResult(True, "1", "sent")):
        await outbox.deliver_email(db, _draft(body_text="Your call is tomorrow at 10."), context="x")
    assert db.added[0].secrets_masked is False


# --- the masker's own contract ------------------------------------------------------


def test_the_backstop_catches_every_link_shape_we_actually_send():
    for body in (
        "https://app.qc.com/buckets/request/aB3xY9zQ7t",
        "https://app.qc.com/room?token=abc123def456",
        "https://app.qc.com/r/x#p=884120",
        "Your PIN is 104293.",
    ):
        out, hits = mask_secrets(body)
        assert hits, f"unmasked: {body}"
        assert MARKER in out


def test_a_short_declared_string_is_ignored_rather_than_corrupting_the_body():
    """Two characters is a coincidence, not a secret."""
    out, hits = mask_secrets("Meet at 10 to review the file.", known=("10",))
    assert out == "Meet at 10 to review the file." and hits == []


def test_masking_leaves_an_innocent_message_untouched():
    body = "Thanks for sending the bank statements — we have everything we need."
    out, hits = mask_secrets(body)
    assert out == body and hits == []


# --- who sent it, why, and who may see it --------------------------------------------


@pytest.mark.asyncio
async def test_a_message_carries_the_request_that_caused_it():
    db = _db()
    with rc.bind(request_id="req-42"):
        rc.set_actor("user-7", actor_label="loan_exec")
        with patch("app.services.email.ses_client.send_email", return_value=SesSendResult(True, "1", "sent")):
            await outbox.deliver_email(db, _draft(), context="x")
    row = db.added[0]
    assert row.request_id == "req-42"
    assert row.actor_user_id == "user-7" and row.actor_label == "loan_exec"


@pytest.mark.asyncio
async def test_the_sender_owns_what_they_sent():
    db = _db()
    with rc.bind(request_id="r"):
        rc.set_actor("user-7", actor_label="loan_exec")
        with patch("app.services.email.ses_client.send_email", return_value=SesSendResult(True, "1", "sent")):
            await outbox.deliver_email(db, _draft(), context="x")
    assert db.added[0].owner_user_id == "user-7"


@pytest.mark.asyncio
async def test_a_cron_send_with_no_subject_belongs_to_nobody():
    """And nobody means super admins only — a message with no owner must not
    default to everybody."""
    db = _db()
    with rc.bind(actor_label="cron", job="job_admin_activity_digest"):
        with patch("app.services.email.ses_client.send_email", return_value=SesSendResult(True, "1", "sent")):
            await outbox.deliver_email(db, _draft(), context="digest")
    row = db.added[0]
    assert row.owner_user_id is None
    assert row.actor_label == "cron" and row.job == "job_admin_activity_digest"


@pytest.mark.asyncio
async def test_a_cron_send_about_a_file_inherits_that_file_s_owner():
    db = _db()
    desk = uuid4()
    with rc.bind(actor_label="cron", job="job_booking_reminders"):
        with patch("app.services.email.ses_client.send_email", return_value=SesSendResult(True, "1", "sent")):
            await outbox.deliver_email(
                db, _draft(), context="booking_reminder",
                subject=outbox.Subject(owner_user_id=desk, client_id=uuid4()),
            )
    assert db.added[0].owner_user_id == desk


@pytest.mark.asyncio
async def test_a_ledger_failure_never_stops_the_send():
    db = _db()
    db.flush = AsyncMock(side_effect=RuntimeError("db gone"))
    with patch("app.services.email.ses_client.send_email", return_value=SesSendResult(True, "1", "sent")) as send:
        out = await outbox.deliver_email(db, _draft(), context="x")
    send.assert_called_once()
    assert out.ok is True
