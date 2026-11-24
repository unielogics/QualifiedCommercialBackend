from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select

from app.models.activity import Activity
from app.models.client import Client
from app.models.email_marketing import MarketingDelivery, MarketingEnrollment, MarketingSubscriber
from app.routers.marketing_consent import Evidence, record_evidence
from app.tests._sqlite_schema import sqlite_session, sqlite_template


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    return sqlite_template(tmp_path_factory, "consent_evidence")


async def test_existing_consent_does_not_start_followups_or_override_withdrawal(template, tmp_path):
    async with sqlite_session(template, tmp_path) as db:
        event.listen(db.bind.sync_engine, "connect",
                     lambda connection, _: connection.create_function("pg_advisory_xact_lock", 1, lambda _: 0))
        email = "owner@example.org"
        admin = SimpleNamespace(id=uuid4())
        today = datetime.now(UTC).date()
        evidence = Evidence(email=email.upper(), source="written_request", reference="Example email thread 12",
                            granted_on=today, locale="en", confirmed=True)
        with pytest.raises(HTTPException, match="directory"):
            await record_evidence(evidence, db, admin)
        db.add(Client(email=email, name="Example Owner"))
        await db.flush()
        for invalid in ({"confirmed": False}, {"granted_on": today + timedelta(days=1)}):
            with pytest.raises(HTTPException, match="Confirm"):
                await record_evidence(evidence.model_copy(update=invalid), db, admin)
        for _ in range(2):
            await record_evidence(evidence, db, admin)
        assert (await db.scalars(select(MarketingEnrollment))).one().source == "documented_opt_in"
        assert await db.scalar(select(MarketingDelivery.id)) is None
        audit = (await db.scalars(select(Activity).where(Activity.kind == "email_marketing.consent_recorded"))).one()
        assert audit.actor_id == admin.id and audit.payload["reference"] == evidence.reference
        subscriber = (await db.scalars(select(MarketingSubscriber))).one()
        subscriber.unsubscribed_at = datetime.now(UTC)
        await db.commit()
        with pytest.raises(HTTPException) as error:
            await record_evidence(evidence, db, admin)
        assert error.value.status_code == 409
