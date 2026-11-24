from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from app.models.client import Client
from app.models.email_marketing import (
    MarketingEnrollment,
    MarketingPermissionBatch,
    MarketingPermissionRequest,
    MarketingSubscriber,
)
from app.models.message_send import MessageSend
from app.services import marketing_permission_delivery as worker
from app.services.email import ses_client
from app.services.prospect_outreach import set_suppression
from app.tests._sqlite_schema import sqlite_session, sqlite_template


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    return sqlite_template(tmp_path_factory, "permission_delivery")


@pytest.fixture
async def db(template, tmp_path):
    async with sqlite_session(template, tmp_path) as session:
        event.listen(session.bind.sync_engine, "connect", lambda connection, _:
                     connection.create_function("pg_advisory_xact_lock", 1, lambda key: 0))
        yield session


@pytest.fixture
def provider(monkeypatch):
    fake_ses = Mock()
    fake_ses.send_raw_email.return_value = {"MessageId": "provider-id"}
    monkeypatch.setattr(ses_client, "_ses_client", lambda region: fake_ses)
    monkeypatch.setattr(ses_client, "get_settings", lambda: SimpleNamespace(
        ses_region="us-east-1", ses_from_address="desk@example.test", ses_configuration_set=""))
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(prospect_mailing_address="123 Example Ave"))
    monkeypatch.setattr(worker.marketing_permission_requests, "permission_token", lambda _id: "fixture-capability")
    monkeypatch.setattr(worker.marketing_permission_requests, "request_link", lambda token: "https://example.test/" + token)
    monkeypatch.setattr(worker.outbox, "_encrypt_body", lambda body: ("encrypted-example", "fernet"))
    return fake_ses


async def prepare(db, *, status="queued", locale="en", subscribe=False):
    now = datetime.now(UTC)
    batch = MarketingPermissionBatch(id=uuid4(), action="request_opt_in", fingerprint="f" * 64)
    row = MarketingPermissionRequest(email=f"test-{uuid4().hex[:8]}@example.org", locale=locale,
        status=status, due_at=now, expires_at=now + timedelta(days=30), batch_id=batch.id,
        invite_batch_id=batch.id if status != "task_only" else None)
    db.add_all([batch, row, Client(name="Example Person", email=row.email)])
    if subscribe:
        from app.services.email_marketing import enroll
        await enroll(db, email=row.email, source="test-form", consent=True)
    await db.commit()
    return now, row


@pytest.mark.parametrize(("locale", "cta"), [("es", "Revisar mi preferencia"), ("en", "Review my email preference")])
async def test_permission_invite_has_only_recipient_choice_and_never_enrolls(db, provider, locale, cta):
    now, row = await prepare(db, locale=locale)
    await worker.dispatch_one(db, row.id, now)
    await worker.dispatch_one(db, row.id, now + timedelta(hours=2))
    assert row.status == "sent" and row.message_send_id is not None
    provider.send_raw_email.assert_called_once()
    wire = BytesParser(policy=policy.default).parsebytes(provider.send_raw_email.call_args.kwargs["RawMessage"]["Data"])
    assert wire["List-Unsubscribe"] == "<https://example.test/fixture-capability/decline>"
    assert wire["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    for part in ("plain", "html"):
        body = wire.get_body(preferencelist=(part,)).get_content()
        assert cta in body and "123 Example Ave" in body and "/fixture-capability" in body
        assert "instagram.com" not in body and "facebook.com" not in body
    assert await db.scalar(select(MarketingSubscriber.id)) is None
    assert await db.scalar(select(MarketingEnrollment.id)) is None
    assert (await db.get(MessageSend, row.message_send_id)).context == "marketing_permission"


@pytest.mark.parametrize("change", ["blocked", "already_consent", "withdrawn", "expired", "task_only", "future"])
async def test_final_invitation_check_prevents_outbound_mail(db, provider, change):
    now, row = await prepare(db, status="task_only" if change == "task_only" else "queued",
                             subscribe=change in {"already_consent", "withdrawn"})
    if change == "blocked":
        await set_suppression(db, email=row.email, reason="unsubscribe", source="fixture")
    elif change == "withdrawn":
        member = await db.scalar(select(MarketingSubscriber).where(MarketingSubscriber.email == row.email))
        member.unsubscribed_at = now
    elif change == "expired":
        row.expires_at = now - timedelta(minutes=1)
    elif change == "future":
        row.due_at = now + timedelta(hours=1)
    await db.commit()
    await worker.dispatch_one(db, row.id, now)
    provider.send_raw_email.assert_not_called()
    assert row.status == {"expired": "expired", "task_only": "task_only", "future": "queued",
                          "already_consent": "confirmed"}.get(change, "blocked")


async def test_ambiguous_delivery_never_retries_and_explicit_refusal_can_retry(db, provider):
    from botocore.exceptions import ClientError

    now, row = await prepare(db)
    provider.send_raw_email.side_effect = ClientError({"Error": {"Code": "Throttling", "Message": "slow down"}}, "SendRawEmail")
    await worker.dispatch_one(db, row.id, now)
    assert row.status == "queued"
    await worker.dispatch_one(db, row.id, now + timedelta(minutes=2))
    assert provider.send_raw_email.call_count == 1
    provider.send_raw_email.side_effect = TimeoutError("outcome unknown")
    await worker.dispatch_one(db, row.id, now + timedelta(hours=1))
    assert row.status == "uncertain"
    await worker.dispatch_one(db, row.id, now + timedelta(hours=2))
    assert provider.send_raw_email.call_count == 2


async def test_recovering_stranded_claim_never_resubmits_to_provider(db, provider):
    now, row = await prepare(db)
    row.status = "sending"
    await db.commit()
    await worker.dispatch_one(db, row.id, now)
    assert row.status == "uncertain"
    provider.send_raw_email.assert_not_called()
