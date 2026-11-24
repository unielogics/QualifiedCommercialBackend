from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.dealer_os.models import DealerRepContact
from app.models.activity import Activity
from app.models.client import Client
from app.models.dealer_prospect import DealerProspect, DealerProspectOutcomeDefinition
from app.models.email_marketing import (
    MarketingCampaign,
    MarketingDelivery,
    MarketingPermissionRequest,
    MarketingSubscriber,
)
from app.models.funding_program import FundingProgramCatalog
from app.models.prospect_outreach import EmailSuppression
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.schemas.email_marketing_audience import RecipientSelection
from app.services import email_marketing as marketing
from app.services import email_marketing_delivery as delivery
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


async def test_crm_funding_intent_and_disposition_follow_the_same_opportunity(db):
    answered = DealerProspectOutcomeDefinition(key="reached", label="Spoke with owner")
    missed = DealerProspectOutcomeDefinition(key="no_answer", label="No answer")
    db.add_all([answered, missed])
    await db.flush()
    updates = []
    for number, (email, intent, result) in enumerate([
        ("owner@example.org", "working_capital", answered),
        ("owner@example.org", "equipment", missed),
        ("historical@example.org", "working_capital", answered),
    ]):
        contact = DealerRepContact(full_name="Example Owner", email=email)
        db.add(contact)
        await db.flush()
        opportunity = DealerProspect(company_id=uuid4(), primary_contact_id=contact.id, stage_definition_id=uuid4(),
            last_outcome_definition_id=result.id, funding_intent=intent, email_normalized=email,
            phone_normalized=f"+1201555000{number}", dealer_name_normalized=f"store {number}")
        db.add(opportunity)
        updates.append(opportunity)
    await marketing.enroll(db, email="owner@example.org", consent=True, source="support_inquiry")
    sub = await db.scalar(select(MarketingSubscriber))
    filters = marketing.AudienceFilters(program="working_capital", outcome="prospect:reached", source="database:crm_prospect")
    options = await marketing.filter_options(db)
    assert {"value": "prospect:reached", "label": "Prospect · Spoke with owner"} in options["outcomes"]
    assert "working_capital" in {o["value"] for o in options["programs"]}
    result = await browse(db, filters=filters, selection={"mode": "all"})
    assert (result["total_count"], result["eligible_count"], result["selected_count"]) == (2, 1, 2)
    assert (result["selected_eligible_count"], result["selected_requestable_count"]) == (1, 1)
    assert list(await db.scalars(directory.selected_ids(filters, None))) == [sub.id]
    for patch in [{"source": None}, {"source": None, "outcome": None}]:
        assert list(await db.scalars(marketing.eligible_ids(filters.model_copy(update=patch)))) == [sub.id]
    for patch in [{"program": "equipment"}, {"source": "support_inquiry"}, {"outcome": "prospect:no_answer"}]:
        assert list(await db.scalars(marketing.eligible_ids(filters.model_copy(update=patch)))) == []

    inputs = marketing.CampaignInput(id=uuid4(), name="Updates", subject="An update", body="News", filters=filters)
    campaign = await marketing.create_campaign(db, uuid4(), inputs)
    assert campaign["recipient_count"] == 1
    updates[0].last_outcome_definition_id = missed.id
    await db.commit()
    job = await db.scalar(select(MarketingDelivery).where(MarketingDelivery.campaign_id == inputs.id))
    # Earlier selection is frozen, but the recipient no longer matches at send time.
    from unittest.mock import AsyncMock, patch
    with patch.object(delivery, "_lock_suppression_addresses", new=AsyncMock()):
        await delivery.dispatch_one(db, job.id, datetime.now(UTC))
    assert job.status == "blocked"


