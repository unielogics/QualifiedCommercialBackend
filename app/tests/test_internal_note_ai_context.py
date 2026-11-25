"""Internal-note context choices persist without creating messages or widening access."""
from datetime import UTC, datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.enums import Role
from app.models.bucket import Bucket, BucketNote
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.routers import communications
from app.schemas.communication import (
    InternalNoteAIContextUpdate,
    UnifiedCommunicationCompose,
    UnifiedCommunicationThread,
)
from app.services.internal_notes import included_in_ai


@pytest.fixture
def store(monkeypatch):
    metadata = sa.MetaData()
    buckets = sa.Table("buckets", metadata, sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("archived_at", sa.DateTime(timezone=True)))
    intakes = sa.Table("public_underwriting_intakes", metadata, sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("bucket_id", sa.Uuid()))
    notes = BucketNote.__table__.to_metadata(metadata)
    for constraint in list(notes.foreign_key_constraints):
        notes.constraints.remove(constraint)
    notes.foreign_keys.clear()
    for column in notes.columns:
        column.foreign_keys.clear()
    engine = sa.create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    intake_id, bucket_id = uuid4(), uuid4()
    session.execute(buckets.insert().values(id=bucket_id))
    session.execute(intakes.insert().values(id=intake_id, bucket_id=bucket_id))
    session.commit()
    thread_id = f"intake:{intake_id}:internal"

    class DB:
        add = session.add

        async def execute(self, statement):
            return session.execute(statement)

        async def commit(self):
            session.commit()

        async def get(self, model, identifier):
            table = intakes if model is PublicUnderwritingIntake else buckets if model is Bucket else None
            assert table is not None
            row = session.execute(sa.select(table).where(table.c.id == identifier)).mappings().one_or_none()
            return SimpleNamespace(**dict(row)) if row else None

    async def summary(_db, _user, requested, **_kwargs):
        return UnifiedCommunicationThread(id=requested, title="Fictional file", participant_type="internal", source_kind="intake",
            source_id=str(intake_id), channel=requested.split(":")[-1], transport="portal", latest_at=datetime.now(UTC), href="/files/fictional")

    event = AsyncMock()
    monkeypatch.setattr(communications, "_thread_summary", summary)
    monkeypatch.setattr(communications.file_events, "emit", event)
    yield SimpleNamespace(db=DB(), session=session, intake_id=intake_id, bucket_id=bucket_id, thread_id=thread_id,
        buckets=buckets, intakes=intakes, notes=notes, event=event,
        user=SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC, name="Fictional reviewer", email="reviewer@example.com"))
    session.close()
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [Role.SUPER_ADMIN, Role.LOAN_EXEC])
async def test_internal_note_defaults_off_then_toggle_persists_on_the_same_message(store, role):
    store.user.role = role
    payload = UnifiedCommunicationCompose(body="Fictional internal context")
    result = await communications.reply_unified_communication_thread(store.thread_id, payload, None, store.user, store.db)
    assert len(result.messages) == 1
    message = result.messages[0]
    assert message.include_in_ai_context is False
    original_id = UUID(message.id)
    created_at = message.created_at
    for selected in (True, True, False):
        result = await communications.update_internal_note_ai_context(store.thread_id, original_id,
            InternalNoteAIContextUpdate(include_in_ai_context=selected), store.user, store.db)
        assert len(result.messages) == 1 and result.messages[0].id == str(original_id)
        assert result.messages[0].created_at == created_at
        assert result.messages[0].include_in_ai_context is selected
        note = store.session.get(BucketNote, original_id)
        assert note.content == "Fictional internal context" and included_in_ai(note) is selected
    # Repeating the same choice writes no second event, note or external send.
    assert store.session.scalar(sa.select(sa.func.count()).select_from(BucketNote)) == 1
    assert store.event.await_count == 3
    assert all(call.kwargs.get("visibility") == communications.file_events.VISIBILITY_DESK for call in store.event.await_args_list)
    assert all("Fictional internal context" not in str(call.kwargs) for call in store.event.await_args_list)


@pytest.mark.asyncio
async def test_selected_internal_note_can_be_created_without_a_second_post(store):
    result = await communications.reply_unified_communication_thread(store.thread_id,
        UnifiedCommunicationCompose(body="Selected context", include_in_ai_context=True), None, store.user, store.db)
    assert len(result.messages) == 1 and result.messages[0].include_in_ai_context is True


