"""Every file has a client, and a client reads back as its files and messages.

Nearly every way a file is opened already found or made a client record, each
with its own copy of the rule. A foreclosure rescue never did, and a file the
desk opened without one was handed a brand-new record even when the person was
already in the book. These tests pin the one rule (`ensure_client`), the repair
that applies it to files that predate it (`sweep`), and what the client screen
reads back: the files that belong to a client and every text and email about
them, with nobody else's mixed in.

The real ORM runs against an on-disk SQLite copy of the schema (foreign keys
left out of the DDL; only the queries matter here).
"""

from __future__ import annotations

import asyncio
import re
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.db import Base
from app.dealer_os.models import DealerRepInboxMessage, DealerRepInboxThread
from app.enums import Role
from app.models.application_profile import ApplicationProfile
from app.models.bucket import Bucket, BucketFile, BucketRequestedDocument
from app.models.client import Client
from app.models.file_event import FileEvent
from app.models.message_send import MessageSend
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.sms_message import SmsMessage
from app.models.user import User
from app.routers import clients as clients_router
from app.services import file_clients as fc

NOW = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(_type, _compiler, **_kwargs):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _sqlite_array(_type, _compiler, **_kwargs):
    return "JSON"


def _sqlite_functions(dbapi_connection, _record) -> None:
    # The phone identity expression is written for Postgres.
    dbapi_connection.create_function(
        "regexp_replace", 4, lambda value, pattern, repl, _flags: re.sub(pattern, repl, value or "")
    )


async def _create_schema(path) -> None:
    import app.models  # noqa: F401  (registers every table)

    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    metadata = sa.MetaData()
    for table in Base.metadata.tables.values():
        clone = table.to_metadata(metadata)
        for constraint in list(clone.foreign_key_constraints):
            clone.constraints.discard(constraint)
        for column in clone.columns:
            column.foreign_keys.clear()
            if isinstance(column.type, PG_UUID):
                column.type = sa.String(36)
    for table in list(metadata.tables.values()):
        for column in table.columns:
            if column.server_default is not None:
                literal = str(getattr(column.server_default, "arg", ""))
                if "::jsonb" in literal:
                    column.server_default = sa.DefaultClause(sa.text(literal.replace("::jsonb", "")))
                elif "now()" in literal:
                    column.server_default = sa.DefaultClause(sa.text("CURRENT_TIMESTAMP"))
                elif "gen_random_uuid" in literal:
                    column.server_default = None
        for index in table.indexes:
            condition = index.dialect_options["postgresql"].get("where")
            if condition is not None:
                index.dialect_options["sqlite"]["where"] = condition
        async with engine.begin() as connection:
            try:
                await connection.run_sync(lambda sync, current=table: metadata.create_all(sync, tables=[current]))
            except Exception:  # noqa: BLE001  a few tables use Postgres-only functions; none are used here
                pass
    await engine.dispose()


@pytest.fixture(scope="module")
def template_database(tmp_path_factory):
    path = tmp_path_factory.mktemp("file_clients") / "template.db"
    asyncio.run(_create_schema(path))
    return path


@pytest.fixture
async def db(template_database, tmp_path):
    copy = tmp_path / "test.db"
    shutil.copy(template_database, copy)
    engine = create_async_engine(f"sqlite+aiosqlite:///{copy}")
    event.listen(engine.sync_engine, "connect", _sqlite_functions)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
    await engine.dispose()


# ── builders ────────────────────────────────────────────────────────────────


async def _file(
    db,
    *,
    full_name="Dana Ruiz",
    email="dana@ruizauto.com",
    phone="+17025550101",
    business="Ruiz Auto Group",
    variant="dealer_gatekeeper_v1",
    client=None,
    archived=False,
    with_profile=True,
    status="collecting",
    underwriting_status="collecting_docs",
    created_at=None,
    **extra,
):
    bucket = Bucket(name=f"{business} room", archived_at=NOW if archived else None)
    db.add(bucket)
    await db.flush()
    intake = PublicUnderwritingIntake(
        bucket_id=bucket.id,
        token_hash=uuid.uuid4().hex,
        full_name=full_name,
        email=email,
        phone=phone,
        business_name=business,
        variant=variant,
        status=status,
        client_id=client.id if client else None,
        intake_state=extra.pop("intake_state", {"messages": []}),
        **extra,
    )
    if created_at is not None:
        intake.created_at = created_at
        intake.updated_at = created_at
    db.add(intake)
    await db.flush()
    profile = None
    if with_profile:
        profile = ApplicationProfile(
            intake_id=intake.id,
            client_id=client.id if client else None,
            primary_bucket_id=bucket.id,
            underwriting_status=underwriting_status,
        )
        db.add(profile)
        await db.flush()
    return intake, bucket, profile


