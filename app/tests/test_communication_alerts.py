"""Origin groups, legacy navigation, and explicit message-read SQL behavior."""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import JSON, MetaData, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.db import get_db
from app.dealer_os.models import DealerRepInboxMessage, DealerRepInboxThread
from app.deps import get_current_user
from app.enums import Role
from app.models.email_message import EmailMessage
from app.models.message_send import (
    MessageSend,  # noqa: F401 - resolves the existing inbox-message ORM FK
)
from app.models.notification import Notification
from app.routers import communications
from app.routers.notifications import router
from app.schemas.communication import FileCommunicationSeen
from app.services import communication_read_state as read_state
from app.services import communication_scope
from app.services.email import user_inbox_sync
from app.services.notification_groups import group_for
from app.services.notification_navigation import communication_link


class DB:
    def __init__(self, session):
        self.session = session

    async def execute(self, statement):
        return self.session.execute(statement)

    async def get(self, model, key):
        return self.session.get(model, key)

    async def flush(self):
        self.session.flush()

    async def refresh(self, row):
        self.session.refresh(row)

    async def commit(self):
        self.session.commit()


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    for model in (Notification, EmailMessage, DealerRepInboxThread, DealerRepInboxMessage):
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
    metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    yield DB(session)
    session.close()
    engine.dispose()


def notice(owner, group="sms", *, message_id=None, thread_id=None, read=False, **extra):
    thread_id = thread_id or ("sms:phone:+12015550100" if group == "sms" else "email:provider-thread")
    return Notification(id=uuid4(), recipient_user_id=owner, event_type=f"{group}_received" if group != "general" else "appointment_created",
        category="messages" if group != "general" else "calendar", priority="medium", title="Fictional alert", body="Open Inbox to review this message.",
        target_type="communication_thread" if group != "general" else "calendar_event", target_id=thread_id,
        deep_link=f"/inbox?thread={thread_id}", channels=["in_app", "email"],
        meta={"channel": group, "thread_id": thread_id, "message_id": str(message_id or uuid4()), **extra},
        read_at=datetime.now(UTC) if read else None)


def app_for(db, user):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: user

    async def database():
        yield db
        await db.commit()

    app.dependency_overrides[get_db] = database
    return app


async def test_global_group_counts_exceed_page_and_scoped_read_all_preserves_other_groups_users(db):
    user = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    other = uuid4()
    db.session.add_all([*(notice(user.id) for _ in range(85)), notice(user.id, "email"),
        notice(user.id, "email", read=True), notice(user.id, "general"), *(notice(other) for _ in range(3))])
    await db.commit()
    app = app_for(db, user)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/notifications", params={"group": "sms", "limit": 3, "offset": 84})
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 85 and len(data["items"]) == 1 and data["offset"] == 84
        assert data["unread_counts"] == {"sms": 85, "email": 1, "general": 1} and data["unread_count"] == 87
        assert data["items"][0]["group"] == "sms" and data["items"][0]["deep_link"].startswith("/messages?thread=sms")
        response = await client.post("/notifications/read-all", params={"group": "sms"})
        assert response.status_code == 204
        data = (await client.get("/notifications", params={"status": "unread"})).json()
        assert data["unread_counts"] == {"sms": 0, "email": 1, "general": 1} and data["total"] == 2
    assert all(row.read_at is None for row in db.session.query(Notification).filter_by(recipient_user_id=other))


async def test_notification_get_repairs_legacy_links_without_marking_read_or_mutating_history(db):
    user = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    row = notice(user.id)
    original_link = row.deep_link
    db.session.add(row)
    await db.commit()
    async with AsyncClient(transport=ASGITransport(app=app_for(db, user)), base_url="http://test") as client:
        data = (await client.get("/notifications")).json()
        assert data["items"][0]["body"] == "Open the conversation to review this message."
        assert data["items"][0]["deep_link"] == "/messages?thread=sms%3Aphone%3A%2B12015550100"
    assert row.read_at is None and row.deep_link == original_link and row.body == "Open Inbox to review this message."


async def test_notification_validation_and_current_recipient_isolation(db):
    user = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    other = notice(uuid4())
    db.session.add(other)
    await db.commit()
    async with AsyncClient(transport=ASGITransport(app=app_for(db, user)), base_url="http://test") as client:
        for params in ({"group": "unknown"}, {"offset": -1}, {"limit": 101}):
            assert (await client.get("/notifications", params=params)).status_code == 422
        assert (await client.post(f"/notifications/{other.id}/read")).status_code == 404
    assert other.read_at is None


