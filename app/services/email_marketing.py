"""Opt-in enrollment and staff-managed audiences. No provider calls in HTTP requests."""
from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4, uuid5

from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Uuid, and_, case, exists, func, insert, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as upsert

from app.models.application_profile import ApplicationProfile, ApplicationProgramSelection
from app.models.client import Client
from app.models.dealer_prospect import DealerProspect
from app.models.email_marketing import (
    MarketingCampaign,
    MarketingDelivery,
    MarketingEnrollment,
    MarketingSubscriber,
)
from app.models.funding_program import FundingProgramCatalog
from app.models.prospect_outreach import EmailSuppression
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.schemas.email_marketing_audience import RecipientSelection
from app.services import marketing_content
from app.services.file_clients import NO_DIRECT_CONTACT
from app.services.foreclosure_rescue import FORECLOSURE_RESCUE_VARIANT
from app.services.prospect_outreach import normalize_email, set_suppression, valid_email
from app.services.resource_assessments import _secret

CONSENT_VERSION = "lending_updates_v1"


class AudienceFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    program: str | None = Field(None, min_length=1, max_length=120)
    outcome: str | None = Field(None, min_length=1, max_length=120)
    source: str | None = Field(None, min_length=1, max_length=120)
    locale: Literal["en", "es"] | None = None


class CampaignMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=10000)
    body_html: str | None = Field(default=None, min_length=1, max_length=100000)
    filters: AudienceFilters
    selection: RecipientSelection = Field(default_factory=RecipientSelection)

    @property
    def uses_composer(self) -> bool:
        return True

    @field_validator("subject")
    @classmethod
    def single_line(cls, value: str):
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError("Enter a single-line subject.")
        return value

    @model_validator(mode="after")
    def validate_content(self):
        if not self.body and not self.body_html:
            raise ValueError("Enter a text message or paste an HTML email.")
        if self.uses_composer:
            marketing_content.validate_template(self.subject, self.body, self.body_html)
        return self


class CampaignInput(CampaignMessage):
    id: UUID
    name: str = Field(min_length=1, max_length=120)
    scheduled_at: AwareDatetime | None = None
    expected_recipient_count: int | None = Field(default=None, ge=0, strict=True)

    @property
    def uses_composer(self) -> bool:
        # Existing clients post a literal plain-text message with no selection,
        # HTML or schedule. Preserve even a sentence containing double braces.
        return not self.model_fields_set.isdisjoint({"selection", "body_html", "scheduled_at"})

    @field_validator("scheduled_at")
    @classmethod
    def in_utc(cls, value):
        return value.astimezone(UTC) if value else None


class CampaignPreview(CampaignMessage):
    sample_id: UUID | None = None


async def queue_step(db, subscriber_id: UUID, step: int, due_at: datetime):
    await db.execute(upsert(MarketingDelivery).values(
        subscriber_id=subscriber_id, purpose_key=f"follow:{step}", step=step, due_at=due_at,
        status="queued",
    ).on_conflict_do_nothing(index_elements=["subscriber_id", "purpose_key"]))


async def enroll(db, *, email: str, consent: bool, source: str, program: str | None = None,
                 outcome: str | None = None, locale: str = "en", client_id: UUID | None = None,
                 intake_id: UUID | None = None):
    """Join the caller's transaction. Repeat forms add provenance, never restart the sequence."""
    address = normalize_email(email)
    if consent is not True or not valid_email(address) or len(address) > 320:
        return
    language = "es" if locale == "es" else "en"
    subscriber_id, withdrawn = (await db.execute(upsert(MarketingSubscriber).values(
        id=uuid4(), email=address, locale=language,
    ).on_conflict_do_update(index_elements=["email"], set_={"locale": language}).returning(
        MarketingSubscriber.id, MarketingSubscriber.unsubscribed_at))).one()
    if withdrawn:
        return
    enrollment_key = uuid5(subscriber_id, f"{source}|{program}|{outcome}|{intake_id}|{client_id}")
    await db.execute(upsert(MarketingEnrollment).values(
        id=enrollment_key, subscriber_id=subscriber_id, source=source[:120], program=(program or "")[:120] or None,
        outcome=(outcome or "")[:120] or None, client_id=client_id, intake_id=intake_id, consent_version=CONSENT_VERSION,
    ).on_conflict_do_nothing(index_elements=["id"]))
    await queue_step(db, subscriber_id, 0, datetime.now(UTC))