@pytest.mark.asyncio
async def test_internal_note_cannot_be_added_to_an_archived_file(store):
    store.session.execute(store.buckets.update().where(store.buckets.c.id == store.bucket_id).values(archived_at=datetime.now(UTC)))
    store.session.commit()
    with pytest.raises(HTTPException) as error:
        await communications.reply_unified_communication_thread(store.thread_id,
            UnifiedCommunicationCompose(body="An archived-file note"), None, store.user, store.db)
    assert error.value.status_code == 404
    assert store.session.scalar(sa.select(sa.func.count()).select_from(BucketNote)) == 0
    store.event.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_internal_thread_is_not_limited_to_the_recent_inbox_scan(monkeypatch):
    intake_id = uuid4()
    intake = SimpleNamespace(id=intake_id, full_name="Fictional owner", business_name="Fictional file",
        email="owner@example.com", created_at=datetime.now(UTC))
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: intake)))
    monkeypatch.setattr(communications, "_all_threads", AsyncMock(return_value=[]))
    visible = AsyncMock(side_effect=AssertionError("A direct internal thread must use its file ID"))
    monkeypatch.setattr(communications, "_visible_intakes", visible)
    result = await communications._thread_summary(db, SimpleNamespace(role=Role.LOAN_EXEC), f"intake:{intake_id}:internal")
    assert result.source_id == str(intake_id) and result.channel == "internal"
    statement = db.execute.await_args.args[0]
    assert "archived_at IS NULL" in str(statement)
    assert intake_id in statement.compile().params.values()
    visible.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [Role.CLIENT, Role.BROKER, Role.REGIONAL_MANAGER, Role.DEALER_PARTNER, Role.FIELD_REP, Role.CAPITAL_PARTNER])
async def test_only_internal_staff_can_update_ai_selection(store, role):
    store.user.role = role
    with pytest.raises(HTTPException) as error:
        await communications.update_internal_note_ai_context(store.thread_id, uuid4(),
            InternalNoteAIContextUpdate(include_in_ai_context=True), store.user, store.db)
    assert error.value.status_code == 403
    store.event.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["other_file", "missing", "partner", "shared", "external_owner", "archived"])
async def test_note_updates_are_bound_to_the_active_internal_file(store, fault):
    note = BucketNote(bucket_id=store.bucket_id, author_name="Staff", author_role="loan_exec", visibility="admin", channel="internal", content="Unchanged note")
    if fault == "other_file":
        note.bucket_id = uuid4()
        store.session.execute(store.buckets.insert().values(id=note.bucket_id))
    elif fault == "partner":
        note.channel = "partner"
    elif fault == "shared":
        note.visibility = "shared"
    elif fault == "external_owner":
        note.vendor_access_id = uuid4()
    elif fault == "archived":
        store.session.execute(store.buckets.update().where(store.buckets.c.id == store.bucket_id).values(archived_at=datetime.now(UTC)))
    store.session.add(note)
    store.session.commit()
    identifier = uuid4() if fault == "missing" else note.id
    with pytest.raises(HTTPException) as error:
        await communications.update_internal_note_ai_context(store.thread_id, identifier,
            InternalNoteAIContextUpdate(include_in_ai_context=True), store.user, store.db)
    assert error.value.status_code == 404
    assert note.include_in_ai_context is False
    store.event.assert_not_awaited()


@pytest.mark.asyncio
async def test_partner_channels_cannot_accept_or_expose_ai_selection(store):
    thread_id = store.thread_id.replace(":internal", ":partner")
    with pytest.raises(HTTPException) as error:
        await communications.reply_unified_communication_thread(thread_id,
            UnifiedCommunicationCompose(body="A partner message", include_in_ai_context=True), None, store.user, store.db)
    assert error.value.status_code == 422
    note = BucketNote(bucket_id=store.bucket_id, author_name="Staff", author_role="loan_exec", visibility="admin", channel="partner", content="Partner message")
    store.session.add(note)
    store.session.commit()
    result = await communications.get_unified_communication_thread(thread_id, store.user, store.db)
    assert "include_in_ai_context" not in result.model_dump()["messages"][0]


@pytest.mark.parametrize("value", ["true", "false", 0, 1, None, []])
def test_context_choice_requires_a_json_boolean(value):
    with pytest.raises(ValidationError):
        InternalNoteAIContextUpdate(include_in_ai_context=value)
    with pytest.raises(ValidationError):
        UnifiedCommunicationCompose(body="A note", include_in_ai_context=value)


def test_context_patch_cannot_edit_raw_note_text():
    with pytest.raises(ValidationError):
        InternalNoteAIContextUpdate(include_in_ai_context=True, content="A replacement")


def test_migration_keeps_existing_and_new_notes_excluded_by_default():
    location = Path(__file__).resolve().parents[2] / "alembic/versions/0289_internal_note_ai_context.py"
    spec = spec_from_file_location("internal_note_migration", location)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE bucket_notes (id INTEGER PRIMARY KEY, content TEXT NOT NULL)")
        connection.exec_driver_sql("INSERT INTO bucket_notes (id, content) VALUES (1, 'Existing note')")
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        connection.exec_driver_sql("INSERT INTO bucket_notes (id, content) VALUES (2, 'New note')")
        assert connection.exec_driver_sql("SELECT include_in_ai_context FROM bucket_notes ORDER BY id").scalars().all() == [0, 0]
        module.downgrade()
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM bucket_notes").scalar_one() == 2
    engine.dispose()
