"""Inbound SMS identity is exact and abstains when the phone is shared."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import JSON, MetaData, select
from sqlalchemy.dialects.postgresql import JSONB

from app.dealer_os.models import DealerRepContact, DealerRepInboxMessage, DealerRepInboxThread
from app.models.client import Client
from app.models.message_send import MessageSend  # noqa: F401 - resolves existing inbox ORM FK
from app.models.sms_message import SmsMessage
from app.routers import webhooks
from app.services.sms import ledger, optout
from app.tests import test_communication_sms_scope as sms_scope_tests

PHONE = "+12015550100"


@pytest.fixture
def db():
    yield from sms_scope_tests.db.__wrapped__()


@pytest.fixture
def inbound(db, monkeypatch):
    metadata = MetaData()
    for model in (DealerRepInboxThread, DealerRepInboxMessage):
        table = model.__table__.to_metadata(metadata)
        for constraint in list(table.foreign_key_constraints):
            table.constraints.remove(constraint)
        table.foreign_keys.clear()
        for column in table.columns:
            column.foreign_keys.clear()
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(db.session.get_bind())
    db.add = db.session.add
    db.flush = AsyncMock(side_effect=db.session.flush)
    db.commit = AsyncMock(side_effect=db.session.commit)

    class Context:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return False

    admin, agent = uuid4(), uuid4()
    monkeypatch.setattr(webhooks, "SessionLocal", Context)
    monkeypatch.setattr(webhooks, "users_with_roles", AsyncMock(return_value=[SimpleNamespace(id=admin)]))
    monkeypatch.setattr(webhooks, "client_agent_user_ids", AsyncMock(return_value={agent}))
    monkeypatch.setattr(webhooks, "notify_inbound_communication", AsyncMock())
    monkeypatch.setattr(webhooks, "publish_communication_event", AsyncMock())
    monkeypatch.setattr(webhooks, "_store_inbound_attachments", AsyncMock())
    monkeypatch.setattr(optout, "record_opt_out", AsyncMock())
    return SimpleNamespace(db=db, admin=admin, agent=agent)


def client(db, *, phone=PHONE, name="Unique client"):
    row = Client(id=uuid4(), name=name, phone=phone)
    db.session.add(row)
    db.session.flush()
    return row


def contact(db, *, phone=PHONE, owner=None, name="Rep contact"):
    row = DealerRepContact(id=uuid4(), full_name=name, phone_e164=phone, owner_user_id=owner or uuid4())
    db.session.add(row)
    db.session.flush()
    return row


async def receive(body="A private reply"):
    return await webhooks._store_inbound_sms(provider="android", provider_id=None, from_phone=PHONE,
        to_phone="+12015550999", body=body)


@pytest.mark.asyncio
async def test_unique_canonical_client_match_keeps_formatting_variants(db):
    own = client(db, phone="(201) 555-0100")
    assert await ledger.client_for_phone(db, PHONE) is own


@pytest.mark.asyncio
async def test_foreign_number_with_same_suffix_is_not_client_identity(db):
    client(db, phone="+442015550100")
    assert await ledger.client_for_phone(db, PHONE) is None


@pytest.mark.asyncio
async def test_two_clients_with_same_canonical_phone_never_choose_newest(db):
    client(db)
    client(db, phone="201-555-0100", name="Newest duplicate")
    assert await ledger.client_for_phone(db, PHONE) is None
    assert len(await ledger.client_candidates_for_phone(db, PHONE)) == 2


@pytest.mark.asyncio
async def test_invalid_phone_does_not_query_or_claim_client(db):
    client(db)
    db.statements.clear()
    assert await ledger.client_for_phone(db, "") is None
    assert db.statements == []


@pytest.mark.asyncio
async def test_shared_rep_contacts_keep_unbound_ledger_and_generic_admin_alert(inbound):
    db = inbound.db
    client(db)
    first = contact(db, name="First unrelated contact")
    other = contact(db, phone="(201) 555-0100", name="Second unrelated contact")
    response = await receive()
    row = db.session.execute(select(SmsMessage)).scalar_one()
    assert response.status_code == 202 and row.client_id is None and row.profile_id is None and row.intake_id is None
    assert db.session.execute(select(DealerRepInboxMessage)).scalars().all() == []
    kwargs = webhooks.notify_inbound_communication.await_args.kwargs
    assert kwargs["recipient_ids"] == {inbound.admin}
    assert kwargs["sender_label"] == PHONE and kwargs["message_id"] == str(row.id)
    assert first.owner_user_id not in kwargs["recipient_ids"] and other.owner_user_id not in kwargs["recipient_ids"]
    webhooks.client_agent_user_ids.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_clients_do_not_route_into_single_rep_contact(inbound):
    db = inbound.db
    client(db)
    client(db, phone="2015550100")
    contact(db)
    await receive()
    assert db.session.execute(select(SmsMessage)).scalar_one().client_id is None
    assert db.session.execute(select(DealerRepInboxThread)).scalars().all() == []
    assert webhooks.notify_inbound_communication.await_args.kwargs["recipient_ids"] == {inbound.admin}
    assert webhooks.notify_inbound_communication.await_args.kwargs["sender_label"] == PHONE


@pytest.mark.asyncio
async def test_unambiguous_contact_and_client_preserve_owner_mirror_and_notifications(inbound):
    db = inbound.db
    own_client = client(db, phone="2015550100")
    own_contact = contact(db, phone="(201) 555-0100")
    response = await receive()
    assert response.status_code == 204
    assert db.session.execute(select(SmsMessage)).scalar_one().client_id == own_client.id
    mirror = db.session.execute(select(DealerRepInboxMessage)).scalar_one()
    assert mirror.owner_user_id == own_contact.owner_user_id and mirror.contact_id == own_contact.id
    kwargs = webhooks.notify_inbound_communication.await_args.kwargs
    assert kwargs["recipient_ids"] == {own_contact.owner_user_id, inbound.agent}
    assert kwargs["sender_label"] == own_contact.full_name


@pytest.mark.asyncio
async def test_shared_phone_stop_applies_optout_to_all_but_does_not_assign_sender(inbound):
    db = inbound.db
    first = contact(db)
    other = contact(db, phone="2015550100")
    response = await receive("STOP")
    assert response.status_code == 204
    assert first.sms_opted_out_at is not None and other.sms_opted_out_at is not None
    optout.record_opt_out.assert_awaited_once()
    assert db.session.execute(select(DealerRepInboxMessage)).scalars().all() == []
    kwargs = webhooks.notify_inbound_communication.await_args.kwargs
    assert kwargs["recipient_ids"] == {inbound.admin} and kwargs["sender_label"] == PHONE
    assert kwargs["subject"] == "Opt-out request received"


@pytest.mark.asyncio
async def test_unique_client_without_rep_contact_still_alerts_its_agent(inbound):
    own = client(inbound.db)
    response = await receive()
    assert response.status_code == 202
    assert inbound.db.session.execute(select(SmsMessage)).scalar_one().client_id == own.id
    kwargs = webhooks.notify_inbound_communication.await_args.kwargs
    assert kwargs["recipient_ids"] == {inbound.agent} and kwargs["sender_label"] == own.name
