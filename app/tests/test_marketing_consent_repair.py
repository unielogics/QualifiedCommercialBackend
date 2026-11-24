"""Repair only a missing, address-matched broad grant without starting automation."""
from datetime import UTC, datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import uuid4, uuid5

import pytest
from sqlalchemy import event, select

from app.models.activity import Activity
from app.models.client import Client
from app.models.email_marketing import MarketingDelivery, MarketingEnrollment, MarketingSubscriber
from app.models.message_send import MessageSend  # noqa: F401 - register the delivery ledger FK
from app.models.prospect_outreach import EmailSuppression
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.resource_assessment import ResourceAssessment
from app.tests._sqlite_schema import sqlite_session, sqlite_template


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    return sqlite_template(tmp_path_factory, "repair_marketing_consent")


async def test_repair_obeys_original_scope_address_and_suppression(template, tmp_path):
    spec = spec_from_file_location("repair_revision", Path(__file__).resolve().parents[2] /
                                   "alembic/versions/0287_campaign_composer.py")
    revision = module_from_spec(spec)
    spec.loader.exec_module(revision)
    async with sqlite_session(template, tmp_path) as db:
        event.listen(db.bind.sync_engine, "connect", lambda con, _:
                     con.create_function("gen_random_uuid", 0, lambda: uuid4().hex))
        now = datetime.now(UTC)
        client = Client(name="Borrower", email="primary@example.org", user_id=uuid4())
        db.add(client)
        await db.flush()

        def file(address, *, broad=True, captured=None, **extra):
            return PublicUnderwritingIntake(
                full_name="Applicant", client_id=client.id, bucket_id=uuid4(), token_hash=uuid4().hex,
                email=address, variant="main_street_v1", source_kind="public_form", preferred_language="es",
                intake_state={"source": "business_term_intake", "legal_acceptance": {
                    "email_marketing_consent": broad, "application_email_consent": {
                        "granted": True, "version": "application-email-v1", "email": captured or address,
                    }}}, **extra,
            )

        claim = file("readiness@example.org")
        claim.source_kind = "client_capital_readiness"
        claim.intake_state = {"capital_readiness_claim": {**claim.intake_state["legal_acceptance"],
                                                          "user_id": str(client.user_id)}}
        db.add_all([
            claim, file("Owner@Example.org"), file("owner@example.org"), file("terms@example.org", broad=False),
            file("string@example.org", broad="true"), file("changed@example.org", captured="previous@example.org"),
            file("withdrawn@example.org"), file("suppressed@example.org"), file("held@example.org", client_contact_suppressed=True),
            MarketingSubscriber(email="withdrawn@example.org", locale="en", unsubscribed_at=now),
            EmailSuppression(email_normalized="suppressed@example.org", reason="complaint", source="test"),
        ])
        for label, broad in (("short-only", None), ("expanded", True), ("wrong-event", True), ("text-only", "true")):
            identifier = uuid4()
            db.add(ResourceAssessment(id=identifier, client_id=client.id, idempotency_key=str(uuid4()),
                goal="term_loan", locale="en", resource_id="business_funding_check",
                contact={"email": f"{label}@example.org"}, answers={}, report={}, email_marketing_consent=True,
                consent_version="business-funding-resource-v1", resource_token_hash=uuid4().hex, created_at=now,
                token_expires_at=now + timedelta(days=7)))
            db.add(Activity(id=uuid5(identifier, "resource.lead_received"), kind="resource.lead_received",
                actor_label="public_resource", client_id=client.id, summary="Resource captured", payload={
                    "resource_id": "business_funding_check", "goal": "term_loan", "consent_version": "business-funding-resource-v1",
                    "assessment_id": str(uuid4() if label == "wrong-event" else identifier),
                    "email_marketing_consent": True, **({"lending_updates_consent": broad} if broad is not None else {}),
                }))
        await db.flush()
        for _ in range(2):
            await db.run_sync(lambda session: revision.repair_missing_subscriptions(session.connection().execute))
        members = {s.email: s for s in await db.scalars(select(MarketingSubscriber))}
        assert set(members) == {"owner@example.org", "expanded@example.org", "withdrawn@example.org", "readiness@example.org"}
        assert members["withdrawn@example.org"].unsubscribed_at is not None
        assert members["owner@example.org"].locale == "es"
        grants = (await db.scalars(select(MarketingEnrollment))).all()
        assert {(g.subscriber_id, g.source, g.program) for g in grants} == {
            (members["owner@example.org"].id, "business_term_intake", "term_loan"),
            (members["expanded@example.org"].id, "business_funding_check", "term_loan"),
            (members["readiness@example.org"].id, "capital_readiness_diagnostic", "main_street"),
        }
        assert len(grants) == 4  # Two distinct subscribed intakes for the same address.
        assert not (await db.scalars(select(MarketingDelivery))).all()