async def _client(db, name="Dana Ruiz", email="dana@ruizauto.com", **fields):
    row = Client(name=name, email=email, **fields)
    db.add(row)
    await db.flush()
    return row


async def _user(db, role=Role.SUPER_ADMIN, email=None):
    row = User(email=email or f"{uuid.uuid4().hex[:8]}@qc.test", role=role, name="Desk User")
    db.add(row)
    await db.flush()
    return row


async def _clients(db) -> list[Client]:
    return list((await db.execute(select(Client).order_by(Client.created_at))).scalars().all())


def _rescue_state(**overrides):
    details = {
        "submitter_type": "attorney",
        "contact_name": "Lena Park, Esq.",
        "contact_email": "lena@parklaw.com",
        "borrower_name": "Marcus Hale",
        "client_email": "Marcus@HaleHoldings.com",
        "client_phone": "+13055550144",
        "holding_entity": "Hale Holdings LLC",
    }
    details.update(overrides)
    return {"messages": [], "foreclosure_rescue": details}


# ── who the client is ───────────────────────────────────────────────────────


def test_the_person_on_the_file_is_the_client() -> None:
    intake = SimpleNamespace(
        full_name="  Dana   Ruiz ", email=" Dana@RuizAuto.com ", phone="+1 702 555 0101",
        business_name="Ruiz Auto Group", intake_state={}, client_contact_suppressed=False,
    )
    identity = fc.identity_for(intake)
    assert identity == fc.ClientIdentity(
        name="Dana Ruiz", email="dana@ruizauto.com", phone="+1 702 555 0101",
        business_name="Ruiz Auto Group", direct=True,
    )


def test_a_professional_who_submits_a_rescue_is_not_the_client() -> None:
    intake = SimpleNamespace(
        full_name="Lena Park, Esq.", email="lena@parklaw.com", phone="+12125550100",
        business_name="Hale Holdings LLC", intake_state=_rescue_state(), client_contact_suppressed=True,
    )
    identity = fc.identity_for(intake)
    assert identity.name == "Marcus Hale"
    assert identity.email == "marcus@haleholdings.com"
    assert identity.phone == "+13055550144"
    assert identity.direct is False


def test_an_unnamed_owner_is_known_by_the_holding_entity() -> None:
    intake = SimpleNamespace(
        full_name="Lena Park, Esq.", email="lena@parklaw.com", phone=None, business_name="Hale Holdings LLC",
        intake_state=_rescue_state(borrower_name="", client_email=None), client_contact_suppressed=True,
    )
    identity = fc.identity_for(intake)
    assert identity.name == "Hale Holdings LLC"
    assert identity.email is None


def test_a_blank_email_is_no_email() -> None:
    assert fc.clean_email("") is None
    assert fc.clean_email("   ") is None
    assert fc.clean_email("not-an-address") is None
    assert fc.clean_email(" A@B.co ") == "a@b.co"


def test_each_kind_of_file_names_its_channel() -> None:
    assert fc.source_channel_for(SimpleNamespace(variant="mca_refi_v1")) == "mca_refinance"
    assert fc.source_channel_for(SimpleNamespace(variant="commercial_foreclosure_bailout_v1")) == "foreclosure_rescue"
    assert fc.source_channel_for(SimpleNamespace(variant="something_new")) == "ai_intake"


# ── the rule ────────────────────────────────────────────────────────────────