async def test_select_all_directory_contacts_and_partition_by_permitted_action(db):
    for address in ["a-member@example.org", "b-history@example.org", "c-held@example.org", "d-malformed"]:
        db.add(Client(name="Owner", email=address))
    await marketing.enroll(db, email="a-member@example.org", consent=True, source="support")
    db.add(EmailSuppression(email_normalized="c-held@example.org", reason="complaint", source="ses_event", active=True))
    await db.flush()
    member = await db.scalar(select(MarketingSubscriber))
    all_selected = RecipientSelection(mode="all")
    first = await browse(db, selection=all_selected, page_size=1)
    assert [first[key] for key in ("selected_count", "selected_eligible_count", "selected_requestable_count",
                                   "selected_blocked_count")] == [4, 1, 1, 2]
    second = await browse(db, selection=all_selected, page=2, page_size=1)
    assert second["rows"][0]["selected"] and second["rows"][0]["id"] is None
    addresses = directory.selected_contacts(marketing.AudienceFilters(), all_selected).subquery()
    permitted = await db.scalars(select(addresses.c.email).where(addresses.c.status == "requestable"))
    assert list(permitted) == ["b-history@example.org"]
    assert list(await db.scalars(directory.selected_ids(marketing.AudienceFilters(), all_selected))) == [member.id]
    # An ID cannot override an excluded email. A contact with no subscription
    # can still be selected, including a held contact, without becoming eligible.
    explicit = RecipientSelection(mode="explicit", included_ids=[member.id],
        included_emails=[" A-MEMBER@example.ORG ", "c-held@example.org", "b-history@example.org", "invented@example.org"],
        excluded_emails=["a-member@example.org"])
    result = await browse(db, selection=explicit, selected_only=True)
    assert [r["email"] for r in result["rows"]] == ["b-history@example.org", "c-held@example.org"]
    assert result["selected_eligible_count"] == 0
    assert list(await db.scalars(directory.selected_ids(marketing.AudienceFilters(), explicit))) == []
    by_status = RecipientSelection(mode="all", eligibility="requestable", included_emails=["c-held@example.org"])
    assert (await browse(db, selection=by_status))["selected_count"] == 2
    assert not await directory.requestable_contact(db, "a-member@example.org", ignore_prior_request=True)
    assert not await directory.requestable_contact(db, "c-held@example.org", ignore_prior_request=True)
    assert await directory.requestable_contact(db, "b-history@example.org")
    assert not await directory.requestable_contact(db, "invented@example.org")
    assert (await browse(db, selection=all_selected, search="malformed"))["rows"][0]["reason"] == "Invalid email address"


async def test_existing_permission_task_can_be_invited_once_and_closed_requests_stay_closed(db):
    now = datetime.now(UTC)
    for phase in ("task_only", "queued", "sent", "declined", "failed", "expired"):
        address = f"{phase}@example.org"
        db.add(Client(name=phase, email=address))
        db.add(MarketingPermissionRequest(email=address, locale="en", status=phase, task_event_id=uuid4(),
                                          batch_id=uuid4(), due_at=now))
    await db.flush()
    selected = RecipientSelection(mode="all")
    result = await browse(db, selection=selected)
    assert (result["selected_count"], result["selected_requestable_count"], result["selected_blocked_count"]) == (6, 1, 5)
    assert all(row["selected"] and row["task_event_id"] for row in result["rows"])
    assert await directory.requestable_contact(db, "task_only@example.org")
    assert not await directory.requestable_contact(db, "queued@example.org")
    assert await directory.requestable_contact(db, "queued@example.org", ignore_prior_request=True)
    assert not await directory.requestable_contact(db, "declined@example.org", ignore_prior_request=True)
    await marketing.enroll(db, email="sent@example.org", consent=True, source="application")
    await db.flush()
    confirmed = (await browse(db, selection=selected, search="sent@"))["rows"][0]
    assert confirmed["eligible"] and not confirmed["can_record_consent"]


async def test_idempotent_campaign_replay_with_previous_selection_schema(db):
    await marketing.enroll(db, email="legacy-subscriber@example.org", consent=True, source="contact")
    subscriber_id = await db.scalar(select(MarketingSubscriber.id))
    payload = marketing.CampaignInput(id=uuid4(), name="Guides", subject="Funding guide", body="News",
        filters=marketing.AudienceFilters(), selection=RecipientSelection(mode="explicit", included_ids=[subscriber_id]))
    actor = uuid4()
    assert (await marketing.create_campaign(db, actor, payload))["recipient_count"] == 1
    campaign = await db.get(MarketingCampaign, payload.id)
    campaign.selection = {"mode": "explicit", "included_ids": [str(subscriber_id)], "excluded_ids": [], "search": ""}
    await db.commit()
    assert (await marketing.create_campaign(db, actor, payload))["recipient_count"] == 1
    assert await db.scalar(select(func.count()).select_from(MarketingDelivery).where(MarketingDelivery.campaign_id == payload.id)) == 1