def intake_owner_email():
    intake = PublicUnderwritingIntake
    # A broker/attorney's foreclosure submission stores THEIR address in the
    # intake email. File outcomes and owner contact holds belong to the owner.
    third_party = and_(intake.variant == FORECLOSURE_RESCUE_VARIANT,
                       func.coalesce(intake.referral_source, "") != "owner_direct")
    client_email = select(Client.email).where(Client.id == intake.client_id).correlate(intake).scalar_subquery()
    return func.lower(func.trim(case((third_party, func.coalesce(
        client_email, intake.intake_state["foreclosure_rescue"]["client_email"].as_string(),
    )), else_=intake.email)))


def contact_hold_reason(address):
    intake = PublicUnderwritingIntake
    suppression = select(EmailSuppression.reason).where(EmailSuppression.email_normalized == address,
        EmailSuppression.active.is_(True)).correlate_except(EmailSuppression).scalar_subquery()
    return case(
        (suppression.is_not(None), literal("Email suppression: ") + suppression),
        (exists().where(DealerProspect.email_normalized == address, DealerProspect.do_not_contact.is_(True))
         .correlate_except(DealerProspect), "Prospect marked do not contact"),
        (exists().where(func.lower(func.trim(Client.email)) == address, Client.contact_permission == NO_DIRECT_CONTACT)
         .correlate_except(Client), "Client contact hold"),
        (exists().where(intake_owner_email() == address, intake.client_contact_suppressed.is_(True))
         .correlate_except(intake), "Borrower contact hold"),
    )


def eligible_ids(filters: AudienceFilters):
    return matching_subscribers(filters).where(MarketingSubscriber.unsubscribed_at.is_(None),
                                               contact_hold_reason(MarketingSubscriber.email).is_(None))


def matching_subscribers(filters: AudienceFilters):
    subscriber = MarketingSubscriber
    intake = PublicUnderwritingIntake
    owner_email = intake_owner_email()
    query = select(subscriber.id)
    if filters.locale:
        query = query.where(subscriber.locale == filters.locale)
    capture = MarketingEnrollment
    contexts = select(capture.id).where(capture.subscriber_id == subscriber.id).correlate(subscriber)
    if filters.source and filters.source.startswith("database:"):
        from app.services.email_marketing_directory import origin_addresses

        query = query.where(subscriber.email.in_(origin_addresses(filters.source)))
    elif filters.source:
        contexts = contexts.where(capture.source == filters.source)
    if filters.program or filters.outcome:
        # Compare an outcome and a selected program on the same application.
        # Address-only form captures may acquire a funding application later.
        contexts = contexts.outerjoin(intake, and_(
            owner_email == subscriber.email,
            or_(intake.id == capture.intake_id,
                and_(capture.intake_id.is_(None), or_(capture.client_id.is_(None), capture.client_id == intake.client_id))),
            intake.archived_at.is_(None),
        )).outerjoin(ApplicationProfile, ApplicationProfile.intake_id == intake.id).where(
            or_(capture.intake_id.is_(None), intake.id.is_not(None)))
        if filters.outcome:
            if filters.outcome.startswith("file:"):
                contexts = contexts.where(ApplicationProfile.underwriting_status == filters.outcome[5:])
            else:
                contexts = contexts.where(func.coalesce(intake.outcome_status, capture.outcome) == filters.outcome)
        if filters.program:
            active = select(ApplicationProgramSelection.id).where(
                ApplicationProgramSelection.profile_id == ApplicationProfile.id,
                ApplicationProgramSelection.removed_at.is_(None),
            ).correlate(ApplicationProfile)
            contexts = contexts.where(or_(
                active.where(ApplicationProgramSelection.program_key == filters.program).exists(),
                and_(~active.exists(), case(
                    (capture.intake_id.is_not(None), func.coalesce(capture.program, intake.variant)),
                    else_=func.coalesce(intake.variant, capture.program),
                ) == filters.program),
            ))
    return query.where(contexts.exists())