async def test_a_file_with_no_client_is_given_a_new_record(db) -> None:
    intake, _bucket, profile = await _file(db)

    ensured = await fc.ensure_client(db, intake)

    assert ensured.outcome == "created"
    client = ensured.client
    assert (client.name, client.email, client.phone) == ("Dana Ruiz", "dana@ruizauto.com", "+17025550101")
    assert client.source_channel == "dealer_ai_intake"
    assert client.contact_permission is None
    assert client.lead_intake["business_name"] == "Ruiz Auto Group"
    assert client.lead_intake["created_from_file"] == str(intake.id)
    assert intake.client_id == client.id
    assert profile.client_id == client.id


async def test_a_person_already_in_the_book_is_found_not_duplicated(db) -> None:
    existing = await _client(db, name="Dana R.", email="Dana@RuizAuto.com", phone=None)
    intake, _bucket, _profile = await _file(db)

    ensured = await fc.ensure_client(db, intake)

    assert ensured.outcome == "linked"
    assert ensured.client.id == existing.id
    assert len(await _clients(db)) == 1
    # What the record lacked is filled; what somebody entered is kept.
    assert existing.phone == "+17025550101"
    assert existing.name == "Dana R."


async def test_a_record_named_by_its_email_takes_the_real_name(db) -> None:
    existing = await _client(db, name="dana@ruizauto.com")
    intake, _bucket, _profile = await _file(db)

    await fc.ensure_client(db, intake)

    assert existing.name == "Dana Ruiz"


async def test_among_records_sharing_an_email_the_same_kind_of_file_wins(db) -> None:
    older_same_kind = await _client(db, source_channel="dealer_ai_intake")
    older_same_kind.created_at = NOW - timedelta(days=30)
    newer_other = await _client(db, source_channel="funding_review")
    newer_other.created_at = NOW - timedelta(days=1)
    await db.flush()
    intake, _bucket, _profile = await _file(db)

    ensured = await fc.ensure_client(db, intake)

    assert ensured.client.id == older_same_kind.id


async def test_with_no_record_of_the_same_kind_the_newest_wins(db) -> None:
    older = await _client(db, source_channel="mca_funding")
    older.created_at = NOW - timedelta(days=30)
    newer = await _client(db, source_channel="funding_review")
    newer.created_at = NOW - timedelta(days=1)
    await db.flush()
    intake, _bucket, _profile = await _file(db)

    assert (await fc.ensure_client(db, intake)).client.id == newer.id


async def test_a_file_that_has_its_client_keeps_it(db) -> None:
    mine = await _client(db, name="Dana Ruiz")
    await _client(db, name="Somebody Else")  # same email, newer
    intake, bucket, _profile = await _file(db, client=mine, with_profile=False)
    # A profile opened before the file had a client still says it has none.
    profile = ApplicationProfile(intake_id=intake.id, primary_bucket_id=bucket.id)
    db.add(profile)
    await db.flush()

    ensured = await fc.ensure_client(db, intake)

    assert ensured.outcome == "kept"
    assert ensured.client.id == mine.id
    assert profile.client_id == mine.id


async def test_a_shared_phone_or_name_never_joins_two_people(db) -> None:
    first, _b1, _p1 = await _file(db, email="", full_name="Dana Ruiz", phone="+17025550101")
    second, _b2, _p2 = await _file(db, email="", full_name="Dana Ruiz", phone="+17025550101")

    one = await fc.ensure_client(db, first)
    two = await fc.ensure_client(db, second)

    assert one.outcome == two.outcome == "created"
    assert one.client.id != two.client.id
    assert one.client.email is None


async def test_the_rule_is_safe_to_run_twice(db) -> None:
    intake, _bucket, _profile = await _file(db)
    first = await fc.ensure_client(db, intake)
    second = await fc.ensure_client(db, intake)
    assert second.outcome == "kept"
    assert second.client.id == first.client.id
    assert len(await _clients(db)) == 1


