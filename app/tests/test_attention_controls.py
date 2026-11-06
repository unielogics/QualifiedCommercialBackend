from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.schema import CreateTable

from app.db import get_db
from app.deps import get_current_user
from app.enums import Role
from app.models.operator_attention import OperatorAttentionControl, OperatorAttentionEvent
from app.routers.operator_files import router
from app.schemas.operator_file import AttentionAction, AttentionItem
from app.services import attention_controls as controls
from app.services import attention_queue as queue


class Result:
    def __init__(self, rows=(), scalar=None):
        self.rows, self.scalar = list(rows), scalar

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        return self.scalar

    def scalar_one(self):
        return self.scalar


class Store:
    def __init__(self):
        self.controls, self.events, self.added, self.live = {}, [], [], []
        self.lock = asyncio.Lock()


class DB:
    def __init__(self, store):
        self.store, self.held = store, False

    def add(self, row):
        self.store.added.append(row)
        if isinstance(row, OperatorAttentionControl):
            self.store.controls[row.item_key] = row
        elif isinstance(row, OperatorAttentionEvent):
            self.store.events.append(row)
        else:
            raise AssertionError("Queue actions must never write client/business records")

    async def flush(self):
        await asyncio.sleep(0)

    async def commit(self):
        if self.held:
            self.held = False
            self.store.lock.release()

    async def rollback(self):
        await self.commit()

    async def get(self, model, key):
        assert model is OperatorAttentionControl
        return self.store.controls.get(key)

    async def execute(self, statement):
        if "pg_advisory_xact_lock" in str(statement):
            if not self.held:
                await self.store.lock.acquire()
                self.held = True
            return Result()
        model = statement.column_descriptions[0].get("entity")
        params = statement.compile().params
        if model is OperatorAttentionControl:
            return Result(self.store.controls.values())
        if model is OperatorAttentionEvent:
            if "idempotency_key_1" in params:
                return Result(scalar=next((row for row in self.store.events if row.idempotency_key == params["idempotency_key_1"]), None))
            rows = [row for row in self.store.events if row.item_key == params["item_key_1"]]
            rows.sort(key=lambda row: row.revision, reverse=True)
            offset = statement._offset_clause.value if statement._offset_clause is not None else 0
            limit = statement._limit_clause.value if statement._limit_clause is not None else len(rows)
            return Result(rows[offset:offset + limit])
        if "count(" in str(statement):
            return Result(scalar=sum(row.item_key == params["item_key_1"] for row in self.store.events))
        raise AssertionError(f"Unexpected queue write/read: {statement}")


def item(number, *, kind="intake_no_files", title="Example Company"):
    return AttentionItem(kind=kind, severity="warn", title=title, detail="Waiting for source evidence", age_days=31,
        bucket_id=UUID(int=1000 + number), intake_id=UUID(int=number), profile_id=UUID(int=2000 + number), href=f"/file/{number}", actions=["resend_link"])


@pytest.fixture
def setup(monkeypatch):
    store = Store()
    store.live = [item(1), item(2), item(3, kind="request_overdue")]
    actor = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC, name="Example Staff")
    monkeypatch.setattr(queue, "load_source_items", AsyncMock(side_effect=lambda *args, **kwargs: copy.deepcopy(store.live)))
    return SimpleNamespace(store=store, actor=actor)


async def act(setup, number=1, action="archive", revision=0, *, target=None, reason="Reviewed on desk", request_id=None):
    db = DB(setup.store)
    payload = AttentionAction(item_key=controls.item_key(item(number)), action=action, expected_revision=revision,
        idempotency_key=request_id or uuid4(), duplicate_of_key=controls.item_key(item(target)) if target else None, reason=reason)
    try:
        result = await controls.apply_action(db, payload=payload, actor=setup.actor)
        await db.commit()
        return result, payload
    except Exception:
        await db.rollback()
        raise


@pytest.mark.parametrize("action,status", [("archive", "archived"), ("dismiss", "dismissed")])
async def test_hidden_controls_persist_across_sessions_without_business_writes_and_restore(setup, action, status):
    result, _ = await act(setup, action=action)
    assert result.item.queue_status == status and result.item.queue_revision == 1
    assert result.item.queue_actor_name == "Example Staff" and result.item.queue_reason == "Reviewed on desk"
    assert result.event.actor_user_id == setup.actor.id
    active = await queue.load_attention_queue(DB(setup.store))
    hidden = await queue.load_attention_queue(DB(setup.store), view="hidden")
    assert active.total == 2 and active.hidden_total == 1 and active.filtered_total == 2
    assert len(hidden.items) == 1 and hidden.items[0].item_key == controls.item_key(item(1))
    assert hidden.items[0].actions == []
    assert hidden.counts == {"intake_no_files": 1, "request_overdue": 1}
    assert hidden.status_counts[status] == 1
    restored, _ = await act(setup, action="restore", revision=1)
    assert restored.item.queue_status == "active" and restored.item.queue_revision == 2
    assert len(setup.store.events) == 2
    assert (await queue.load_attention_queue(DB(setup.store))).total == 3
    assert all(isinstance(row, (OperatorAttentionControl, OperatorAttentionEvent)) for row in setup.store.added)


