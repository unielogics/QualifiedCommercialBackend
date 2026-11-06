"""Real-SQL page counts, file isolation, and explicit source-read regressions."""
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import JSON, Column, MetaData, String, Table, Uuid, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.db import get_db
from app.dealer_os.models import DealerRepInboxMessage, DealerRepInboxThread
from app.deps import get_current_user
from app.enums import Role
from app.models.email_message import EmailMessage
from app.models.notification import Notification
from app.models.sms_message import SmsMessage
from app.routers import communications
from app.schemas.communication import FileCommunicationSeen
from app.services import communication_read_state as read_state
from app.services.file_communication_unread import unread_batch


class DB:
    def __init__(self, session, tables):
        self.session, self.tables, self.statements = session, tables, []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.session.execute(statement)

    async def get(self, model, key):
        return self.session.get(model, key)

    async def flush(self):
        self.session.flush()


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def functions(connection, _record):
        connection.create_function("regexp_replace", 4, lambda value, pattern, replacement, _flags: re.sub(pattern, replacement, value or ""))

    metadata = MetaData()
    for model in (SmsMessage, Notification, EmailMessage, DealerRepInboxThread, DealerRepInboxMessage):
        table = model.__table__.to_metadata(metadata)
        for constraint in list(table.foreign_key_constraints):
            table.constraints.remove(constraint)
        table.foreign_keys.clear()
        for column in table.columns:
            column.foreign_keys.clear()
            if isinstance(column.type, JSONB):
                column.type = JSON()
            if column.server_default is not None and "::" in str(column.server_default.arg):
                column.server_default = None
    Table("public_underwriting_intakes", metadata, Column("id", Uuid, primary_key=True),
          Column("bucket_id", Uuid), Column("phone", String), Column("client_id", Uuid))
    Table("application_profiles", metadata, Column("id", Uuid, primary_key=True), Column("intake_id", Uuid),
          Column("primary_bucket_id", Uuid), Column("dealer_id", Uuid), Column("client_id", Uuid))
    metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    try:
        yield DB(session, metadata.tables)
    finally:
        session.close()
        engine.dispose()


def intake(db, phone="+12015550100", *, profile=True):
    row = SimpleNamespace(id=uuid4(), bucket_id=uuid4(), phone=phone, client_id=uuid4())
    db.session.execute(db.tables["public_underwriting_intakes"].insert().values(**vars(row)))
    row.profile = None
    if profile:
        row.profile = SimpleNamespace(id=uuid4())
        db.session.execute(db.tables["application_profiles"].insert().values(id=row.profile.id,
            intake_id=row.id, primary_bucket_id=row.bucket_id, client_id=row.client_id))
    return row


def sms(db, target, *, bound=True, **extra):
    row = SmsMessage(**{"id": uuid4(), "direction": "inbound", "phone_e164": target.phone,
        "provider": "aws", "provider_message_id": str(uuid4()), "status": "received", "body": "Fictional text",
        "intake_id": target.id if bound else None, "client_id": target.client_id, **extra})
    db.session.add(row)
    db.session.flush()
    return row


def notice(db, owner, message, *, read=False):
    row = Notification(id=uuid4(), recipient_user_id=owner, event_type="sms_received", category="messages",
        priority="medium", title="Fictional text", body="Open the conversation", target_type="communication_thread",
        target_id="sms:phone:+12015550100", channels=["in_app"], meta={"message_id": str(message.id)},
        read_at=datetime.now(UTC) if read else None)
    db.session.add(row)
    db.session.flush()
    return row


def email(db, target, owner, *, read=False, direction="inbound"):
    thread = DealerRepInboxThread(id=uuid4(), profile_id=target.profile.id, owner_user_id=owner,
        channel="email", source="test", subject="Fictional file")
    message = DealerRepInboxMessage(id=uuid4(), thread_id=thread.id, owner_user_id=owner, channel="email",
        direction=direction, body="Fictional reply", provider="gmail", provider_message_id=str(uuid4()),
        delivery_status="received", read_at=datetime.now(UTC) if read else None)
    db.session.add_all([thread, message])
    db.session.flush()
    return message


@pytest.mark.asyncio
async def test_batch_matches_single_counts_with_aliases_owner_scope_and_no_read_side_effects(db):
    user = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    a, b = intake(db), intake(db, "+12015550101")
    source = sms(db, a)
    notices = [notice(db, user.id, source), notice(db, user.id, source), notice(db, uuid4(), source)]
    mirror_thread = DealerRepInboxThread(id=uuid4(), profile_id=a.profile.id, owner_user_id=user.id,
        channel="sms", source="test", subject="Text source")
    mirror = DealerRepInboxMessage(id=uuid4(), thread_id=mirror_thread.id, profile_id=a.profile.id,
        owner_user_id=user.id, channel="sms", direction="inbound", sender=a.phone, body="Same text",
        provider=source.provider, provider_message_id=source.provider_message_id, delivery_status="received")
    db.session.add_all([mirror_thread, mirror])
    notices.append(notice(db, user.id, mirror))
    legacy = sms(db, a, bound=False)
    notices.append(notice(db, user.id, legacy))
    for ignored in (sms(db, a, direction="outbound"), sms(db, a, client_id=b.client_id),
                    sms(db, a, profile_id=b.profile.id)):
        notices.append(notice(db, user.id, ignored))
    noticed_b = sms(db, b)
    notices.append(notice(db, user.id, noticed_b, read=True))
    owned_mail = email(db, a, user.id)
    email(db, a, user.id, read=True)
    email(db, a, user.id, direction="outbound")
    email(db, a, uuid4())
    email(db, b, uuid4())

    counts = await unread_batch(db, user=user, intake_ids=[b.id, a.id, a.id])
    assert [row.intake_id for row in counts] == [b.id, a.id]
    assert [(row.sms, row.email, row.total) for row in counts] == [(0, 0, 0), (2, 1, 3)]
    for target, count in ((b, counts[0]), (a, counts[1])):
        single = await read_state.file_unread(db, user=user, intake=target, profile=target.profile)
        assert (single.sms, single.email, single.total) == (count.sms, count.email, count.total)
    assert sum(row.read_at is not None for row in notices) == 1
    assert owned_mail.read_at is None and mirror.read_at is None