async def test_a_rescue_sent_by_an_attorney_makes_the_owner_the_client(db) -> None:
    # The attorney is in the book for a file of their own. They are not this file's client.
    attorney = await _client(db, name="Lena Park", email="lena@parklaw.com")
    intake, _bucket, profile = await _file(
        db, full_name="Lena Park, Esq.", email="lena@parklaw.com", business="Hale Holdings LLC",
        variant="commercial_foreclosure_bailout_v1", client_contact_suppressed=True, intake_state=_rescue_state(),
    )

    ensured = await fc.ensure_client(db, intake)

    assert ensured.outcome == "created"
    owner = ensured.client
    assert owner.id != attorney.id
    assert (owner.name, owner.email, owner.phone) == ("Marcus Hale", "marcus@haleholdings.com", "+13055550144")
    assert owner.source_channel == "foreclosure_rescue"
    # The firm may not reach out to an owner somebody else submitted for.
    assert owner.contact_permission == fc.NO_DIRECT_CONTACT
    assert profile.client_id == owner.id


async def test_an_owner_who_submits_their_own_rescue_is_the_client(db) -> None:
    intake, _bucket, _profile = await _file(
        db, full_name="Marcus Hale", email="marcus@haleholdings.com",
        variant="commercial_foreclosure_bailout_v1", client_contact_suppressed=False,
        intake_state=_rescue_state(submitter_type="owner_direct"),
    )
    owner = (await fc.ensure_client(db, intake)).client
    assert owner.name == "Marcus Hale"
    assert owner.contact_permission is None


# ── the repair ──────────────────────────────────────────────────────────────


async def test_the_repair_gives_every_live_file_its_client_and_skips_what_was_thrown_away(db) -> None:
    known = await _client(db, name="Known Person", email="known@example.com")
    stranger, _b1, stranger_profile = await _file(db, email="new@example.com", full_name="New Person")
    returning, _b2, _p2 = await _file(db, email="known@example.com", full_name="Known Person")
    archived, _b3, _p3 = await _file(db, email="gone@example.com", archived=True)
    flagged, _b4, _p4 = await _file(db, email="flagged@example.com", delete_requested_at=NOW)
    already, _b5, _p5 = await _file(db, email="known@example.com", client=known)

    assert await fc.count_unlinked(db) == 2
    report = await fc.sweep(db)

    assert report == {"checked": 2, "linked": 1, "created": 1, "failed": 0, "profiles": 0}
    assert stranger.client_id is not None and stranger.client_id != known.id
    assert stranger_profile.client_id == stranger.client_id
    assert returning.client_id == known.id
    assert archived.client_id is None
    assert flagged.client_id is None
    assert already.client_id == known.id
    assert await fc.count_unlinked(db) == 0
    # A second pass finds nothing to do.
    assert await fc.sweep(db) == {"checked": 0, "linked": 0, "created": 0, "failed": 0, "profiles": 0}
    assert len(await _clients(db)) == 2


async def test_the_repair_brings_a_stale_profile_along(db) -> None:
    client = await _client(db)
    intake, bucket, _none = await _file(db, client=client, with_profile=False)
    profile = ApplicationProfile(intake_id=intake.id, primary_bucket_id=bucket.id)
    db.add(profile)
    await db.flush()

    report = await fc.sweep(db)

    assert report["profiles"] == 1
    assert profile.client_id == client.id


async def test_one_file_that_cannot_be_repaired_does_not_undo_the_others(db, monkeypatch) -> None:
    good, _b1, _p1 = await _file(db, email="good@example.com", created_at=NOW - timedelta(days=2))
    bad, _b2, _p2 = await _file(db, email="bad@example.com", created_at=NOW - timedelta(days=1))
    real = fc.ensure_client

    async def flaky(session, intake):
        if intake.id == bad.id:
            raise RuntimeError("cannot read this one")
        return await real(session, intake)

    monkeypatch.setattr(fc, "ensure_client", flaky)
    report = await fc.sweep(db)

    assert report["created"] == 1 and report["failed"] == 1
    assert good.client_id is not None
    assert bad.client_id is None


# ── a client's files ────────────────────────────────────────────────────────


