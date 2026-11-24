from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.dealer_os.models import DealerRepContact
from app.models.activity import Activity
from app.models.client import Client
from app.models.email_marketing import MarketingDelivery, MarketingSubscriber
from app.models.funding_program import FundingProgramCatalog
from app.models.prospect_outreach import EmailSuppression
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.schemas.email_marketing_audience import RecipientSelection
from app.services import email_marketing as marketing
from app.services import email_marketing_directory as directory
from app.tests._sqlite_schema import sqlite_session, sqlite_template


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    return sqlite_template(tmp_path_factory, "recipient_directory")


@pytest.fixture
async def db(template, tmp_path):
    async with sqlite_session(template, tmp_path) as session:
        yield session


async def browse(db, **kwargs):
    return await directory.directory(db, directory.DirectoryRequest(**kwargs))


async def test_directory_separates_historical_contacts_from_permission_and_suppression(db):
    db.add_all([
        Client(email="  Historical@Example.org ", name="Port Owner"),
        DealerRepContact(full_name="Port Owner", company="Example Shipping", email="historical@example.org"),
        Client(email="stopped@example.org", name="Stopped", contact_permission="save_lead_only"),
        EmailSuppression(email_normalized="unsub@example.org", reason="complaint", source="ses_event", active=True),
        Activity(actor_label="public", kind="support.inquiry", summary="Support", payload={
            "full_name": "Support Customer", "email": "unsub@example.org"}),
    ])
    for email in ["stopped@example.org", "one@example.org", "two@example.org"]:
        await marketing.enroll(db, email=email, consent=True, source="support_inquiry")
    result = await browse(db, page_size=2)
    assert result["total_count"] == 5 and result["eligible_count"] == 2
    assert [r["email"] for r in result["rows"]] == ["historical@example.org", "one@example.org"]
    assert result["rows"][0]["can_record_consent"] and not result["rows"][0]["eligible"]
    for key, reason in [("stopped", "Client contact hold"), ("unsub", "Email suppression: complaint")]:
        rows = (await browse(db, search=key))["rows"]
        assert len(rows) == 1 and rows[0]["reason"] == reason and not rows[0]["can_record_consent"]
    alias = (await browse(db, search="shipping"))["rows"]
    assert [r["email"] for r in alias] == ["historical@example.org"]
    assert (await browse(db, eligibility="ineligible"))["total_count"] == 3
    assert await directory.directory_contains(db, "historical@EXAMPLE.org")
    assert not await directory.directory_contains(db, "missing@example.org")
    assert await db.scalar(select(func.count()).select_from(MarketingDelivery)) == 3


async def test_all_minus_exclusions_plus_addition_is_independent_of_browsing_page(db):
    for name in ["Alpha percent%", "Alpha two", "Bravo", "Charlie"]:
        address = name.split()[-1].replace("%", "") + "@example.org"
        db.add(Client(name=name, email=address))
        await marketing.enroll(db, email=address, consent=True, source="support_inquiry")
    ids = {s.email: s.id for s in await db.scalars(select(MarketingSubscriber))}
    selection = RecipientSelection(mode="all", search="alpha", excluded_ids=[ids["two@example.org"]],
                                   included_ids=[ids["bravo@example.org"]])
    rows = await browse(db, search="bravo", selection=selection, page_size=1)
    assert rows["selected_count"] == 2 and rows["rows"][0]["selected"]
    selected = await browse(db, selection=selection, selected_only=True)
    assert [r["email"] for r in selected["rows"]] == ["bravo@example.org", "percent@example.org"]
    assert [r["email"] for r in (await browse(db, search="%"))["rows"]] == ["percent@example.org"]
    explicit = RecipientSelection(mode="explicit", included_ids=[ids["two@example.org"], uuid4()])
    assert list(await db.scalars(directory.selected_ids(marketing.AudienceFilters(source="database:crm_client"), explicit))) == [
        ids["two@example.org"]]
    assert list(await db.scalars(directory.selected_ids(marketing.AudienceFilters(source="database:crm_contact"), explicit))) == []


async def test_file_filter_and_recipient_merge_never_identify_referrer_as_borrower(db):
    email = "attorney@example.org"
    db.add(PublicUnderwritingIntake(email=email, full_name="Alex Counsel", business_name="Private Owner LLC",
        token_hash=uuid4().hex, bucket_id=uuid4(), variant="commercial_foreclosure_bailout_v1",
        referral_source="attorney", client_contact_suppressed=True, outcome_status="denied"))
    db.add(FundingProgramCatalog(program_key="sba", public_slug="sba", name="SBA financing", status="active", aliases=[]))
    await marketing.enroll(db, email=email, consent=True, source="foreclosure_rescue", program="sba")
    sub = await db.scalar(select(MarketingSubscriber))
    assert (await browse(db, filters={"outcome": "denied"}))["rows"] == []
    details = await directory.recipient_context(db, sub.id)
    assert details["full_name"] == "Alex Counsel" and details["first_name"] == "Alex"
    assert details["business_name"] == "" and details["program"] == "SBA financing"
    assert (await directory.recipient_context(db, sub.id, "internal-file-only"))["program"] == ""
    sub.unsubscribed_at = datetime.now(UTC)
    await db.flush()
    assert (await browse(db))["rows"][0]["reason"] == "Unsubscribed"