async def test_resolved_hidden_snapshot_remains_reviewable_and_restore_does_not_fabricate_alert(setup):
    await act(setup)
    setup.store.live = [row for row in setup.store.live if row.intake_id != UUID(int=1)]
    hidden = await queue.load_attention_queue(DB(setup.store), view="hidden")
    assert hidden.hidden_total == 1 and hidden.items[0].title == "Example Company"
    assert hidden.items[0].current_source_active is False
    restored, _ = await act(setup, action="restore", revision=1)
    assert restored.current_source_active is False and "resolved" in restored.message
    assert (await queue.load_attention_queue(DB(setup.store), view="all")).filtered_total == 2
    setup.store.live.append(item(1))
    revived = await queue.load_attention_queue(DB(setup.store))
    assert revived.total == 3 and next(row for row in revived.items if row.intake_id == UUID(int=1)).queue_revision == 2


async def test_suppression_survives_source_updates_but_never_suppresses_other_issue_kind(setup):
    await act(setup)
    setup.store.live[0].title = "Corrected Company LLC"
    setup.store.live[0].profile_id = uuid4()
    setup.store.live[0].count = 99
    other_kind = item(1, kind="action_items_stale")
    setup.store.live.append(other_kind)
    active = await queue.load_attention_queue(DB(setup.store))
    assert active.total == 3 and other_kind.kind in active.counts
    assert controls.item_key(item(1)) not in {row.item_key for row in active.items}
    hidden = await queue.load_attention_queue(DB(setup.store), view="hidden")
    assert hidden.items[0].title == "Corrected Company LLC" and hidden.items[0].queue_revision == 1


async def test_old_idempotent_archive_retry_after_restore_returns_current_state_without_rehiding(setup):
    first, payload = await act(setup)
    await act(setup, action="restore", revision=1)
    db = DB(setup.store)
    result = await controls.apply_action(db, payload=payload, actor=setup.actor)
    await db.commit()
    assert result.replayed is True and result.item.queue_status == "active" and result.item.queue_revision == 2
    assert result.event.id == first.event.id and result.event.revision == 1
    assert len(setup.store.events) == 2


async def test_duplicate_requires_explicit_active_same_kind_survivor_and_preserves_provenance(setup):
    result, _ = await act(setup, action="duplicate", target=2)
    assert result.item.queue_status == "duplicate" and result.item.duplicate_of.item_key == controls.item_key(item(2))
    assert result.event.duplicate_of.title == "Example Company"
    assert (await queue.load_attention_queue(DB(setup.store))).total == 2
    restored, _ = await act(setup, action="restore", revision=1)
    assert restored.item.duplicate_of is None and restored.event.duplicate_of.item_key == controls.item_key(item(2))


@pytest.mark.parametrize("target", [1, 3, 99])
async def test_self_wrong_kind_or_nonexistent_duplicate_target_is_rejected(setup, target):
    target_key = controls.item_key(setup.store.live[2]) if target == 3 else controls.item_key(item(target))
    payload = AttentionAction(item_key=controls.item_key(item(1)), action="duplicate", expected_revision=0,
        idempotency_key=uuid4(), duplicate_of_key=target_key)
    db = DB(setup.store)
    with pytest.raises(HTTPException) as error:
        await controls.apply_action(db, payload=payload, actor=setup.actor)
    await db.rollback()
    assert error.value.status_code == 422 and not setup.store.events


async def test_hidden_or_resolved_survivor_is_not_a_valid_duplicate_destination(setup):
    await act(setup, number=2)
    with pytest.raises(HTTPException):
        await act(setup, action="duplicate", target=2)
    await act(setup, number=2, action="restore", revision=1)
    setup.store.live = [row for row in setup.store.live if row.intake_id != UUID(int=2)]
    with pytest.raises(HTTPException):
        await act(setup, action="duplicate", target=2)


async def test_survivor_cannot_be_hidden_until_its_duplicates_are_restored(setup):
    await act(setup, action="duplicate", target=2)
    with pytest.raises(HTTPException) as error:
        await act(setup, number=2)
    assert error.value.detail["code"] == "attention_duplicate_dependents"
    await act(setup, action="restore", revision=1)
    result, _ = await act(setup, number=2)
    assert result.item.queue_status == "archived"


async def test_concurrent_reverse_duplicate_actions_are_serialized_and_cannot_make_cycle(setup):
    outcomes = await asyncio.gather(act(setup, action="duplicate", target=2), act(setup, number=2, action="duplicate", target=1), return_exceptions=True)
    assert sum(isinstance(outcome, HTTPException) for outcome in outcomes) == 1
    assert len(setup.store.events) == 1
    assert sum(row.status == "duplicate" for row in setup.store.controls.values()) == 1