async def test_a_client_reads_back_as_its_files_newest_first(db) -> None:
    client = await _client(db)
    other = await _client(db, name="Other", email="other@example.com")
    first, first_bucket, first_profile = await _file(
        db, client=client, business="Ruiz Auto Group", created_at=NOW - timedelta(days=9),
        underwriting_status="in_underwriting",
    )
    second, _b2, _p2 = await _file(
        db, client=client, business="Ruiz Leasing", created_at=NOW - timedelta(days=2), archived=True,
    )
    await _file(db, client=other, business="Not theirs")
    asked = BucketRequestedDocument(bucket_id=first_bucket.id, name="Bank statements", required=True, status="requested")
    answered = BucketRequestedDocument(bucket_id=first_bucket.id, name="Tax return", required=True, status="uploaded")
    db.add_all([asked, answered])
    db.add_all([
        BucketFile(bucket_id=first_bucket.id, file_name="tax.pdf", s3_key="k/tax.pdf", status="uploaded"),
        BucketFile(bucket_id=first_bucket.id, file_name="old.pdf", s3_key="k/old.pdf", status="uploaded", deleted_at=NOW),
    ])
    await db.flush()
    client_id, first_id, first_profile_id, second_id = client.id, first.id, first_profile.id, second.id
    db.expire_all()  # read it back the way a new request would

    files = await fc.files_for_client(db, client_id)

    assert [row["business_name"] for row in files] == ["Ruiz Leasing", "Ruiz Auto Group"]
    archived, live = files
    assert archived["workspace_state"] == "archived"
    assert live["workspace_state"] == "active"
    assert live["id"] == first_id and live["profile_id"] == first_profile_id
    assert live["underwriting_status"] == "in_underwriting"
    assert live["file_count"] == 1  # the removed document is not counted
    assert live["missing_required_count"] == 1
    assert archived["id"] == second_id


async def test_a_client_with_no_files_reads_back_empty(db) -> None:
    client = await _client(db)
    assert await fc.files_for_client(db, client.id) == []


# ── a client's messages ─────────────────────────────────────────────────────


async def test_messages_are_gathered_from_the_client_and_from_each_of_their_files(db) -> None:
    viewer = await _user(db, Role.SUPER_ADMIN)
    client = await _client(db, phone="+1 (702) 555-0101")
    other = await _client(db, name="Other", email="other@example.com", phone="+17025550199")
    intake, _bucket, profile = await _file(db, client=client)
    other_intake, _ob, other_profile = await _file(db, client=other, email="other@example.com", business="Other Co")

    def text(body, minutes, **binding):
        row = SmsMessage(direction=binding.pop("direction", "outbound"), phone_e164=binding.pop("phone", "+17025550101"),
                         status=binding.pop("status", "delivered"), body=body, **binding)
        row.created_at = NOW - timedelta(minutes=minutes)
        return row

    db.add_all([
        text("bound to the client", 50, client_id=client.id),
        text("bound to the file", 40, intake_id=intake.id, direction="inbound", status="received"),
        text("bound to the profile", 30, profile_id=profile.id),
        text("on their number, filed nowhere", 20),
        text("somebody else's", 10, client_id=other.id, phone="+17025550199"),
        text("somebody else's file", 5, intake_id=other_intake.id, phone="+17025550199"),
    ])
    sent = MessageSend(channel="email", direction="outbound", status="delivered", to_email="dana@ruizauto.com",
                       subject="Your secure room link", context="room_link", intake_id=intake.id,
                       body_text_enc="ciphertext", actor_user_id=viewer.id)
    sent.created_at = NOW - timedelta(minutes=25)
    theirs = MessageSend(channel="email", direction="outbound", status="sent", to_email="other@example.com",
                         subject="Not this client's", profile_id=other_profile.id)
    theirs.created_at = NOW - timedelta(minutes=2)
    thread = DealerRepInboxThread(subject="Re: documents", profile_id=profile.id, channel="email")
    db.add_all([sent, theirs, thread])
    await db.flush()
    reply = DealerRepInboxMessage(thread_id=thread.id, profile_id=profile.id, direction="inbound", channel="email",
                                  body="Here are the statements you asked for.", sender="dana@ruizauto.com")
    reply.created_at = NOW - timedelta(minutes=15)
    db.add(reply)
    await db.flush()

    result = await fc.communications_for_client(db, client, viewer=viewer)

    assert [item["snippet"] or item["subject"] for item in result["items"]] == [
        "Here are the statements you asked for.",
        "on their number, filed nowhere",
        "Your secure room link",
        "bound to the profile",
        "bound to the file",
        "bound to the client",
    ]
    assert result["counts"] == {"sms": 4, "email": 2}
    assert result["file_count"] == 1
    by_id = {item["id"]: item for item in result["items"]}
    email = by_id[f"send:{sent.id}"]
    assert email["file_title"] == "Ruiz Auto Group" and email["intake_id"] == str(intake.id)
    assert email["snippet"] is None  # a sent email's body is opened through the log, not copied here
    assert email["can_open"] is True
    inbound = by_id[f"reply:{reply.id}"]
    assert inbound["direction"] == "inbound" and inbound["subject"] == "Re: documents"
    assert inbound["counterpart"] == "dana@ruizauto.com"
    assert inbound["file_title"] == "Ruiz Auto Group"


