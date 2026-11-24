"""Real database coverage of audience identity, campaign snapshots and durable dispatch."""
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import event, select

from app.models.application_profile import ApplicationProfile, ApplicationProgramSelection
from app.models.client import Client
from app.models.email_marketing import MarketingCampaign, MarketingDelivery, MarketingSubscriber
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


async def test_selected_recipient_html_preview_and_scheduled_delivery(db, monkeypatch):
    for address in ["selected@example.org", "excluded@example.org"]:
        await marketing.enroll(db, email=address, source="contact", consent=True)
    await db.commit()
    members = {row.email: row for row in await db.scalars(select(MarketingSubscriber))}
    member_id = members["selected@example.org"].id
    excluded = members["excluded@example.org"].id
    selection = {"mode": "all", "excluded_ids": [excluded]}
    template = {"subject": "News for {{email}}", "body_html": "<table><tr><td>Updates for {{email}}</td></tr></table>",
                "filters": {}, "selection": selection}
    preview = await marketing.preview_campaign(db, marketing.CampaignPreview(**template))
    assert preview["eligible_count"] == 1 and preview["sample_id"] == member_id
    assert "<td>Updates for selected@example.org</td>" in preview["body_html"]
    assert "Updates for selected@example.org" in preview["body_text"]
    with pytest.raises(HTTPException, match="outside"):
        await marketing.preview_campaign(db, marketing.CampaignPreview(**template, sample_id=excluded))

    when = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)
    actor = uuid4()
    payload = marketing.CampaignInput(id=uuid4(), name="Scheduled newsletter",
                                      scheduled_at=when.isoformat(), expected_recipient_count=1, **template)
    with pytest.raises(HTTPException) as mismatch:
        await marketing.create_campaign(db, actor, payload.model_copy(update={"expected_recipient_count": 5}))
    assert mismatch.value.status_code == 409
    assert await db.get(MarketingCampaign, payload.id) is None
    assert await db.scalar(select(MarketingDelivery.id).where(MarketingDelivery.campaign_id == payload.id)) is None
    saved = await marketing.create_campaign(db, actor, payload)
    assert saved["recipient_count"] == 1
    assert saved["scheduled_at"].replace(tzinfo=UTC) == when
    await marketing.enroll(db, email="later@example.org", source="contact", consent=True)
    assert (await marketing.create_campaign(db, actor, payload.model_copy(update={"expected_recipient_count": 2})))["recipient_count"] == 1
    with pytest.raises(HTTPException) as changed:
        await marketing.create_campaign(db, actor, payload.model_copy(update={"scheduled_at": when + timedelta(days=1)}))
    assert changed.value.status_code == 409

    job = (await db.scalars(select(MarketingDelivery).where(MarketingDelivery.campaign_id == payload.id))).one()
    assert job.subscriber_id == member_id and job.due_at.replace(tzinfo=UTC) == when
    monkeypatch.setattr(marketing, "_secret", lambda: b"test-only")
    monkeypatch.setattr(delivery.outbox, "_encrypt_body", lambda _value: ("test", "fernet"))
    provider = Mock(return_value={"MessageId": "mock"})
    monkeypatch.setattr(ses_client, "_ses_client", lambda _region: SimpleNamespace(send_raw_email=provider))
    monkeypatch.setattr(ses_client, "get_settings", lambda: SimpleNamespace(
        ses_from_address="desk@example.org", ses_region="us-east-1", ses_configuration_set=""))
    await delivery.dispatch_one(db, job.id, when - timedelta(seconds=1))
    provider.assert_not_called()
    await delivery.dispatch_one(db, job.id, when)
    await delivery.dispatch_one(db, job.id, when + timedelta(hours=1))
    provider.assert_called_once()
    wire = BytesParser(policy=policy.default).parsebytes(provider.call_args.kwargs["RawMessage"]["Data"])
    assert wire["Subject"] == preview["subject"] and wire.get_content_type() == "multipart/alternative"
    real_unsubscribe = wire["List-Unsubscribe"].strip("<>")
    for part, key in (("html", "body_html"), ("plain", "body_text")):
        expected = preview[key].replace("https://example.invalid/unsubscribe-preview", real_unsubscribe)
        assert wire.get_body(preferencelist=(part,)).get_content().rstrip("\n") == expected

    class AfterSchedule(datetime):
        @classmethod
        def now(cls, tz=None):
            return when + timedelta(hours=4)

    monkeypatch.setattr(marketing, "datetime", AfterSchedule)
    assert (await marketing.create_campaign(db, actor, payload))["sent"] == 1
    past_request = payload.model_copy(update={"id": uuid4()})
    with pytest.raises(HTTPException, match="future"):
        await marketing.create_campaign(db, actor, past_request)
    assert await db.get(MarketingCampaign, past_request.id) is None


async def test_fictional_preview_with_empty_directory_cannot_create_or_send(db):
    template = {"subject": "Hola {{first_name}}", "body_html": "<p>{{full_name}} de {{business_name}}</p>",
                "filters": {"locale": "es"}, "selection": {"mode": "explicit", "included_ids": []}}
    # Passing no session proves this mode reads and writes no recipient data.
    preview = await marketing.preview_campaign(None, marketing.CampaignPreview(**template, sample_mode="example"))
    assert preview["sample_id"] is None and preview["eligible_count"] == 0
    assert preview["sample_email"] == "taylor@example.invalid" and preview["subject"] == "Hola Taylor"
    assert "Taylor Morgan de Example Company" in preview["body_html"] and "Cancelar suscripción" in preview["body_text"]
    with pytest.raises(HTTPException, match="eligible recipient"):
        await marketing.preview_campaign(db, marketing.CampaignPreview(**template))
    with pytest.raises(ValidationError):
        marketing.CampaignInput(**template, name="Sample newsletter", id=uuid4(), sample_mode="example")
    with pytest.raises(HTTPException, match="No eligible"):
        await marketing.create_campaign(db, uuid4(), marketing.CampaignInput(**template, name="Newsletter", id=uuid4()))
    assert await db.scalar(select(MarketingDelivery.id)) is None


def test_schedule_rejects_ambiguous_wall_times_and_converts_explicit_offset():
    fields = dict(id=uuid4(), name="Newsletter", subject="Update", body="Message", filters={})
    with pytest.raises(ValidationError):
        marketing.CampaignInput(**fields, scheduled_at="2027-03-15T10:00:00")
    assert marketing.CampaignInput(**fields, scheduled_at="2027-03-15T10:00:00-04:00").scheduled_at == datetime(
        2027, 3, 15, 14, tzinfo=UTC)


async def test_migrated_campaign_accepts_legacy_retry_and_preserves_literal_text(db, monkeypatch):
    actor = uuid4()
    campaign = MarketingCampaign(id=uuid4(), created_by=actor, subject="Reference {{reference}}", name="Old campaign",
                                 body="A literal template example: {{first_name}}", filters={})
    db.add(campaign)
    await db.commit()
    replay = marketing.CampaignInput(id=campaign.id, name=campaign.name, subject=campaign.subject,
                                     body=campaign.body, filters={})
    assert (await marketing.create_campaign(db, actor, replay))["recipient_count"] == 0
    monkeypatch.setattr(marketing, "_secret", lambda: b"test-only")
    draft = delivery.compose(SimpleNamespace(id=uuid4(), email="borrower@example.org", locale="en"),
                             SimpleNamespace(step=None), campaign)
    assert draft.subject == campaign.subject and campaign.body in draft.body_text
