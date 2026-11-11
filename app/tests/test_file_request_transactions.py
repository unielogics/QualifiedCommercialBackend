"""Real foreign keys and route transactions for staff document requests."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.db import get_db
from app.deps import get_current_user
from app.enums import Role
from app.models.application_profile import (
    ApplicationCollectionTask,
    ApplicationRequirementState,
    ApplicationRoomDelivery,
)
from app.models.bucket import BucketRequestedDocument
from app.routers import file_requests as routes
from app.schemas.file_request import FileRequestItem
from app.services import collection_tasks, file_requests


@compiles(JSONB, "sqlite")
def sqlite_json(_type, _compiler, **_kwargs):
    return "JSON"


@asynccontextmanager
async def database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'requests.db'}")

    @sa.event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    metadata = sa.MetaData()
    for name in ("buckets", "application_profiles", "users", "bucket_files", "bucket_document_templates"):
        sa.Table(name, metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    for model in (BucketRequestedDocument, ApplicationRequirementState, ApplicationCollectionTask, ApplicationRoomDelivery):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if column.server_default is not None:
                default = str(column.server_default.arg)
                if "::jsonb" in default:
                    column.server_default = sa.DefaultClause(sa.text(default.replace("::jsonb", "")))
                elif default == "now()":
                    column.server_default = sa.DefaultClause(sa.text("CURRENT_TIMESTAMP"))
        for index in table.indexes:
            where = index.dialect_options["postgresql"].get("where")
            if where is not None:
                index.dialect_options["sqlite"]["where"] = where
    owner = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), dealer_id=None, intake_id=None)
    actor = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
        for name, identifier in (("application_profiles", owner.id), ("buckets", owner.primary_bucket_id), ("users", actor.id)):
            await connection.execute(metadata.tables[name].insert().values(id=identifier))
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            yield db, engine, owner, actor
    finally:
        await engine.dispose()


def mock_external_work(monkeypatch, owner):
    monkeypatch.setattr(routes.profiles, "load_profile", AsyncMock(return_value=owner))
    monkeypatch.setattr(routes, "_require_training_live_action", AsyncMock())
    monkeypatch.setattr(file_requests.profiles, "log_profile_action", AsyncMock())
    monkeypatch.setattr("app.services.arf_channel.profile_uses_arf", AsyncMock(return_value=False))
    monkeypatch.setattr(collection_tasks, "sync_collection_tasks", AsyncMock(return_value=[]))


async def counts(db):
    values = []
    for model in (BucketRequestedDocument, ApplicationRequirementState, ApplicationRoomDelivery):
        values.append((await db.execute(sa.select(sa.func.count()).select_from(model))).scalar_one())
    return tuple(values)


def app_for(db, actor):
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: actor
    return app


@pytest.mark.asyncio
async def test_new_multi_item_request_satisfies_real_foreign_keys_and_replay_reuses_destinations(tmp_path, monkeypatch):
    async with database(tmp_path) as (db, _engine, owner, actor):
        mock_external_work(monkeypatch, owner)
        request_id = uuid4()
        items = [FileRequestItem(document_type="operating_plan", name="Operating expenses and earnings"),
                 FileRequestItem(document_type="bank_statement", name="Business bank statements", quantity=3, unit="months")]
        first = await file_requests.create_batch(db, profile=owner, request_id=request_id, items=items, user=actor)
        assert len(first) == 2
        assert await counts(db) == (2, 2, 0)
        for state in first:
            assert await db.get(BucketRequestedDocument, state.requested_document_id) is not None
        repeated = await file_requests.create_batch(db, profile=owner, request_id=request_id, items=items, user=actor)
        assert [row.id for row in repeated] == [row.id for row in first]
        assert await counts(db) == (2, 2, 0)


@pytest.mark.asyncio
async def test_later_item_failure_rolls_back_flushed_destinations_and_states_together(tmp_path, monkeypatch):
    async with database(tmp_path) as (db, engine, owner, actor):
        mock_external_work(monkeypatch, owner)
        original = file_requests.item_config

        def invalid_second(item, request_id):
            if item.document_type == "other":
                raise HTTPException(422, "Synthetic second-item rejection")
            return original(item, request_id)

        monkeypatch.setattr(file_requests, "item_config", invalid_second)
        with pytest.raises(HTTPException):
            await file_requests.create_batch(db, profile=owner, request_id=uuid4(), user=actor, items=[
                FileRequestItem(document_type="operating_plan", name="Operating plan"),
                FileRequestItem(document_type="other", name="Supporting explanation")])
        await db.rollback()
        async with AsyncSession(engine) as observer:
            assert await counts(observer) == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_fails", [False, True])
async def test_real_notify_post_commits_requests_before_provider_and_replays_one_receipt(tmp_path, monkeypatch, provider_fails):
    async with database(tmp_path) as (db, engine, owner, actor):
        mock_external_work(monkeypatch, owner)

        async def provider(session, **values):
            async with AsyncSession(engine) as observer:
                # A separate database session sees committed destinations
                # before any external provider is called.
                assert await counts(observer) == (2, 2, 0)
            if provider_fails:
                raise HTTPException(409, "Synthetic provider failure")
            states = await file_requests.request_states(session, owner.id, request_id)
            receipt = ApplicationRoomDelivery(id=uuid4(), profile_id=owner.id, bucket_id=owner.primary_bucket_id,
                requested_document_id=states[0].requested_document_id, action_kind="missing_item_email", channel="email",
                status="sent", recipient_email="client@example.com", detail="Accepted by synthetic provider",
                initiation_source="staff_file_request", idempotency_key=values["delivery_key"],
                created_by_user_id=actor.id, provider_result={"file_request_id": str(request_id)})
            session.add(receipt)
            return receipt

        sender = AsyncMock(side_effect=provider)
        monkeypatch.setattr(file_requests.missing_item_automation, "send_requirements_email", sender)
        request_id = uuid4()
        payload = {"request_id": str(request_id), "notify": True, "items": [
            {"document_type": "operating_plan", "name": "Operating expenses and earnings"},
            {"document_type": "bank_statement", "name": "Bank statements", "quantity": 3, "unit": "months"}]}
        async with AsyncClient(transport=ASGITransport(app=app_for(db, actor)), base_url="http://test") as client:
            first = await client.post(f"/api/v1/application-profiles/{owner.id}/file-requests", json=payload)
            assert first.status_code == 200, first.text
            output = first.json()
            assert output["notification"]["status"] == ("failed" if provider_fails else "sent")
            assert output["request_id"] == str(request_id)
            assert len(output["requests"]) == 1 and len(output["requests"][0]["items"]) == 2
            assert await counts(db) == (2, 2, 1)
            repeated = await client.post(f"/api/v1/application-profiles/{owner.id}/file-requests", json=payload)
            assert repeated.status_code == 200
            assert repeated.json()["notification"]["id"] == output["notification"]["id"]
            assert await counts(db) == (2, 2, 1)
        sender.assert_awaited_once()