async def test_a_number_two_clients_share_proves_nothing(db) -> None:
    viewer = await _user(db)
    client = await _client(db, phone="+17025550101")
    await _client(db, name="Housemate", email="mate@example.com", phone="(702) 555-0101")
    db.add(SmsMessage(direction="inbound", phone_e164="+17025550101", status="received", body="who is this for"))
    await db.flush()

    result = await fc.communications_for_client(db, client, viewer=viewer)

    assert result["items"] == []


async def test_an_email_is_opened_only_by_whoever_the_log_would_let_read_it(db) -> None:
    admin = await _user(db, Role.SUPER_ADMIN)
    sender = await _user(db, Role.LOAN_EXEC)
    colleague = await _user(db, Role.LOAN_EXEC)
    client = await _client(db)
    intake, _bucket, _profile = await _file(db, client=client)
    db.add_all([
        MessageSend(channel="email", status="sent", subject="mine", intake_id=intake.id,
                    body_text_enc="x", actor_user_id=sender.id),
        MessageSend(channel="email", status="sent", subject="no body kept", intake_id=intake.id,
                    actor_user_id=sender.id),
    ])
    await db.flush()

    async def openable(viewer):
        result = await fc.communications_for_client(db, client, viewer=viewer)
        return {item["subject"]: item["can_open"] for item in result["items"]}

    assert await openable(admin) == {"mine": True, "no body kept": False}
    assert await openable(sender) == {"mine": True, "no body kept": False}
    assert await openable(colleague) == {"mine": False, "no body kept": False}


# ── the desk's view ─────────────────────────────────────────────────────────


async def test_the_summary_counts_live_files_and_where_they_stand(db) -> None:
    client = await _client(db)
    await _file(db, client=client, created_at=NOW - timedelta(days=2), underwriting_status="collecting_docs")
    in_review, in_review_bucket, _p = await _file(
        db, client=client, created_at=NOW - timedelta(days=40), underwriting_status="in_underwriting"
    )
    db.add(BucketFile(bucket_id=in_review_bucket.id, file_name="a.pdf", s3_key="k/a.pdf", status="uploaded"))
    await _file(db, created_at=NOW - timedelta(days=3), with_profile=False)  # no profile yet, no client
    await _file(db, client=client, created_at=NOW - timedelta(days=1), archived=True)
    await _file(db, client=client, created_at=NOW - timedelta(days=1), status="denied")
    await _file(db, client=client, created_at=NOW - timedelta(days=90), underwriting_status="closed_won")
    await db.flush()

    summary = await fc.desk_summary(db, now=NOW)

    assert summary["active"] == 4  # archived and denied files have left the desk; a funded one has not
    assert summary["new_7d"] == 2
    assert summary["no_documents"] == 3
    assert summary["without_client"] == 1
    stages = {row["key"]: row["count"] for row in summary["stages"]}
    assert stages == {
        "submitted": 1, "collecting_docs": 1, "in_underwriting": 1,
        "term_sheet_provided": 0, "approved": 0, "closed_won": 1,
    }
    assert [row["key"] for row in summary["stages"]] == list(fc.LIFECYCLE_ORDER)