async def test_public_form_directory_before_and_after_unrelated_opt_in(db):
    def intake(email, form, status, locale="es", source_kind="public_form", **state):
        return PublicUnderwritingIntake(email=email, full_name="Applicant", token_hash=uuid4().hex, bucket_id=uuid4(),
            variant="dealer", intake_state={"source": form, **state}, source_kind=source_kind,
            preferred_language=locale, outcome_status=status)
    email = "applicant@example.org"
    db.add_all([intake(email, "dealer_ai_intake", "closed"), intake(email, "dealer_ai_intake", "denied",
        funding_intent="equipment financing"), intake("office@example.org", "dealer_ai_intake", "closed", source_kind="operator"),
        Client(name="Unknown language", email="unknown@example.org"),
        Activity(actor_label="public", kind="support.inquiry", summary="Support", payload={
            "full_name": "Historical Customer", "email": "historic@example.org"}),
        Client(name="MCA Lead", email="mca@example.org", source_channel="mca_funding", language="Spanish",
               lead_intake={"consent_version": "mca-funding-review-v2", "funding_intent": "merchant_cash_advance",
                            "review_status": "pending_review"})])
    await db.flush()
    options = await marketing.filter_options(db)
    assert {"dealer_intake", "equipment_intake", "support_inquiry", "mca_lead"} <= {x["value"] for x in options["sources"]}
    assert {"dealer", "equipment_financing", "merchant_cash_advance"} <= {x["value"] for x in options["programs"]}
    assert {"denied", "closed", "application_started", "pending_review", "inquiry_received"} <= {
        x["value"] for x in options["outcomes"]}
    filters = marketing.AudienceFilters(source="dealer_intake", program="dealer", outcome="closed", locale="es")
    async def addresses(**changes):
        rows = (await browse(db, filters=filters.model_copy(update=changes), selection={"mode": "all"}))["rows"]
        return [(r["email"], r["status"]) for r in rows]
    assert await addresses() == [(email, "requestable")]
    assert await addresses(outcome="denied") == []
    assert await addresses(source="equipment_intake", program="equipment_financing", outcome="denied") == [(email, "requestable")]
    assert await addresses(source="support_inquiry", program=None, outcome="inquiry_received", locale=None) == [
        ("historic@example.org", "requestable")]
    assert await addresses(source="support_inquiry", program=None, outcome="inquiry_received") == []
    assert await addresses(source="mca_lead", program="merchant_cash_advance", outcome="pending_review") == [
        ("mca@example.org", "requestable")]

    await marketing.enroll(db, email=email, consent=True, source="account_signup", locale="en")
    assert await addresses() == [(email, "eligible")]
    assert await addresses(outcome="denied") == []
    assert await addresses(locale="en") == []
    subscriber_id = await db.scalar(select(MarketingSubscriber.id))
    assert list(await db.scalars(directory.selected_ids(filters, RecipientSelection(mode="all")))) == [subscriber_id]
    assert list(await db.scalars(marketing.eligible_ids(filters.model_copy(update={"outcome": "denied"})))) == []


async def test_prequalification_and_professional_forms_respect_participant_roles(db):
    from app.enums import Role
    from app.models.prequal_request import PrequalRequest
    from app.models.user import User

    borrower = User(email="borrower@example.org", role=Role.CLIENT)
    staff = User(email="staff@example.org", role=Role.SUPER_ADMIN)
    db.add_all([borrower, staff])
    await db.flush()
    db.add_all([PrequalRequest(requester_id=user.id, target_property_address="Property", purchase_price=100000,
        requested_loan_amount=50000, loan_type="dscr") for user in (borrower, staff)])
    db.add(PublicUnderwritingIntake(email="referrer@example.org", full_name="Attorney", token_hash=uuid4().hex,
        bucket_id=uuid4(), variant="commercial_foreclosure_bailout_v1", referral_source="attorney",
        source_kind="public_form", intake_state={"source": "foreclosure_rescue"}, outcome_status="denied"))
    await db.flush()
    filters = {"source": "borrower_prequalification", "program": "dscr", "outcome": "prequalification_requested"}
    assert [row["email"] for row in (await browse(db, filters=filters))["rows"]] == [borrower.email]
    rescue_rows = (await browse(db, filters={"source": "foreclosure_rescue"}))["rows"]
    assert [row["email"] for row in rescue_rows] == ["referrer@example.org"]
    for outcome in ["denied", "new_rescue"]:
        assert (await browse(db, filters={"source": "foreclosure_rescue", "outcome": outcome}))["total_count"] == 0