@pytest.mark.parametrize("event,category,meta,expected", [("appointment_created", "calendar", {"delivery": "email"}, "general"),
    ("sms_delivery_failed", "system", {}, "sms"), ("email_received", "messages", {}, "email"),
    ("message_received", "messages", {"channel": "sms"}, "sms"), ("message_received", "messages", {"channel": "client"}, "general")])
def test_grouping_uses_message_origin_not_delivery_channel(event, category, meta, expected):
    assert group_for(event, category, meta) == expected


def test_namespace_and_exact_file_navigation_preserve_loans_and_do_not_guess_same_named_client():
    intake, thread, loan = uuid4(), uuid4(), uuid4()
    assert communication_link(f"loan:{loan}") == f"/messages?thread=loan%3A{loan}"
    assert communication_link(f"rep:{thread}", meta={"intake_id": str(intake), "channel": "email"}, role=Role.LOAN_EXEC) == f"/files/{intake}?section=communications&channel=email&thread={thread}"
    assert communication_link("sms:phone:+12015550100", meta={"intake_id": str(intake), "channel": "sms"}, role=Role.BROKER).startswith("/messages?")
    assert communication_link("unrecognized:private") is None


async def test_file_sms_counts_deduplicate_legacy_alerts_and_seen_keeps_new_arrival_other_recipient(db, monkeypatch):
    user = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    intake = SimpleNamespace(id=uuid4())
    first, later = uuid4(), uuid4()
    rows = [SimpleNamespace(id=value, provider="test", provider_message_id="", direction="inbound") for value in (first, later)]

    async def source(_db, _intake, ids=None, limit=200):
        return [row for row in rows if ids is None or row.id in ids]

    loader = AsyncMock(side_effect=source)
    monkeypatch.setattr(communication_scope, "intake_sms_rows", loader)
    records = [notice(user.id, message_id=first), notice(user.id, message_id=first), notice(user.id, message_id=later), notice(uuid4(), message_id=first)]
    db.session.add_all(records)
    await db.commit()
    before = await read_state.file_unread(db, user=user, intake=intake, profile=None)
    assert before.sms == 2 and loader.await_args.kwargs["limit"] is None
    payload = FileCommunicationSeen(channel="sms", message_ids=[first])
    after = await read_state.file_seen(db, user=user, intake=intake, profile=None, payload=payload)
    assert after.sms == 1 and records[2].read_at is None and records[3].read_at is None
    assert records[0].read_at and records[1].read_at
    with pytest.raises(HTTPException) as error:
        await read_state.file_seen(db, user=user, intake=intake, profile=None, payload=FileCommunicationSeen(channel="sms", message_ids=[uuid4()]))
    assert error.value.status_code == 404


async def test_rep_email_seen_marks_only_rendered_owner_messages_and_exact_mailbox_alias(db):
    owner, other = uuid4(), uuid4()
    thread = DealerRepInboxThread(id=uuid4(), owner_user_id=owner, subject="Fictional file", channel="email", source="test", unread_count=2)
    first = DealerRepInboxMessage(id=uuid4(), thread_id=thread.id, owner_user_id=owner, channel="email", direction="inbound", body="First", provider="gmail", provider_message_id="g-first", delivery_status="received")
    later = DealerRepInboxMessage(id=uuid4(), thread_id=thread.id, owner_user_id=owner, channel="email", direction="inbound", body="Later", provider="gmail", provider_message_id="g-later", delivery_status="received")
    alias = EmailMessage(id=uuid4(), owner_user_id=owner, mailbox="fiction@example.com", gmail_message_id="g-first", gmail_thread_id="thread-one", direction="inbound", is_read=False)
    db.session.add_all([thread, first, later, alias])
    await db.commit()
    owned_notice, raw_notice, other_notice = notice(owner, "email", message_id=first.id), notice(owner, "email", message_id=alias.id), notice(other, "email", message_id=first.id)
    db.session.add_all([owned_notice, raw_notice, other_notice])
    await db.commit()
    await read_state.mark_rep_messages_seen(db, thread=thread, user_id=other, rows=[first])
    assert first.read_at is None and alias.is_read is False and other_notice.read_at
    await read_state.mark_rep_messages_seen(db, thread=thread, user_id=owner, rows=[first])
    assert first.read_at and later.read_at is None and thread.unread_count == 1
    assert alias.is_read and owned_notice.read_at and raw_notice.read_at