async def test_activity_names_the_file_and_the_client_and_filters_by_kind(db) -> None:
    client = await _client(db)
    intake, _bucket, profile = await _file(db, client=client)
    no_intake = ApplicationProfile(underwriting_status="collecting_docs")  # a file that never came through an intake
    db.add(no_intake)
    await db.flush()

    def line(kind, title, minutes, target=None, **fields):
        return FileEvent(profile_id=(target or profile).id, kind=kind, visibility=fields.pop("visibility", "client"),
                         title=title, created_at=NOW - timedelta(minutes=minutes), **fields)

    db.add_all([
        line("document.received", "Bank statement received", 30, actor_label="Dana Ruiz"),
        line("message.sent", "Dana sent a message", 20),
        line("status.changed", "Moved to underwriting", 10, visibility="desk"),
        line("document.received", "Not an intake file", 5, target=no_intake),
    ])
    await db.flush()

    everything = await fc.desk_activity(db)
    assert [item["title"] for item in everything] == [
        "Moved to underwriting", "Dana sent a message", "Bank statement received",
    ]
    newest = everything[0]
    assert newest["intake_id"] == str(intake.id)
    assert newest["file_title"] == "Ruiz Auto Group"
    assert newest["client_id"] == str(client.id) and newest["client_name"] == "Dana Ruiz"
    assert [item["group"] for item in everything] == ["status", "messages", "documents"]

    documents = await fc.desk_activity(db, group="documents")
    assert [item["title"] for item in documents] == ["Bank statement received"]
    older = await fc.desk_activity(db, before=NOW - timedelta(minutes=15))
    assert [item["title"] for item in older] == ["Dana sent a message", "Bank statement received"]
    assert len(await fc.desk_activity(db, limit=1)) == 1


def test_timeline_kinds_fall_into_the_desks_groups() -> None:
    assert fc.activity_group("document.requested") == "documents"
    assert fc.activity_group("requirement.added") == "documents"
    assert fc.activity_group("forms.packet_sent") == "documents"
    assert fc.activity_group("message.sent") == "messages"
    assert fc.activity_group("offer.answered") == "messages"
    assert fc.activity_group("review.completed") == "status"
    assert fc.activity_group("something.else") == "other"


# ── who may read it ─────────────────────────────────────────────────────────


async def test_the_routes_are_a_desk_view(db) -> None:
    client = await _client(db)
    await _file(db, client=client)
    desk = SimpleNamespace(id=uuid.uuid4(), role=Role.LOAN_EXEC, client=None, broker=None)

    files = await clients_router.list_client_files(client.id, desk, db)
    assert [row.business_name for row in files.items] == ["Ruiz Auto Group"]
    messages = await clients_router.list_client_communications(client.id, desk, db)
    assert messages.items == [] and messages.file_count == 1

    for role in (Role.BROKER, Role.CLIENT, Role.FIELD_REP, Role.DEALER_PARTNER):
        outsider = SimpleNamespace(id=uuid.uuid4(), role=role, client=None, broker=None)
        with pytest.raises(HTTPException) as refused:
            await clients_router.list_client_files(client.id, outsider, db)
        assert refused.value.status_code == 403
        with pytest.raises(HTTPException) as refused:
            await clients_router.list_client_communications(client.id, outsider, db)
        assert refused.value.status_code == 403

    with pytest.raises(HTTPException) as missing:
        await clients_router.list_client_files(uuid.uuid4(), desk, db)
    assert missing.value.status_code == 404


# ── the paths that open a file ──────────────────────────────────────────────


async def test_a_rescue_file_is_opened_with_the_owner_as_its_client(db) -> None:
    from app.routers import foreclosure_rescue
    from app.tests.test_foreclosure_rescue import _intake as rescue_payload

    actor = await _user(db, Role.SUPER_ADMIN)
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})

    created = await foreclosure_rescue._create_foreclosure_rescue(
        rescue_payload(), request, db, actor=actor, send_resume_email=False
    )

    intake = await db.get(PublicUnderwritingIntake, created.id)
    assert intake.client_id is not None
    client = await db.get(Client, intake.client_id)
    # The attorney sent it in; the owner they named is the client.
    assert (client.name, client.email) == ("Owner Guarantor", "client@example.com")
    assert client.contact_permission == fc.NO_DIRECT_CONTACT
    profile = (
        await db.execute(select(ApplicationProfile).where(ApplicationProfile.intake_id == intake.id))
    ).scalar_one()
    assert profile.client_id == client.id


