"""Real SQL regressions for guessed/shared-phone and per-file SMS isolation."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import JSON, Column, MetaData, String, Table, Uuid, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.dealer_os.models import DealerRepContact
from app.enums import Role
from app.models.client import Client
from app.models.sms_message import SmsMessage
from app.routers import communications
from app.schemas.communication import UnifiedCommunicationThread
from app.services import communication_scope as scope

PHONE = "+15551234567"


class LocalDB:
    def __init__(self, session):
        self.session = session
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.session.execute(statement)

    async def get(self, model, key):
        return self.session.get(model, key)


@pytest.fixture
def db():
    import re

    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def functions(connection, _record):
        connection.create_function("regexp_replace", 4, lambda value, pattern, replacement, _flags: re.sub(pattern, replacement, value or ""))

    metadata = MetaData()
    # Keep actual ORM message/client/contact columns and bind processors; only
    # adapt PostgreSQL JSON/FKs in this disposable in-memory test database.
    for model in (Client, DealerRepContact, SmsMessage):
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
    Table("application_profiles", metadata, Column("id", Uuid, primary_key=True), Column("intake_id", Uuid),
          Column("primary_bucket_id", Uuid), Column("dealer_id", Uuid), Column("client_id", Uuid))
    Table("public_underwriting_intakes", metadata, Column("id", Uuid, primary_key=True), Column("bucket_id", Uuid), Column("phone", String), Column("client_id", Uuid))
    Table("dos_dealers", metadata, Column("id", Uuid, primary_key=True), Column("owner_user_id", Uuid))
    metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    local = LocalDB(session)
    local.tables = metadata.tables
    try:
        yield local
    finally:
        session.close()
        engine.dispose()


def user(role=Role.BROKER):
    return SimpleNamespace(id=uuid4(), role=role, broker=SimpleNamespace(id=uuid4()), client=None)


def sms(db, *, phone=PHONE, client_id=None, **kwargs):
    row = SmsMessage(id=uuid4(), direction="inbound", phone_e164=phone, body="Private text",
        provider="aws", provider_message_id=str(uuid4()), status="received", detail="", context="reply", client_id=client_id,
        created_at=datetime.now(UTC), **kwargs)
    db.session.add(row)
    db.session.flush()
    return row


@pytest.mark.asyncio
async def test_guessed_other_book_phone_cannot_open_sms_thread(db, monkeypatch):
    viewer = user()
    other = Client(id=uuid4(), name="Other book", phone=PHONE, broker_id=uuid4())
    db.session.add(other)
    db.session.flush()
    sms(db, client_id=other.id)
    monkeypatch.setattr(communications, "_all_threads", AsyncMock(return_value=[]))
    with pytest.raises(HTTPException) as error:
        await communications._thread_summary(db, viewer, f"sms:phone:{PHONE}")
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_shared_phone_detail_reapplies_broker_row_scope(db, monkeypatch):
    viewer = user()
    own = Client(id=uuid4(), name="Own client", phone=PHONE, broker_id=viewer.broker.id)
    other = Client(id=uuid4(), name="Other client", phone=PHONE, broker_id=uuid4())
    db.session.add_all([own, other])
    db.session.flush()
    visible = sms(db, client_id=own.id)
    sms(db, client_id=other.id)
    sms(db)
    summary = UnifiedCommunicationThread(id=f"sms:phone:{PHONE}", title="Shared number", participant_phone=PHONE,
        participant_type="client", source_kind="sms", source_id=PHONE, channel="sms", transport="sms",
        latest_at=datetime.now(UTC), href="/messages")
    monkeypatch.setattr(communications, "_thread_summary", AsyncMock(return_value=summary))
    monkeypatch.setattr(communications.inline_images, "hydrate", AsyncMock(return_value={}))
    # Exercise the real GET query branch; authorization of a phone summary is
    # insufficient to authorize every message ever sent to that shared number.
    response = await communications.get_unified_communication_thread(f"sms:phone:{PHONE}", viewer, db)
    assert [row.id for row in response.messages] == [str(visible.id)]
    rows = (await db.execute(await scope.scoped_sms_query(db, viewer, PHONE))).scalars().all()
    assert [row.id for row in rows] == [visible.id]
    hydrate_ids = communications.inline_images.hydrate.await_args.args[2]
    assert hydrate_ids == [str(visible.id)]


@pytest.mark.asyncio
async def test_duplicate_client_phone_never_resolves_to_newest_visible_client(db):
    viewer = user()
    db.session.add_all([Client(id=uuid4(), name="First", phone=PHONE, broker_id=viewer.broker.id),
                        Client(id=uuid4(), name="Second", phone="(555) 123-4567", broker_id=uuid4())])
    db.session.flush()
    assert await communications._client_for_phone_scoped(db, viewer, PHONE) is None


@pytest.mark.asyncio
async def test_field_rep_shared_phone_or_contrary_file_binding_does_not_grant_access(db):
    viewer = user(Role.FIELD_REP)
    db.session.add(DealerRepContact(id=uuid4(), owner_user_id=viewer.id, full_name="Own contact", phone_e164=PHONE))
    db.session.flush()
    unbound = sms(db)
    sms(db, client_id=uuid4())
    sms(db, intake_id=uuid4())
    rows = (await db.execute(await scope.scoped_sms_query(db, viewer, PHONE))).scalars().all()
    assert [row.id for row in rows] == [unbound.id]
    db.session.add(DealerRepContact(id=uuid4(), owner_user_id=uuid4(), full_name="Other owner", phone_e164="5551234567"))
    db.session.flush()
    assert (await db.execute(await scope.scoped_sms_query(db, viewer, PHONE))).scalars().all() == []
    assert await communications._rep_contact_for_phone(db, viewer, PHONE) is None


@pytest.mark.asyncio
async def test_field_rep_can_read_explicit_own_file_but_not_another_profile_or_contrary_intake(db):
    viewer = user(Role.FIELD_REP)
    dealer, other_dealer, own_profile, other_profile, intake, bucket = [uuid4() for _ in range(6)]
    db.session.execute(db.tables["dos_dealers"].insert(), [{"id": dealer, "owner_user_id": viewer.id}, {"id": other_dealer, "owner_user_id": uuid4()}])
    db.session.execute(db.tables["public_underwriting_intakes"].insert(), {"id": intake, "bucket_id": bucket, "phone": PHONE})
    db.session.execute(db.tables["application_profiles"].insert(), [
        {"id": own_profile, "dealer_id": dealer, "intake_id": intake, "primary_bucket_id": bucket},
        {"id": other_profile, "dealer_id": other_dealer, "intake_id": None, "primary_bucket_id": uuid4()}])
    direct_profile = sms(db, profile_id=own_profile)
    direct_intake = sms(db, intake_id=intake)
    sms(db, profile_id=other_profile)
    sms(db, profile_id=own_profile, intake_id=uuid4())
    sms(db, profile_id=own_profile, client_id=uuid4())
    rows = (await db.execute(await scope.scoped_sms_query(db, viewer, PHONE))).scalars().all()
    assert {row.id for row in rows} == {direct_profile.id, direct_intake.id}


@pytest.mark.asyncio
async def test_empty_contact_phone_never_grants_unattributed_ledger_access(db):
    viewer = user(Role.FIELD_REP)
    db.session.add(DealerRepContact(id=uuid4(), owner_user_id=viewer.id, full_name="No phone", phone_e164=None))
    db.session.flush()
    sms(db, phone="")
    assert (await db.execute(await scope.scoped_sms_query(db, viewer))).scalars().all() == []


@pytest.mark.asyncio
async def test_international_suffix_is_not_the_same_number(db):
    viewer = user(Role.SUPER_ADMIN)
    visible = sms(db)
    sms(db, phone="+495551234567")
    rows = (await db.execute(await scope.scoped_sms_query(db, viewer, PHONE))).scalars().all()
    assert [row.id for row in rows] == [visible.id]
    assert scope.canonical_phone("(555) 123-4567") == PHONE
    assert scope.canonical_phone("+4923456789") == "+4923456789"


@pytest.mark.asyncio
async def test_file_scope_prefers_explicit_binding_and_omits_shared_phone_fallback(db):
    intake = SimpleNamespace(id=uuid4(), bucket_id=uuid4(), phone=PHONE, client_id=None)
    other_id, other_bucket, profile_id, other_profile = uuid4(), uuid4(), uuid4(), uuid4()
    db.session.execute(db.tables["public_underwriting_intakes"].insert(), [
        {"id": intake.id, "bucket_id": intake.bucket_id, "phone": PHONE},
        {"id": other_id, "bucket_id": other_bucket, "phone": "5551234567"}])
    db.session.execute(db.tables["application_profiles"].insert(), [
        {"id": profile_id, "intake_id": intake.id, "primary_bucket_id": intake.bucket_id},
        {"id": other_profile, "intake_id": other_id, "primary_bucket_id": other_bucket}])
    by_intake = sms(db, intake_id=intake.id)
    by_profile = sms(db, profile_id=profile_id)
    sms(db)
    sms(db, intake_id=other_id)
    sms(db, intake_id=intake.id, profile_id=other_profile)
    sms(db, profile_id=profile_id, intake_id=other_id)
    rows = await scope.intake_sms_rows(db, intake)
    assert {row.id for row in rows} == {by_intake.id, by_profile.id}


@pytest.mark.asyncio
async def test_unique_file_phone_fallback_excludes_contrary_client_and_profile(db):
    intake = SimpleNamespace(id=uuid4(), bucket_id=uuid4(), phone=PHONE, client_id=None)
    db.session.execute(db.tables["public_underwriting_intakes"].insert(), {"id": intake.id, "bucket_id": intake.bucket_id, "phone": PHONE})
    legacy = sms(db, phone="5551234567")
    sms(db, profile_id=uuid4())
    sms(db, client_id=uuid4())
    assert [row.id for row in await scope.intake_sms_rows(db, intake)] == [legacy.id]


@pytest.mark.asyncio
async def test_file_history_uses_latest200_but_read_scope_can_count_all_and_select_old_visible_ids(db):
    intake = SimpleNamespace(id=uuid4(), bucket_id=uuid4(), phone=PHONE, client_id=None)
    start = datetime.now(UTC) - timedelta(days=1)
    rows = [SmsMessage(id=uuid4(), direction="inbound", phone_e164=PHONE, intake_id=intake.id,
        status="received", body=str(index), created_at=start + timedelta(seconds=index)) for index in range(205)]
    db.session.add_all(rows)
    db.session.flush()
    shown = await scope.intake_sms_rows(db, intake)
    assert len(shown) == 200 and shown[0].body == "5" and shown[-1].body == "204"
    assert len(await scope.intake_sms_rows(db, intake, limit=None)) == 205
    assert [row.id for row in await scope.intake_sms_rows(db, intake, ids=[rows[0].id])] == [rows[0].id]


@pytest.mark.asyncio
async def test_sms_list_window_includes_newest_messages_and_canonical_unread_count(db, monkeypatch):
    viewer = user(Role.SUPER_ADMIN)
    start = datetime.now(UTC) - timedelta(days=1)
    db.session.add_all([SmsMessage(id=uuid4(), direction="inbound", phone_e164=PHONE, status="received",
        body=str(index), created_at=start + timedelta(seconds=index)) for index in range(communications.SCAN_LIMIT + 2)])
    db.session.flush()
    unread = AsyncMock(return_value={PHONE: 3})
    monkeypatch.setattr("app.services.communication_read_state.sms_unread_by_phone", unread)
    threads = await communications._sms_threads(db, viewer)
    assert len(threads) == 1 and threads[0].id == f"sms:phone:{PHONE}"
    assert threads[0].latest_snippet == str(communications.SCAN_LIMIT + 1)
    assert threads[0].unread_count == 3
    unread.assert_awaited_once()
