from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy import event, func, select

from app.db import get_db
from app.enums import Role
from app.models.activity import Activity
from app.models.client import Client
from app.models.email_marketing import (
    MarketingDelivery,
    MarketingEnrollment,
    MarketingPermissionBatch,
    MarketingPermissionRequest,
    MarketingSubscriber,
)
from app.models.event import CalendarEvent
from app.models.notification import Notification
from app.models.prospect_outreach import EmailSuppression
from app.models.user import User
from app.routers.calendar import _scope_calendar_for_audience
from app.routers.marketing_permission_requests import public
from app.services import email_marketing
from app.services import marketing_permission_requests as requests
from app.services import marketing_permission_tasks as tasks
from app.tests._sqlite_schema import sqlite_session, sqlite_template


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    return sqlite_template(tmp_path_factory, "permission_requests")


@pytest.fixture
async def db(template, tmp_path, monkeypatch):
    monkeypatch.setattr(email_marketing, "_secret", lambda: b"test-secret")
    async with sqlite_session(template, tmp_path) as db:
        event.listen(db.bind.sync_engine, "connect", lambda connection, _: connection.create_function("pg_advisory_xact_lock", 1, lambda _: 0))
        yield db


async def seed(db):
    actor = User(email="staff@example.invalid", role=Role.SUPER_ADMIN)
    db.add_all([actor, Client(email="owner@example.invalid", name="Example Owner")])
    await db.commit()
    return actor.id


def action(kind, **kwargs):
    return requests.PermissionAction.model_validate({
        "id": uuid4(), "expected_recipient_count": 1, "selection": {"mode": "all"},
        "action": kind, "confirmed": True, "expected_by_contacts": kind == "send_requests", **kwargs,
    })


async def count(db, model):
    return await db.scalar(select(func.count()).select_from(model))


async def test_batch_replay_and_review_guard_task_then_one_expected_invitation(db):
    actor = await seed(db)
    with pytest.raises(ValidationError):
        action("send_requests", expected_by_contacts=False)
    with pytest.raises(ValidationError):
        action("create_tasks", confirmed=False)
    with pytest.raises(HTTPException) as changed:
        await requests.activate(db, actor, action("create_tasks", expected_recipient_count=2))
    assert changed.value.status_code == 409 and await count(db, MarketingPermissionBatch) == 0
    task = action("create_tasks")
    assert (await requests.activate(db, actor, task))["requested_count"] == 0
    row = await db.scalar(select(MarketingPermissionRequest))
    assert row.status == "task_only" and row.invite_batch_id is None
    assert (await db.get(CalendarEvent, row.task_event_id)).owner_user_id == actor
    assert await count(db, MarketingSubscriber) == await count(db, MarketingDelivery) == 0
    assert (await requests.activate(db, actor, task))["task_count"] == 1
    with pytest.raises(HTTPException):
        await requests.invitation(db, requests.permission_token(row.id))
    invite = action("send_requests")
    assert (await requests.activate(db, actor, invite))["requested_count"] == 1
    assert row.status == "queued" and row.invite_batch_id == invite.id
    assert (await requests.activate(db, actor, invite))["already_queued_count"] == 1
    with pytest.raises(HTTPException):
        await requests.activate(db, actor, action("send_requests"))
    assert await count(db, CalendarEvent) == await count(db, MarketingPermissionRequest) == 1
    assert await count(db, MarketingPermissionBatch) == 2 and await count(db, MarketingDelivery) == 0