@pytest.mark.asyncio
async def test_counts_after_explicit_seen_keep_later_arrivals_and_other_users_unread(db):
    user = SimpleNamespace(id=uuid4(), role=Role.SUPER_ADMIN)
    target = intake(db)
    first, later = sms(db, target), sms(db, target)
    notices = [notice(db, user.id, first), notice(db, user.id, later), notice(db, uuid4(), first)]
    first_mail, later_mail = email(db, target, user.id), email(db, target, user.id)
    before = await unread_batch(db, user=user, intake_ids=[target.id])
    assert before[0].sms == 2
    await read_state.file_seen(db, user=user, intake=target, profile=target.profile,
        payload=FileCommunicationSeen(channel="sms", message_ids=[first.id]))
    after = await unread_batch(db, user=user, intake_ids=[target.id])
    assert after[0].sms == 1 and notices[0].read_at is not None
    assert notices[1].read_at is None and notices[2].read_at is None
    await read_state.file_seen(db, user=user, intake=target, profile=target.profile,
        payload=FileCommunicationSeen(channel="email", thread_id=first_mail.thread_id, message_ids=[first_mail.id]))
    final = (await unread_batch(db, user=user, intake_ids=[target.id]))[0]
    assert (final.sms, final.email, final.total) == (1, 1, 2)
    assert first_mail.read_at is not None and later_mail.read_at is None


@pytest.mark.asyncio
async def test_shared_phone_outside_visible_page_blocks_fallback_and_stale_profile_mail(db):
    user = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    target = intake(db)
    intake(db, target.phone)
    unbound = sms(db, target, bound=False)
    notice(db, user.id, unbound)
    wrong = uuid4()
    db.session.execute(db.tables["application_profiles"].update().where(
        db.tables["application_profiles"].c.id == target.profile.id).values(primary_bucket_id=wrong))
    email(db, target, user.id)
    result = (await unread_batch(db, user=user, intake_ids=[target.id]))[0]
    assert result.sms == 0 and result.email == 0 and result.profile_id is None


@pytest.mark.asyncio
async def test_page_query_count_is_constant_and_includes_old_messages_beyond_display_cap(db):
    user = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    target = intake(db)
    messages = [sms(db, target) for _ in range(205)]
    notice(db, user.id, messages[0])
    for count in (1, 50):
        extra = [intake(db, f"+1201555{index:04}") for index in range(1, count)]
        db.statements.clear()
        result = await unread_batch(db, user=user, intake_ids=[target.id, *(row.id for row in extra)])
        assert result[0].sms == 1 and all(row.total == 0 for row in result[1:])
        assert len(db.statements) == 6


@pytest.mark.asyncio
async def test_batch_authorizes_every_requested_file_before_counting_and_empty_is_read_only(db):
    user = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    target = intake(db)
    assert await unread_batch(db, user=user, intake_ids=[]) == []
    assert not db.statements
    with pytest.raises(HTTPException) as missing:
        await unread_batch(db, user=user, intake_ids=[target.id, uuid4()])
    assert missing.value.status_code == 404 and len(db.statements) == 1
    db.statements.clear()
    for role in (Role.CLIENT, Role.BROKER, Role.FIELD_REP, Role.REGIONAL_MANAGER):
        with pytest.raises(HTTPException) as denied:
            await unread_batch(db, user=SimpleNamespace(id=user.id, role=role), intake_ids=[target.id])
        assert denied.value.status_code == 403
    assert not db.statements


@pytest.mark.asyncio
async def test_http_batch_validates_maximum_ids_and_returns_explicit_zero_counts(db):
    target = intake(db, profile=False)
    app = FastAPI()
    app.include_router(communications.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)

    async def database():
        yield db

    app.dependency_overrides[get_db] = database
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for payload in ({"intake_ids": [str(uuid4()) for _ in range(101)]}, {"intake_ids": ["invalid"]}):
            assert (await client.post("/communications/intakes/unread-batch", json=payload)).status_code == 422
        response = await client.post("/communications/intakes/unread-batch", json={"intake_ids": [str(target.id)]})
        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["sms"] == item["email"] == item["total"] == 0 and item["as_of"]
        assert (await client.post("/communications/intakes/unread-batch", json={"intake_ids": []})).json() == {"items": []}