async def test_provider_id_reused_in_two_mailboxes_cannot_mark_ambiguous_file_mirror(db):
    owner = uuid4()
    thread = DealerRepInboxThread(id=uuid4(), owner_user_id=owner, subject="Same subject", channel="email", source="test", unread_count=1)
    message = DealerRepInboxMessage(id=uuid4(), thread_id=thread.id, owner_user_id=owner, channel="email", direction="inbound", body="Ambiguous provider source", provider="gmail", provider_message_id="same-gmail", delivery_status="received")
    alias = EmailMessage(id=uuid4(), owner_user_id=owner, mailbox="fiction@example.com", gmail_message_id="same-gmail", direction="inbound", is_read=False)
    other_mailbox = EmailMessage(id=uuid4(), owner_user_id=owner, mailbox="other@example.com", gmail_message_id="same-gmail", direction="inbound", is_read=False)
    db.session.add_all([thread, message, alias, other_mailbox])
    await db.commit()
    await read_state.mark_mailbox_messages_seen(db, user_id=owner, rows=[alias])
    assert alias.is_read and other_mailbox.is_read is False and message.read_at is None


@pytest.mark.parametrize("mirrored", [True, False])
async def test_mailbox_ingestion_emits_no_second_alert_for_its_file_mirror(monkeypatch, mirrored):
    db = SimpleNamespace(add=lambda row: None, flush=AsyncMock())
    monkeypatch.setattr(user_inbox_sync, "_headers", lambda _detail: {"from": "client@example.com", "subject": "Fictional reply", "to": "owner@example.com"})
    monkeypatch.setattr(user_inbox_sync, "_encrypt_body", lambda body: ("encrypted", "test"))
    monkeypatch.setattr(user_inbox_sync._poller, "_extract_body", lambda payload: "Fictional body")
    monkeypatch.setattr(user_inbox_sync._poller, "_walk_attachments", lambda payload: [])
    monkeypatch.setattr(user_inbox_sync, "match_inbound", AsyncMock(return_value=SimpleNamespace(loan_id=None, client_id=None, party_role=None)))
    monkeypatch.setattr(user_inbox_sync, "_mirror_file_email_reply", AsyncMock(return_value=mirrored))
    monkeypatch.setattr("app.services.email.prospect_reply.ingest_synced_reply", AsyncMock())
    notify = AsyncMock()
    monkeypatch.setattr(user_inbox_sync, "notify_inbound_communication", notify)
    await user_inbox_sync._ingest_message(db, owner_user_id=uuid4(), mailbox="owner@example.com", gmail_id="fictional-id", detail={"threadId": "fictional-thread", "payload": {}})
    assert notify.await_count == (0 if mirrored else 1)


@pytest.mark.parametrize("provider_known", [True, False])
async def test_file_email_matching_never_chooses_first_of_ambiguous_sources(monkeypatch, provider_known):
    candidates = [SimpleNamespace(participant_emails=["client@example.com"], provider_thread_id=None) for _ in range(2)]
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: candidates))
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    append = AsyncMock()
    monkeypatch.setattr("app.dealer_os.router._append_rep_inbox_message", append)
    matched = await user_inbox_sync._mirror_file_email_reply(db, from_email="client@example.com", mailbox="owner@example.com",
        subject="Same subject", body="Source body", gmail_id="source-id", gmail_thread_id="source-thread" if provider_known else None, owner_user_id=uuid4())
    assert matched is False
    append.assert_not_awaited()


async def test_direct_loan_alert_resolves_outside_recent_thread_scan_with_existing_loan_scope(monkeypatch):
    loan_id, user_id = uuid4(), uuid4()
    loan = SimpleNamespace(id=loan_id, deal_id="Fictional loan", updated_at=datetime.now(UTC), created_at=datetime.now(UTC))
    monkeypatch.setattr(communications, "_all_threads", AsyncMock(return_value=[]))
    query = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: loan))
    user = SimpleNamespace(id=user_id, role=Role.LOAN_EXEC)
    result = await communications._thread_summary(SimpleNamespace(execute=query), user, f"loan:{loan_id}")
    assert result.id == f"loan:{loan_id}" and result.source_id == str(loan_id)
    assert loan_id in query.await_args.args[0].compile().params.values()
    query.return_value = SimpleNamespace(scalar_one_or_none=lambda: None)
    with pytest.raises(HTTPException) as error:
        await communications._thread_summary(SimpleNamespace(execute=query), user, f"loan:{uuid4()}")
    assert error.value.status_code == 404
