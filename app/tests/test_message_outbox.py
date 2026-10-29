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
    db.commit = AsyncMock()
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
async def test_durable_handoff_never_auto_retries_an_uncertain_send():
    row = SimpleNamespace(
        status="queued",
        detail="",
        provider="",
        provider_message_id=None,
        failed_at=None,
    )
    db = _db()
    db.commit = AsyncMock(side_effect=[None, RuntimeError("result commit failed")])
    with patch(
        "app.services.email.ses_client.send_email",
        return_value=SesSendResult(True, "SES-accepted", "sent"),
    ) as send:
        with pytest.raises(RuntimeError, match="result commit failed"):
            await outbox.deliver_email(
                db,
                _draft(),
                context="ach_mandate_proof",
                recorded_row=row,
                durable_handoff=True,
            )
    send.assert_called_once()

    # A fresh session reloads the last committed pre-handoff state. It must
    # not call the provider again because the first outcome is unknowable.
    reloaded = SimpleNamespace(
        status="sending",
        detail="provider handoff in progress",
        provider="",
        provider_message_id=None,
        failed_at=None,
    )
    retry_db = _db()
    with patch("app.services.email.ses_client.send_email") as retry_send:
        retry = await outbox.deliver_email(
            retry_db,
            _draft(),
            context="ach_mandate_proof",
            recorded_row=reloaded,
            durable_handoff=True,
        )
    assert retry.ok is False
    assert "fresh audited resend" in retry.detail
    retry_send.assert_not_called()
    retry_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_durable_resend_uses_a_fresh_attempt_row():
    fresh = SimpleNamespace(
        status="queued",
        detail="",
        provider="",
        provider_message_id=None,
        failed_at=None,
    )
    db = _db()
    with patch(
        "app.services.email.ses_client.send_email",
        return_value=SesSendResult(True, "SES-new", "sent"),
    ) as send:
        outcome = await outbox.deliver_email(
            db,
            _draft(),
            context="ach_mandate_proof",
            recorded_row=fresh,
            durable_handoff=True,
        )
    assert outcome.ok is True
    assert fresh.status == "sent"
    assert db.commit.await_count == 2
    send.assert_called_once()


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


# --- feedback for a send we never recorded ------------------------------------------
#
# Fifteen call sites reach ses_client directly and only the outbox writes a
# ledger row, so a delivery event can arrive for an id we have never stored —
# six did in one day on 2026-10-29. An unattributable delivery costs nothing. An
# unattributable hard bounce is an address we would otherwise keep writing to.


def _delivery_db(row=None):
    db = _db()
    result = SimpleNamespace(scalar_one_or_none=lambda: row)
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_a_permanent_bounce_for_an_unknown_id_still_suppresses_the_address():
    db = _delivery_db(row=None)
    with patch("app.services.prospect_outreach.set_suppression", new=AsyncMock()) as suppress:
        handled = await outbox.mark_delivery(
            db,
            provider_message_id="SES-unledgered",
            event="Bounce",
            detail="Permanent/General",
            recipients=["gone@example.com"],
        )
    assert handled is True
    assert suppress.await_count == 1
    assert suppress.await_args.kwargs["email"] == "gone@example.com"
    assert suppress.await_args.kwargs["details"]["unledgered"] is True


@pytest.mark.asyncio
async def test_a_transient_bounce_suppresses_nothing():
    """A full mailbox is not a dead address, and suppressing on one quietly
    loses a recipient who was fine an hour later."""
    db = _delivery_db(row=None)
    with patch("app.services.prospect_outreach.set_suppression", new=AsyncMock()) as suppress:
        handled = await outbox.mark_delivery(
            db,
            provider_message_id="SES-unledgered",
            event="Bounce",
            detail="Transient/MailboxFull",
            recipients=["busy@example.com"],
        )
    assert handled is False
    assert suppress.await_count == 0


@pytest.mark.asyncio
async def test_a_delivery_for_an_unknown_id_is_not_treated_as_feedback():
    db = _delivery_db(row=None)
    with patch("app.services.prospect_outreach.set_suppression", new=AsyncMock()) as suppress:
        handled = await outbox.mark_delivery(
            db, provider_message_id="SES-x", event="Delivery", recipients=["a@example.com"]
        )
    assert handled is False
    assert suppress.await_count == 0


@pytest.mark.asyncio
async def test_a_complaint_for_an_unknown_id_suppresses_every_recipient():
    db = _delivery_db(row=None)
    with patch("app.services.prospect_outreach.set_suppression", new=AsyncMock()) as suppress:
        await outbox.mark_delivery(
            db,
            provider_message_id="SES-y",
            event="Complaint",
            detail="abuse",
            recipients=["a@example.com", "b@example.com"],
        )
    assert [c.kwargs["email"] for c in suppress.await_args_list] == ["a@example.com", "b@example.com"]


@pytest.mark.asyncio
async def test_one_bad_address_does_not_drop_the_rest_of_the_feedback():
    """SES does not retry these, so a raise on address one must not cost two."""
    db = _delivery_db(row=None)
    failing = AsyncMock(side_effect=[ValueError("bad address"), None])
    with patch("app.services.prospect_outreach.set_suppression", new=failing):
        handled = await outbox.mark_delivery(
            db,
            provider_message_id="SES-z",
            event="Bounce",
            detail="Permanent/General",
            recipients=["broken", "real@example.com"],
        )
    assert handled is True
    assert failing.await_count == 2


@pytest.mark.asyncio
async def test_a_ledgered_transient_bounce_fails_the_row_without_suppressing():
    row = SimpleNamespace(
        id=uuid4(), status="sent", to_email="busy@example.com", prospect_id=uuid4(),
        opened_at=None, delivered_at=None, failed_at=None, detail=None,
    )
    db = _delivery_db(row=row)
    with patch("app.services.prospect_outreach.set_suppression", new=AsyncMock()) as suppress:
        await outbox.mark_delivery(
            db, provider_message_id="SES-1", event="Bounce", detail="Transient/MailboxFull",
        )
    assert row.status == "bounced"
    assert suppress.await_count == 0


@pytest.mark.asyncio
async def test_a_transactional_bounce_is_not_written_onto_the_outreach_list():
    """The prospect_id gate is deliberate. A bounce on a client's own mail is a
    problem for that file — it is not grounds for suppressing the person."""
    row = SimpleNamespace(
        id=uuid4(), status="sent", to_email="client@example.com", prospect_id=None,
        opened_at=None, delivered_at=None, failed_at=None, detail=None,
    )
    db = _delivery_db(row=row)
    with patch("app.services.prospect_outreach.set_suppression", new=AsyncMock()) as suppress:
        await outbox.mark_delivery(
            db, provider_message_id="SES-2", event="Bounce", detail="Permanent/General",
        )
    assert row.status == "bounced"
    assert suppress.await_count == 0