@pytest.mark.parametrize("decline", [True, False])
async def test_scanner_safe_http_then_explicit_decision(db, decline):
    actor = await seed(db)
    await requests.activate(db, actor, action("send_requests"))
    row = await db.scalar(select(MarketingPermissionRequest))
    row.status = "sent"
    row.locale = "es"
    await db.commit()
    app = FastAPI()
    app.include_router(public, prefix="/api/v1")
    async def database():
        yield db
    app.dependency_overrides[get_db] = database
    url = "/api/v1/public/email-marketing/permission/" + requests.permission_token(row.id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://example.invalid") as client:
        for suffix in ("", "/decline", "/confirm"):
            response = await client.get(url + suffix)
            assert response.status_code == 200
            assert response.headers["Referrer-Policy"] == "no-referrer" and "no-store" in response.headers["cache-control"]
            assert "¿Desea" in response.text and row.email not in response.text
        assert (await client.get(url + "0")).status_code == 404
        assert (await client.post(url + "/confirm")).status_code == 422
        assert row.status == "sent" and await count(db, MarketingSubscriber) == await count(db, EmailSuppression) == 0
        if decline:
            assert (await client.post(url + "/decline", data={"List-Unsubscribe": "One-Click"})).status_code == 200
            assert (await client.post(url + "/confirm", data={"decision": "confirm"})).status_code == 410
            assert row.status == "declined" and await count(db, EmailSuppression) == 1
            assert await count(db, MarketingSubscriber) == await count(db, MarketingDelivery) == 0
        else:
            for _ in range(2):
                response = await client.post(url + "/confirm", data={"decision": "confirm"})
                assert response.status_code == 200 and "confirmada" in response.text
            assert row.status == "confirmed" and await count(db, MarketingSubscriber) == await count(db, MarketingDelivery) == 1
            assert (await db.scalar(select(MarketingEnrollment))).source == "permission_confirmation"
            assert await db.scalar(select(func.count()).select_from(Activity).where(Activity.kind == "email_marketing.permission_confirmed")) == 1
            await client.post(url + "/decline")
            assert (await db.scalar(select(MarketingSubscriber))).unsubscribed_at and await count(db, EmailSuppression) == 1
    assert (await db.get(CalendarEvent, row.task_event_id)).status == "done"


async def test_reconciliation_updates_one_private_calendar_entry_and_only_internal_reminders(db):
    actor = await seed(db)
    outsider = User(email="staff2@example.invalid", role=Role.LOAN_EXEC)
    db.add(outsider)
    now = datetime.now(UTC)
    await tasks.reconcile(db, now)
    await tasks.reconcile(db, now + timedelta(minutes=15))
    aggregate = await db.get(CalendarEvent, tasks.AGGREGATE_ID)
    assert "1 contact " in aggregate.title and aggregate.owner_user_id == actor
    assert await count(db, CalendarEvent) == await count(db, Notification) == 1
    assert await db.scalar(_scope_calendar_for_audience(outsider, select(CalendarEvent.id))) is None
    assert (await db.scalar(select(Notification))).deep_link.endswith("eligibility=requestable")
    await requests.activate(db, actor, action("create_tasks"))
    await tasks.reconcile(db, now + timedelta(days=3))
    await tasks.reconcile(db, now + timedelta(days=4))
    row = await db.scalar(select(MarketingPermissionRequest))
    note = await db.scalar(select(Notification).where(Notification.target_id == str(row.task_event_id)))
    assert note.channels == ["in_app"] and note.recipient_user_id == actor and "?search=owner%40example.invalid" in note.deep_link
    calendar_event = await db.get(CalendarEvent, row.task_event_id)
    assert row.email not in calendar_event.description and calendar_event.status == "pending"
    assert aggregate.status == "done" and await count(db, Notification) == 2
    assert await count(db, MarketingDelivery) == 0
    await email_marketing.enroll(db, email=row.email, consent=True, source="fixture_form")
    await tasks.reconcile(db, now + timedelta(days=5))
    assert row.status == "confirmed" and calendar_event.status == "done" and note.read_at is not None


@pytest.mark.parametrize("outcome", ["blocked", "expired"])
async def test_late_hold_or_expiry_resolves_open_review_and_cannot_confirm(db, outcome):
    actor = await seed(db)
    await requests.activate(db, actor, action("send_requests"))
    row = await db.scalar(select(MarketingPermissionRequest))
    row.status = "sent"
    if outcome == "expired":
        row.expires_at = datetime.now(UTC) - timedelta(hours=1)
    else:
        (await db.scalar(select(Client))).contact_permission = "save_lead_only"
    await db.commit()
    with pytest.raises(HTTPException) as unavailable:
        await requests.respond(db, requests.permission_token(row.id), agree=True)
    assert unavailable.value.status_code == 410
    await tasks.reconcile(db, datetime.now(UTC))
    assert row.status == outcome
    assert (await db.get(CalendarEvent, row.task_event_id)).status == "done"
    assert await count(db, MarketingDelivery) == await count(db, MarketingSubscriber) == 0