def unsubscribe_token(subscriber_id: UUID):
    identifier = subscriber_id.hex
    signature = hmac.new(_secret(), ("lending-updates:" + identifier).encode(), hashlib.sha256).hexdigest()
    return f"{identifier}.{signature}"


def verify_token(token: str) -> UUID:
    try:
        identifier, _ = token.split(".", 1)
        subscriber_id = UUID(hex=identifier)
        if not hmac.compare_digest(token.encode(), unsubscribe_token(subscriber_id).encode()):
            raise ValueError
    except (AttributeError, ValueError):
        raise HTTPException(404, "Invalid unsubscribe link.") from None
    return subscriber_id


def _utc_instant(value: datetime | None):
    # PostgreSQL returns an offset; SQLite reads an otherwise identical UTC
    # TIMESTAMP without one. Always compare and serialize absolute instants.
    if value is None:
        return None
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


async def unsubscribe(db, token: str):
    subscriber = await db.get(MarketingSubscriber, verify_token(token))
    if not subscriber:
        raise HTTPException(404, "Invalid unsubscribe link.")
    await set_suppression(db, email=subscriber.email, reason="unsubscribe", source="email_marketing")
    subscriber.unsubscribed_at = subscriber.unsubscribed_at or datetime.now(UTC)
    await db.commit()


async def filter_options(db):
    async def values(column):
        return [val for val in await db.scalars(select(column).distinct().where(column.is_not(None))) if val]

    def labels(values):
        return [{"value": val, "label": val.replace("_", " ").title()} for val in sorted(set(values))]

    programs = {p: p.replace("_", " ").title() for p in
                [*await values(MarketingEnrollment.program), *await values(PublicUnderwritingIntake.variant)]}
    programs.update(dict((await db.execute(select(
        ApplicationProgramSelection.program_key, ApplicationProgramSelection.program_name,
    ).where(ApplicationProgramSelection.removed_at.is_(None)).distinct())).all()))
    programs.update(dict((await db.execute(select(FundingProgramCatalog.program_key, FundingProgramCatalog.name)
                                          .where(FundingProgramCatalog.status == "active"))).all()))
    outcomes = labels([*await values(PublicUnderwritingIntake.outcome_status), *await values(MarketingEnrollment.outcome)])
    outcomes.extend({"value": f"file:{item['value']}", "label": "File · " + item["label"]}
                    for item in labels(await values(ApplicationProfile.underwriting_status)))
    from app.services.email_marketing_directory import ORIGINS

    sources = labels(await values(MarketingEnrollment.source))
    sources.extend({"value": "database:" + code, "label": "Directory · " + name} for code, name in ORIGINS.items())
    return {"programs": [{"value": k, "label": v} for k, v in sorted(programs.items(), key=lambda i: i[1])],
            "outcomes": outcomes, "sources": sources,
            "locales": [{"value": "en", "label": "English"}, {"value": "es", "label": "Español"}]}


