"""Real database coverage of audience identity, campaign snapshots and durable dispatch."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select

from app.models.application_profile import ApplicationProfile, ApplicationProgramSelection
from app.models.client import Client
from app.models.email_marketing import MarketingDelivery, MarketingSubscriber
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.services import email_marketing as marketing
from app.services import email_marketing_delivery as delivery
from app.services.email import ses_client
from app.services.foreclosure_rescue import FORECLOSURE_RESCUE_VARIANT
from app.tests._sqlite_schema import sqlite_session, sqlite_template


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    return sqlite_template(tmp_path_factory, "email_marketing")


@pytest.fixture
async def db(template, tmp_path):
    async with sqlite_session(template, tmp_path) as session:
        event.listen(session.bind.sync_engine, "connect",
                     lambda connection, _: connection.create_function("pg_advisory_xact_lock", 1, lambda _key: 0))
        yield session


async def intake(db, email, **kwargs):
    row = PublicUnderwritingIntake(email=email, token_hash=uuid4().hex, bucket_id=uuid4(), full_name="Example", **kwargs)
    db.add(row)
    await db.flush()
    return row


async def audience(db, **filters):
    return list(await db.scalars(marketing.eligible_ids(marketing.AudienceFilters(**filters))))


async def test_address_only_enrollment_matches_later_file_and_pairs_filters(db):
    email = "subscriber@example.org"
    await marketing.enroll(db, email=email.upper(), source="contact", consent=True)
    await marketing.enroll(db, email=email, source="contact", consent=True)
    member = (await db.scalars(select(MarketingSubscriber))).one()
    assert len(list(await db.scalars(select(MarketingDelivery)))) == 1
    files = []
    for outcome, program in [("denied", "sba"), ("closed", "bridge")]:
        row = await intake(db, email, outcome_status=outcome, variant="main_street")
        profile = ApplicationProfile(intake_id=row.id)
        db.add(profile)
        await db.flush()
        db.add(ApplicationProgramSelection(profile_id=profile.id, program_key=program, program_name=program, source="operator"))
        files.append(row)
    await db.flush()
    assert await audience(db, program="sba", outcome="denied", source="contact") == [member.id]
    assert await audience(db, program="bridge", outcome="denied") == []
    assert await audience(db, program="sba", outcome="closed") == []
    assert await audience(db, program="main_street") == []
    files[0].outcome_status = "closed"
    await db.flush()
    assert await audience(db, program="sba", outcome="denied") == []
    assert await audience(db, program="sba", outcome="closed") == [member.id]


async def test_referring_professional_and_borrower_remain_separate(db):
    owner = Client(name="Owner", email="owner@example.org")
    db.add(owner)
    await db.flush()
    attorney = "attorney@example.org"
    file = await intake(db, attorney, client_id=owner.id, variant=FORECLOSURE_RESCUE_VARIANT,
                        referral_source="attorney", client_contact_suppressed=True, outcome_status="denied")
    for email in [owner.email, attorney]:
        await marketing.enroll(db, email=email, source="contact", consent=True)
    ids = {s.email: s.id for s in await db.scalars(select(MarketingSubscriber))}
    assert await audience(db) == [ids[attorney]]
    assert await audience(db, outcome="denied") == []
    file.client_contact_suppressed = False
    await db.flush()
    assert await audience(db, outcome="denied") == [ids[owner.email]]


async def test_queue_retry_changed_outcome_and_provider_ambiguity(db, monkeypatch):
    email = "subscriber@example.org"
    await marketing.enroll(db, email=email, source="contact", consent=True, locale="es")
    file = await intake(db, email, outcome_status="submitted")
    actor = uuid4()
    inputs = marketing.CampaignInput(id=uuid4(), name="Lending guides", subject="Program news", body="Update",
                                     filters=marketing.AudienceFilters(outcome="submitted"))
    campaign = await marketing.create_campaign(db, actor, inputs)
    assert campaign["recipient_count"] == campaign["queued"] == 1
    assert (await marketing.create_campaign(db, actor, inputs))["queued"] == 1
    with pytest.raises(HTTPException) as conflict:
        await marketing.create_campaign(db, actor, inputs.model_copy(update={"subject": "Different"}))
    assert conflict.value.status_code == 409
    file.outcome_status = "closed"
    await db.commit()
    now = datetime.now(UTC) + timedelta(seconds=1)
    first = await db.scalar(select(MarketingDelivery).where(MarketingDelivery.step == 0))
    job = await db.scalar(select(MarketingDelivery).where(MarketingDelivery.campaign_id == inputs.id))
    await delivery.dispatch_one(db, job.id, now)
    assert job.status == "blocked"
    monkeypatch.setattr(marketing, "_secret", lambda: b"test-only")
    monkeypatch.setattr(delivery.outbox, "_encrypt_body", lambda _value: ("test", "fernet"))
    provider = Mock(return_value=SimpleNamespace(ok=True, message_id="test", detail="sent"))
    monkeypatch.setattr(ses_client, "send_raw_email", provider)
    await delivery.dispatch_one(db, first.id, now)
    await delivery.dispatch_one(db, first.id, now)
    assert provider.call_count == 1 and first.status == "sent"
    message = provider.call_args.kwargs
    assert message["body_html"].count("https://www.instagram.com/qualifiedcommercial/") == 1
    assert "Cancelar suscripción" in message["body_text"]
    assert message["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    second = await db.scalar(select(MarketingDelivery).where(MarketingDelivery.step == 1))
    assert second.due_at.replace(tzinfo=UTC) >= now + timedelta(days=3) - timedelta(minutes=1)
    provider.return_value = SimpleNamespace(ok=False, message_id=None,
        detail="send_failed: An error occurred (Throttling) when calling the SendRawEmail operation: Rate exceeded")
    await delivery.dispatch_one(db, second.id, now + timedelta(days=4))
    assert second.status == "queued" and provider.call_count == 2
    provider.return_value = SimpleNamespace(ok=False, message_id=None, detail="provider timeout")
    await delivery.dispatch_one(db, second.id, now + timedelta(days=5))
    await delivery.dispatch_one(db, second.id, now + timedelta(days=6))
    assert second.status == "uncertain" and provider.call_count == 3
    assert await db.scalar(select(MarketingDelivery.id).where(MarketingDelivery.step == 2)) is None
