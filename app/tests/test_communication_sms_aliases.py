"""Provider IDs alone never authorize clearing a different SMS/file alert."""
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import JSON, Column, MetaData, Table, Uuid, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.dealer_os.models import DealerRepInboxMessage, DealerRepInboxThread
from app.models.message_send import MessageSend  # noqa: F401 - resolves existing inbox ORM FK
from app.services.communication_read_state import sms_alias_map

PHONE = "+12015550100"


class DB:
    def __init__(self, session):
        self.session = session

    async def execute(self, statement):
        return self.session.execute(statement)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    for model in (DealerRepInboxThread, DealerRepInboxMessage):
        table = model.__table__.to_metadata(metadata)
        # Historical/corrupt duplicate provider rows must fail closed too.
        table.indexes.clear()
        for constraint in list(table.foreign_key_constraints):
            table.constraints.remove(constraint)
        table.foreign_keys.clear()
        for column in table.columns:
            column.foreign_keys.clear()
            if isinstance(column.type, JSONB):
                column.type = JSON()
    Table("application_profiles", metadata, Column("id", Uuid, primary_key=True), Column("intake_id", Uuid),
          Column("primary_bucket_id", Uuid), Column("client_id", Uuid), Column("dealer_id", Uuid))
    Table("public_underwriting_intakes", metadata, Column("id", Uuid, primary_key=True), Column("bucket_id", Uuid))
    metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        result = DB(session)
        result.tables = metadata.tables
        yield result
    engine.dispose()


def source(*, phone=PHONE, provider_id="reused-local-id", **bindings):
    return SimpleNamespace(id=uuid4(), provider="android", provider_message_id=provider_id, phone_e164=phone,
        direction="inbound", profile_id=None, intake_id=None, client_id=None, **bindings)


def ledger(*, phone=PHONE, provider_id="reused-local-id", **bindings):
    row = source(phone=phone, provider_id=provider_id)
    for key, value in bindings.items():
        setattr(row, key, value)
    return row


def thread(db, **bindings):
    row = DealerRepInboxThread(id=uuid4(), owner_user_id=uuid4(), subject="Exact file", channel="sms", source="test", **bindings)
    db.session.add(row)
    db.session.flush()
    return row


def mirror(db, target, *, phone=PHONE, provider_id="reused-local-id", **bindings):
    row = DealerRepInboxMessage(id=uuid4(), thread_id=target.id, owner_user_id=target.owner_user_id,
        direction="inbound", channel="sms", body="Private reply", sender=phone, provider="android",
        provider_message_id=provider_id, delivery_status="received", created_at=datetime.now(UTC), **bindings)
    db.session.add(row)
    db.session.flush()
    return row


def profile(db, *, intake_id=None, bucket_id=None, primary_bucket_id=None, client_id=None):
    intake_id, bucket_id = intake_id or uuid4(), bucket_id or uuid4()
    row = SimpleNamespace(id=uuid4(), intake_id=intake_id, bucket_id=bucket_id, client_id=client_id)
    db.session.execute(db.tables["public_underwriting_intakes"].insert().values(id=intake_id, bucket_id=bucket_id))
    db.session.execute(db.tables["application_profiles"].insert().values(id=row.id, intake_id=intake_id,
        primary_bucket_id=primary_bucket_id or bucket_id, client_id=client_id))
    return row


@pytest.mark.asyncio
async def test_exact_provider_phone_and_single_context_alias_same_physical_text(db):
    sms = ledger()
    rep = mirror(db, thread(db), phone="(201) 555-0100")
    aliases = await sms_alias_map(db, [sms])
    assert aliases[str(sms.id)] == aliases[str(rep.id)]


@pytest.mark.asyncio
async def test_provider_id_reused_by_different_phones_never_aliases_or_deduplicates(db):
    first, other = ledger(), ledger(phone="+12015550200")
    rep = mirror(db, thread(db), phone="+12015550200")
    aliases = await sms_alias_map(db, [first, other])
    assert aliases[str(first.id)] != aliases[str(other.id)]
    assert aliases[str(other.id)] == aliases[str(rep.id)]
    assert str(rep.id) not in await sms_alias_map(db, [first])


@pytest.mark.asyncio
async def test_international_phone_suffix_is_not_same_source(db):
    sms = ledger()
    rep = mirror(db, thread(db), phone="+442015550100")
    assert str(rep.id) not in await sms_alias_map(db, [sms])


@pytest.mark.asyncio
async def test_explicit_source_profile_cannot_clear_different_file_mirror(db):
    own, other = profile(db), profile(db)
    sms = ledger(profile_id=own.id)
    rep = mirror(db, thread(db, profile_id=other.id))
    assert str(rep.id) not in await sms_alias_map(db, [sms])


@pytest.mark.asyncio
async def test_matching_profile_and_intake_anchor_aliases(db):
    own = profile(db, client_id=uuid4())
    sms = ledger(profile_id=own.id, intake_id=own.intake_id, client_id=own.client_id)
    rep = mirror(db, thread(db, profile_id=own.id))
    aliases = await sms_alias_map(db, [sms])
    assert aliases[str(sms.id)] == aliases[str(rep.id)]


@pytest.mark.asyncio
async def test_intake_binding_rejects_profile_anchored_to_wrong_bucket(db):
    own = profile(db, primary_bucket_id=uuid4())
    rep = mirror(db, thread(db, profile_id=own.id))
    assert str(rep.id) not in await sms_alias_map(db, [ledger(intake_id=own.intake_id)])


@pytest.mark.asyncio
async def test_explicit_client_binding_cannot_clear_another_client_mirror(db):
    own = profile(db, client_id=uuid4())
    rep = mirror(db, thread(db, profile_id=own.id))
    assert str(rep.id) not in await sms_alias_map(db, [ledger(client_id=uuid4())])


@pytest.mark.asyncio
async def test_multiple_mirror_threads_remain_ambiguous_even_same_phone_provider(db):
    first, second = mirror(db, thread(db)), mirror(db, thread(db))
    aliases = await sms_alias_map(db, [ledger()])
    assert str(first.id) not in aliases and str(second.id) not in aliases


@pytest.mark.asyncio
async def test_message_profile_contradicting_thread_is_never_alias(db):
    own, other = profile(db), profile(db)
    rep = mirror(db, thread(db, profile_id=own.id), profile_id=other.id)
    assert str(rep.id) not in await sms_alias_map(db, [ledger(profile_id=own.id)])


@pytest.mark.asyncio
async def test_bound_source_does_not_alias_unbound_mirror(db):
    rep = mirror(db, thread(db))
    assert str(rep.id) not in await sms_alias_map(db, [ledger(intake_id=uuid4())])


@pytest.mark.asyncio
async def test_same_provider_phone_but_distinct_source_files_stay_distinct(db):
    own, other = ledger(intake_id=uuid4()), ledger(intake_id=uuid4())
    aliases = await sms_alias_map(db, [own, other])
    assert aliases[str(own.id)] != aliases[str(other.id)]


@pytest.mark.asyncio
async def test_missing_phone_or_provider_never_claims_other_source(db):
    first, other = ledger(phone=""), ledger(provider_id="")
    rep = mirror(db, thread(db))
    aliases = await sms_alias_map(db, [first, other])
    assert aliases == {str(first.id): str(first.id), str(other.id): str(other.id)}
    assert str(rep.id) not in aliases