async def test_stale_revision_and_idempotency_payload_reuse_are_rejected(setup):
    _, payload = await act(setup)
    with pytest.raises(HTTPException) as stale:
        await act(setup, action="restore", revision=0)
    assert stale.value.detail["code"] == "attention_revision_conflict"
    with pytest.raises(HTTPException) as reused:
        await act(setup, action="restore", revision=1, request_id=payload.idempotency_key)
    assert reused.value.detail["code"] == "attention_idempotency_conflict"
    assert len(setup.store.events) == 1


async def test_history_is_paginated_and_actor_snapshot_survives_user_fk_removal(setup):
    first, _ = await act(setup)
    await act(setup, action="restore", revision=1)
    setup.store.events[0].actor_user_id = None  # mirrors FK ON DELETE SET NULL
    setup.actor.name = "Renamed Staff"
    history = await controls.history(DB(setup.store), key=controls.item_key(item(1)), limit=1, offset=1, actor=setup.actor)
    assert history.total == 2 and len(history.items) == 1
    assert history.items[0].id == first.event.id and history.items[0].actor_name == "Example Staff"
    assert history.items[0].actor_user_id is None


@pytest.mark.parametrize("role", [Role.BROKER, Role.FIELD_REP, Role.DEALER_PARTNER, Role.CLIENT, Role.CAPITAL_PARTNER])
async def test_queue_controls_and_history_never_widen_desk_only_permissions(setup, role):
    setup.actor.role = role
    with pytest.raises(HTTPException) as error:
        await act(setup)
    assert error.value.status_code == 403 and not setup.store.added
    with pytest.raises(HTTPException):
        await controls.history(DB(setup.store), key=controls.item_key(item(1)), limit=20, offset=0, actor=setup.actor)


def test_item_key_does_not_depend_on_business_name_profile_href_or_counts():
    original = item(1)
    revised = original.model_copy(update={"title": "Different display", "profile_id": uuid4(), "href": "/different", "count": 25})
    assert controls.item_key(original) == controls.item_key(revised)
    upper = f"intake_no_files:{{{str(original.intake_id).upper()}}}"
    assert controls.validate_key(upper) == controls.item_key(original)


async def test_active_hidden_all_filters_and_pagination_keep_global_counts_honest(setup):
    await act(setup)
    await act(setup, number=2, action="dismiss")
    setup.store.live = setup.store.live[1:]
    hidden = await queue.load_attention_queue(DB(setup.store), view="hidden", limit=1, offset=1, kind="intake_no_files", q="EXAMPLE")
    assert hidden.total == 1 and hidden.hidden_total == 2 and hidden.filtered_total == 2 and len(hidden.items) == 1
    assert hidden.counts == {"request_overdue": 1}
    assert hidden.status_counts == {"active": 1, "archived": 1, "dismissed": 1, "duplicate": 0}
    assert (await queue.load_attention_queue(DB(setup.store), view="all")).filtered_total == 3


async def test_actual_router_reaches_history_before_generic_detail_and_roundtrips_controls(setup):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: setup.actor
    app.dependency_overrides[get_db] = lambda: DB(setup.store)
    payload = AttentionAction(item_key=controls.item_key(item(1)), action="archive", expected_revision=0,
                              idempotency_key=uuid4(), reason="Reviewed from the desk")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post("/operator-files/attention/actions", json=payload.model_dump(mode="json"))
        assert result.status_code == 200 and result.json()["item"]["queue_status"] == "archived"
        hidden = await client.get("/operator-files/attention", params={"view": "hidden"})
        assert hidden.status_code == 200 and hidden.json()["hidden_total"] == 1
        history = await client.get("/operator-files/attention/history", params={"item_key": payload.item_key})
        assert history.status_code == 200 and history.json()["total"] == 1
        assert history.json()["items"][0]["actor_name"] == "Example Staff"


@pytest.mark.parametrize("changes", [{"expected_revision": -1}, {"reason": "x" * 1001}, {"queue_status": "active"}, {"idempotency_key": "not-a-uuid"}, {"action": "delete"}])
async def test_action_api_rejects_unbounded_or_injected_controls_before_writes(setup, changes):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: setup.actor
    app.dependency_overrides[get_db] = lambda: DB(setup.store)
    payload = {"item_key": controls.item_key(item(1)), "action": "archive", "expected_revision": 0, "idempotency_key": str(uuid4()), **changes}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post("/operator-files/attention/actions", json=payload)
    assert result.status_code == 422 and not setup.store.added


def test_database_constraints_protect_retry_revision_survivor_and_actor_history():
    control_ddl = str(CreateTable(OperatorAttentionControl.__table__).compile(dialect=dialect()))
    event_ddl = str(CreateTable(OperatorAttentionEvent.__table__).compile(dialect=dialect()))
    assert "duplicate_of_key <> item_key" in control_ddl and "ON DELETE RESTRICT" in control_ddl
    assert "UNIQUE (idempotency_key)" in event_ddl and "UNIQUE (item_key, revision)" in event_ddl
    assert "ON DELETE SET NULL" in event_ddl and "actor_name VARCHAR(255) NOT NULL" in event_ddl