async def test_the_desk_opening_a_file_finds_the_person_already_in_the_book(db) -> None:
    from app.services import application_profiles

    existing = await _client(db)
    intake, _bucket, _none = await _file(db, with_profile=False)
    desk = await _user(db, Role.LOAN_EXEC)

    profile = await application_profiles.resolve_profile(db, "intake", intake.id, desk)

    assert intake.client_id == existing.id
    assert profile.client_id == existing.id
    assert len(await _clients(db)) == 1


def test_the_repair_runs_on_the_scheduler_every_half_hour() -> None:
    import inspect

    from app.services import scheduler

    source = inspect.getsource(scheduler.start_scheduler)
    assert 'id="file_client_sweep"' in source
    assert "run_scheduled_sweep" in inspect.getsource(scheduler.job_file_client_sweep)


async def test_the_desk_routes_refuse_everyone_but_the_desk(db) -> None:
    from app.routers import operator_files

    client = await _client(db)
    intake, _bucket, profile = await _file(db, client=client)
    db.add(FileEvent(profile_id=profile.id, kind="document.received", visibility="client",
                     title="Bank statement received", created_at=NOW))
    await db.flush()
    desk = SimpleNamespace(id=uuid.uuid4(), role=Role.SUPER_ADMIN)

    summary = await operator_files.get_intake_summary(desk, db)
    assert summary["active"] == 1 and summary["without_client"] == 0
    page = await operator_files.get_file_activity(desk, db, group=None, before=None, limit=1)
    assert [item["title"] for item in page["items"]] == ["Bank statement received"]
    assert page["next_before"] is not None  # a full page says where the next one starts
    assert (await operator_files.get_file_activity(desk, db, group=None, before=None, limit=40))["next_before"] is None
    assert page["items"][0]["intake_id"] == str(intake.id)

    for role in (Role.BROKER, Role.CLIENT, Role.FIELD_REP, Role.DEALER_PARTNER):
        outsider = SimpleNamespace(id=uuid.uuid4(), role=role)
        with pytest.raises(HTTPException) as refused:
            await operator_files.get_intake_summary(outsider, db)
        assert refused.value.status_code == 403
        with pytest.raises(HTTPException) as refused:
            await operator_files.get_file_activity(outsider, db, group=None, before=None, limit=40)
        assert refused.value.status_code == 403


async def test_a_file_reads_its_own_suggestions_past_the_cross_room_cap(db) -> None:
    """The retired inbox's queue is now answered on the file too. One room's
    suggestions must not be lost behind the oldest two hundred of every room."""
    from app.models.bucket import BucketAIActionItem
    from app.routers import buckets

    mine, mine_bucket, _p1 = await _file(db, business="Ruiz Auto Group")
    _other, other_bucket, _p2 = await _file(db, business="Other Co", email="other@example.com")
    await _file(db, business="Thrown away", email="gone@example.com", archived=True)
    db.add_all([
        BucketAIActionItem(bucket_id=other_bucket.id, title=f"Older {n}", instructions="x", status="proposed",
                           route="admin", created_at=NOW - timedelta(days=40, minutes=n))
        for n in range(3)
    ])
    db.add_all([
        BucketAIActionItem(bucket_id=mine_bucket.id, title="Ask for the 2025 return", instructions="x",
                           status="proposed", route="admin", created_at=NOW - timedelta(days=2)),
        BucketAIActionItem(bucket_id=mine_bucket.id, title="Already decided", instructions="x",
                           status="approved", route="admin", created_at=NOW - timedelta(days=3)),
    ])
    await db.flush()

    across = await buckets.list_ai_action_items_across_buckets(
        item_status="proposed", limit=3, bucket_id=None, _=SimpleNamespace(), db=db
    )
    assert [row.title for row in across] == ["Older 2", "Older 1", "Older 0"]  # oldest first; the cap hides this file's

    own = await buckets.list_ai_action_items_across_buckets(
        item_status="proposed", limit=3, bucket_id=mine_bucket.id, _=SimpleNamespace(), db=db
    )
    assert [row.title for row in own] == ["Ask for the 2025 return"]
    assert own[0].intake_id == mine.id
    assert own[0].approvable_here is True
