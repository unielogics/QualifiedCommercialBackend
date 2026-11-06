"""Queue decisions survive real ORM transactions and fresh staff sessions."""
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import JSON, Column, MetaData, Table, Uuid, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.enums import Role
from app.models.operator_attention import OperatorAttentionControl, OperatorAttentionEvent
from app.schemas.operator_file import AttentionAction, AttentionItem
from app.services import attention_controls as controls
from app.services import attention_queue as queue


class SessionProxy:
    """Substitute only PostgreSQL's advisory lock; all persistence uses SQL."""
    def __init__(self, session):
        self.session = session
        self.lock_calls = 0

    async def execute(self, statement):
        if "pg_advisory_xact_lock" in str(statement):
            self.lock_calls += 1
            return None
        return await self.session.execute(statement)

    async def get(self, model, identifier):
        return await self.session.get(model, identifier)

    def add(self, row):
        self.session.add(row)

    async def flush(self):
        await self.session.flush()


def issue(title, *, kind="intake_no_files"):
    identifier = uuid4()
    return AttentionItem(kind=kind, severity="warn", title=title, detail="Synthetic issue",
        age_days=12, href=f"/synthetic/{identifier}", intake_id=identifier,
        bucket_id=uuid4(), profile_id=uuid4())


def action(item, verb, revision, **extra):
    return AttentionAction(item_key=controls.item_key(item), action=verb,
        expected_revision=revision, idempotency_key=uuid4(), **extra)


@pytest.mark.asyncio
async def test_shared_hidden_history_restore_and_duplicate_fk_survive_fresh_sessions(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    metadata = MetaData()
    users = Table("users", metadata, Column("id", Uuid, primary_key=True))
    # Use the actual table declarations/constraints. SQLite substitutes JSON
    # storage for PostgreSQL JSONB, leaving the ORM models untouched.
    for model in (OperatorAttentionControl, OperatorAttentionEvent):
        table = model.__table__.to_metadata(metadata)
        table.c.snapshot.type = JSON()
        table.c.duplicate_snapshot.type = JSON()
    async with engine.begin() as connection:
        await connection.execute(text("PRAGMA foreign_keys=ON"))
        await connection.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    actor = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC, name="Original reviewer")
    first, second, survivor, unrelated = issue("Same Company"), issue("Same Company"), issue("Surviving file"), issue("Other issue", kind="request_overdue")
    sources = [first, second, survivor, unrelated]

    async def load_sources(*_args, **_kwargs):
        return deepcopy(sources)

    monkeypatch.setattr(queue, "load_source_items", load_sources)
    archived = action(first, "archive", 0, reason="Old intake")
    try:
        async with sessions() as session:
            await session.execute(users.insert().values(id=actor.id))
            proxy = SessionProxy(session)
            result = await controls.apply_action(proxy, payload=archived, actor=actor)
            assert result.item.queue_status == "archived" and proxy.lock_calls == 1
            await session.commit()

        # A new session reads durable state, not a retained Python object.
        async with sessions() as session:
            proxy = SessionProxy(session)
            active = await queue.load_attention_queue(proxy, limit=1)
            hidden = await queue.load_attention_queue(proxy, view="hidden")
            assert active.total == 3 and active.hidden_total == 1 and len(active.items) == 1
            assert len(hidden.items) == 1 and hidden.items[0].title == "Same Company"
            assert hidden.items[0].queue_reason == "Old intake"
            assert hidden.items[0].queue_actor_name == "Original reviewer"

        sources.remove(first)
        actor.name = "Reviewer renamed later"
        async with sessions() as session:
            proxy = SessionProxy(session)
            hidden = await queue.load_attention_queue(proxy, view="hidden")
            assert hidden.items[0].current_source_active is False
            result = await controls.apply_action(proxy, payload=action(first, "restore", 1), actor=actor)
            assert result.item.queue_status == "active" and not result.current_source_active
            await session.commit()

        async with sessions() as session:
            proxy = SessionProxy(session)
            retry = await controls.apply_action(proxy, payload=archived, actor=actor)
            assert retry.replayed and retry.item.queue_status == "active" and retry.item.queue_revision == 2
            active = await queue.load_attention_queue(proxy)
            assert active.total == 3 and active.hidden_total == 0
            assert controls.item_key(first) not in {row.item_key for row in active.items}
            history = await controls.history(proxy, key=controls.item_key(first), limit=20, offset=0, actor=actor)
            assert history.total == 2 and [row.action for row in history.items] == ["restore", "archive"]
            assert [row.actor_name for row in history.items] == ["Reviewer renamed later", "Original reviewer"]

        duplicate = action(second, "duplicate", 0, duplicate_of_key=controls.item_key(survivor))
        async with sessions() as session:
            proxy = SessionProxy(session)
            result = await controls.apply_action(proxy, payload=duplicate, actor=actor)
            assert result.item.duplicate_of.item_key == controls.item_key(survivor)
            await session.commit()

        async with sessions() as session:
            proxy = SessionProxy(session)
            with pytest.raises(HTTPException) as rejected:
                await controls.apply_action(proxy, payload=action(survivor, "dismiss", 0), actor=actor)
            assert rejected.value.status_code == 409
            await session.rollback()

        async with sessions() as session:
            proxy = SessionProxy(session)
            await controls.apply_action(proxy, payload=action(second, "restore", 1), actor=actor)
            await session.commit()
        async with sessions() as session:
            proxy = SessionProxy(session)
            retry = await controls.apply_action(proxy, payload=duplicate, actor=actor)
            assert retry.replayed and retry.item.queue_status == "active" and retry.item.duplicate_of is None
            await controls.apply_action(proxy, payload=action(second, "dismiss", 2), actor=actor)
            await session.commit()
        async with sessions() as session:
            proxy = SessionProxy(session)
            hidden = await queue.load_attention_queue(proxy, view="hidden")
            assert len(hidden.items) == 1 and hidden.items[0].queue_status == "dismissed"
            await controls.apply_action(proxy, payload=action(second, "restore", 3), actor=actor)
            await session.commit()
        async with sessions() as session:
            active = await queue.load_attention_queue(SessionProxy(session))
            assert active.total == 3 and active.hidden_total == 0
    finally:
        await engine.dispose()