async def preview_campaign(db, payload: CampaignPreview):
    from app.config import get_settings
    from app.services.email_marketing_directory import recipient_context, selected_ids

    recipients = selected_ids(payload.filters, payload.selection).subquery()
    count = await db.scalar(select(func.count()).select_from(recipients))
    if not count:
        raise HTTPException(422, "Select at least one eligible recipient to preview.")
    query = select(MarketingSubscriber).join(recipients, recipients.c.id == MarketingSubscriber.id)
    if payload.sample_id is not None:
        query = query.where(MarketingSubscriber.id == payload.sample_id)
    sample = await db.scalar(query.order_by(MarketingSubscriber.email, MarketingSubscriber.id).limit(1))
    if sample is None:
        raise HTTPException(422, "This preview recipient is outside the current selection.")
    context = await recipient_context(db, sample.id, program_key=payload.filters.program)
    rendered = marketing_content.render_message(
        subject=payload.subject, body=payload.body, body_html=payload.body_html, values=context,
        locale=sample.locale, mailing_address=get_settings().prospect_mailing_address.strip(),
        unsubscribe_url="https://example.invalid/unsubscribe-preview",
    )
    return {"subject": rendered.subject, "body_text": rendered.body_text, "body_html": rendered.body_html,
            "eligible_count": count, "sample_id": sample.id, "sample_email": sample.email}


async def create_campaign(db, actor_id, payload: CampaignInput):
    from app.services.email_marketing_directory import selected_ids

    now = datetime.now(UTC)
    values = payload.model_dump(exclude={"filters", "selection", "expected_recipient_count"})
    values["filters"] = payload.filters.model_dump(exclude_none=True)
    values["selection"] = payload.selection.model_dump(mode="json") if payload.uses_composer else None
    saved = await db.scalar(upsert(MarketingCampaign).values(**values, created_by=actor_id)
                            .on_conflict_do_nothing(index_elements=["id"]).returning(MarketingCampaign.id))
    if saved:
        if payload.scheduled_at is not None and payload.scheduled_at <= now:
            await db.rollback()
            raise HTTPException(422, "Choose a future date and time, or send now.")
        candidates = selected_ids(payload.filters, payload.selection).add_columns(
            literal(payload.id, type_=Uuid()), literal(str(payload.id)), literal(payload.scheduled_at or now), literal("queued"),
        )
        await db.execute(insert(MarketingDelivery).from_select(
            ["subscriber_id", "campaign_id", "purpose_key", "due_at", "status"], candidates))
        frozen_count = await db.scalar(select(func.count()).select_from(MarketingDelivery).where(
            MarketingDelivery.campaign_id == payload.id))
        if payload.expected_recipient_count is not None and frozen_count != payload.expected_recipient_count:
            await db.rollback()
            raise HTTPException(409, "The audience changed. Review the updated recipient count and confirm again.")
        if not frozen_count:
            await db.rollback()
            raise HTTPException(422, "No eligible subscribers match this audience.")
        await db.commit()
    else:
        existing = await db.get(MarketingCampaign, payload.id)
        existing_values = {field: getattr(existing, field) for field in values}
        existing_values["scheduled_at"] = _utc_instant(existing.scheduled_at)
        if existing.created_by != actor_id or existing_values != values:
            raise HTTPException(409, "This campaign identifier has already been used.")
    return (await list_campaigns(db, campaign_id=payload.id))[0]


async def list_campaigns(db, campaign_id=None):
    fields = ("id", "name", "subject", "body", "filters", "created_at", "scheduled_at")
    query = select(*(getattr(MarketingCampaign, field) for field in fields)).order_by(
        MarketingCampaign.created_at.desc(), MarketingCampaign.id)
    if campaign_id:
        query = query.where(MarketingCampaign.id == campaign_id)
    campaigns = (await db.execute(query.limit(100))).all()
    stats = (await db.execute(select(
        MarketingDelivery.campaign_id, MarketingDelivery.status, func.count(),
    ).where(MarketingDelivery.campaign_id.in_([c.id for c in campaigns]))
      .group_by(MarketingDelivery.campaign_id, MarketingDelivery.status))).all()
    counts = {c.id: dict.fromkeys(("queued", "sending", "sent", "failed", "uncertain", "blocked"), 0) for c in campaigns}
    for cid, status, n in stats:
        counts[cid][status] = n
    return [{**dict(c._mapping), "scheduled_at": _utc_instant(c.scheduled_at),
             **counts[c.id], "recipient_count": sum(counts[c.id].values())} for c in campaigns]
