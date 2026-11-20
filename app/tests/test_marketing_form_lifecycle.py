"""Exercise the website's contact form through persistence and withdrawal without a mail provider."""
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, select

from app.db import get_db
from app.models.email_marketing import MarketingDelivery, MarketingSubscriber
from app.routers import public
from app.services import email_marketing as marketing
from app.services.email import gmail_client
from app.tests._sqlite_schema import sqlite_session, sqlite_template


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    return sqlite_template(tmp_path_factory, "marketing_form_lifecycle")


async def test_support_submission_to_marketing_audience_and_withdrawal(template, tmp_path, monkeypatch):
    monkeypatch.setattr(public, "_LAST_SUBMIT", {})
    monkeypatch.setattr(public, "_THROTTLE_SECONDS", 0)
    monkeypatch.setattr(gmail_client, "gmail_config", lambda: None)
    monkeypatch.setattr(marketing, "_secret", lambda: b"marketing-test-only")
    app = FastAPI()
    app.include_router(public.router)
    payload = {
        "full_name": "Example Owner", "email": "Example@Example.org", "phone": "2015550132",
        "topic": "Program information", "title": "Financing guides", "body": "Please help with my inquiry.",
        "consent": True,
    }
    async with sqlite_session(template, tmp_path) as db:
        # This single-connection test exercises the real unsubscribe transaction.
        # Its production PostgreSQL advisory lock has no SQLite equivalent.
        event.listen(db.bind.sync_engine, "connect", lambda connection, _:
                     connection.create_function("pg_advisory_xact_lock", 1, lambda _key: 0))
        async def transactional_db():
            yield db
            await db.commit()

        app.dependency_overrides[get_db] = transactional_db
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://test") as client:
            path = public.router.prefix + "/support-inquiry"
            assert (await client.post(path, json=payload)).status_code == 200
            assert await db.scalar(select(MarketingSubscriber.id)) is None
            assert (await client.post(path, json={**payload, "email_marketing_consent": "false"})).status_code == 422
            opted_in = {**payload, "email_marketing_consent": True}
            for _ in range(2):
                assert (await client.post(path, json=opted_in)).status_code == 200
            subscriber = (await db.scalars(select(MarketingSubscriber))).one()
            assert subscriber.email == "example@example.org"
            delivery = (await db.scalars(select(MarketingDelivery))).one()
            assert delivery.subscriber_id == subscriber.id and delivery.status == "queued"
            audience = marketing.eligible_ids(marketing.AudienceFilters(source="support_inquiry", locale="en"))
            assert list(await db.scalars(audience)) == [subscriber.id]
            await marketing.unsubscribe(db, marketing.unsubscribe_token(subscriber.id))
            assert (await client.post(path, json=opted_in)).status_code == 200
            assert list(await db.scalars(audience)) == []
