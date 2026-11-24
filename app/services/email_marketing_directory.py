"""A read-only, normalized email directory. Membership is separate from consent."""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import String, and_, case, false, func, literal, or_, select, union_all

from app.capital_partners.models import CpAccessRequest
from app.dealer_os.models import DealerRepContact
from app.models.activity import Activity
from app.models.application_profile import ApplicationProfile, ApplicationProgramSelection
from app.models.arf_channel import ARFReferral
from app.models.booking_notification import BookingNotification
from app.models.capital_partner_application import CapitalPartnerApplication
from app.models.client import Client
from app.models.dealer_prospect import DealerProspect, DealerProspectOutcomeDefinition
from app.models.email_marketing import MarketingEnrollment, MarketingSubscriber
from app.models.funding_program import FundingProgramCatalog
from app.models.professional_partner_application import ProfessionalPartnerApplication
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.resource_assessment import ResourceAssessment
from app.schemas.email_marketing_audience import RecipientSelection
from app.services.email_marketing import (
    AudienceFilters,
    contact_hold_reason,
    eligible_ids,
    matching_subscribers,
)
from app.services.foreclosure_rescue import FORECLOSURE_RESCUE_VARIANT
from app.services.prospect_outreach import normalize_email, valid_email

ORIGINS = {
    "crm_client": "CRM client", "loan_application": "Funding application", "crm_prospect": "Marketing prospect",
    "crm_contact": "Field Desk contact", "public_booking": "Appointment", "support_inquiry": "Support inquiry",
    "business_funding_check": "Business funding check", "business_capital_referral": "Business capital referral",
    "capital_partner_application": "Capital partner application", "foreclosure_partner_application": "Professional partner application",
    "capital_access_request": "Capital access request", "subscription": "Email marketing",
}


class DirectoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    filters: AudienceFilters = Field(default_factory=AudienceFilters)
    search: str = Field("", max_length=200)
    eligibility: Literal["all", "eligible", "ineligible"] = "all"
    selected_only: bool = False
    selection: RecipientSelection = Field(default_factory=lambda: RecipientSelection(mode="explicit"))
    page: int = Field(1, ge=1, le=10000000)
    page_size: int = Field(25, ge=1, le=100)


def _text(value):
    return literal(value, String()) if value is None or isinstance(value, str) else value


def _record(email, name, company, origin, priority, *, locale=None, program=None, outcome=None, stage=None):
    return select(func.lower(func.trim(email)).label("email"), _text(name).label("name"), _text(company).label("business"),
                  literal(origin).label("origin"), literal(priority).label("priority"), _text(locale).label("locale"),
                  _text(program).label("program"), _text(outcome).label("outcome"), _text(stage).label("stage"))


def _origins(*, filter_files=False):
    c, i, p, rep = Client, PublicUnderwritingIntake, DealerProspect, DealerRepContact
    a, b, r = Activity, BookingNotification, ResourceAssessment
    cap, professional, access = CapitalPartnerApplication, ProfessionalPartnerApplication, CpAccessRequest
    # Third-party foreclosure forms identify the referring professional. Display
    # that contact without assigning the owner's company or lending outcome.
    referred = and_(i.variant == FORECLOSURE_RESCUE_VARIANT, func.coalesce(i.referral_source, "") != "owner_direct")
    file_program = func.coalesce(ApplicationProgramSelection.program_key, i.variant) if filter_files else i.variant
    intakes = _record(i.email, i.full_name, case((~referred, i.business_name)), "loan_application", 10,
                      locale=i.preferred_language, program=case((~referred, file_program)),
                      outcome=case((~referred, i.outcome_status)), stage=case((~referred, ApplicationProfile.underwriting_status))
                      if filter_files else None).where(i.archived_at.is_(None))
    if filter_files:
        intakes = intakes.select_from(i).outerjoin(ApplicationProfile, ApplicationProfile.intake_id == i.id).outerjoin(
            ApplicationProgramSelection, and_(ApplicationProgramSelection.profile_id == ApplicationProfile.id,
                                               ApplicationProgramSelection.removed_at.is_(None)))
    prospects = _record(p.email_normalized, case((func.lower(func.trim(rep.email)) == p.email_normalized, rep.full_name)),
                        p.dealer_name_normalized, "crm_prospect", 30, program=p.funding_intent,
                        outcome=(literal("prospect:") + DealerProspectOutcomeDefinition.key) if filter_files else None
                        ).select_from(p).outerjoin(rep, rep.id == p.primary_contact_id).where(
                            p.archived_at.is_(None), rep.archived_at.is_(None))
    if filter_files:
        prospects = prospects.outerjoin(DealerProspectOutcomeDefinition,
                                        DealerProspectOutcomeDefinition.id == p.last_outcome_definition_id)
    language = case((func.lower(c.language).in_(("es", "spanish", "español")), "es"),
                    (func.lower(c.language).in_(("en", "english")), "en"))
    return {
        "crm_client": _record(c.email, c.name, c.lead_intake["business_name"].as_string(), "crm_client", 20,
                              locale=language, program=c.lead_intake["funding_intent"].as_string(),
                              outcome=c.lead_intake["review_status"].as_string()),
        "loan_application": intakes, "crm_prospect": prospects,
        "crm_contact": _record(rep.email, rep.full_name, rep.company, "crm_contact", 25).where(rep.archived_at.is_(None)),
        "business_funding_check": _record(r.contact["email"].as_string(), r.contact["name"].as_string(),
             r.contact["business_name"].as_string(), "business_funding_check", 15, locale=r.locale,
             program=r.goal, outcome="resource_requested"),
        "public_booking": _record(b.invitee_email, b.invitee_name, None, "public_booking", 50, outcome="booked"),
        "support_inquiry": _record(a.payload["email"].as_string(), a.payload["full_name"].as_string(), None,
                                   "support_inquiry", 50, outcome="inquiry_received").where(a.kind == "support.inquiry"),
        "business_capital_referral": _record(ARFReferral.email, ARFReferral.full_name, ARFReferral.business_name,
                                    "business_capital_referral", 18, program=ARFReferral.program_key, outcome="inquiry_received"),
        "capital_partner_application": _record(cap.contact_email, cap.contact_name, cap.company_name, "capital_partner_application",
                                               35, outcome=cap.status),
        "foreclosure_partner_application": _record(professional.contact_email, professional.contact_name, professional.company_name,
                                                    "foreclosure_partner_application", 35, outcome=professional.status),
        "capital_access_request": _record(access.email, access.full_name, access.entity_name, "capital_access_request",
                                          35, locale=access.locale, outcome=access.status),
        "subscription": _record(MarketingSubscriber.email, None, None, "subscription", 100, locale=MarketingSubscriber.locale),
    }


def origin_addresses(source):
    stmt = _origins().get(source.removeprefix("database:"))
    if stmt is None:
        return select(literal(None, String())).where(false())
    return select(stmt.subquery().c.email)


def prospect_addresses(filters):
    """Apply the whole intent/outcome pair to a single active opportunity."""
    rows = _origins(filter_files=True)["crm_prospect"].subquery()
    # Language belongs to the subscribing person, not their prospect row.
    return _raw_matches(rows, filters.model_copy(update={"locale": None}))


async def prospect_options(db):
    prospect = DealerProspect
    outcome = DealerProspectOutcomeDefinition
    intents = await db.scalars(select(prospect.funding_intent).distinct().where(
        prospect.archived_at.is_(None), prospect.funding_intent.is_not(None)))
    dispositions = await db.execute(select(outcome.key, outcome.label).where(or_(
        outcome.is_active.is_(True), outcome.id.in_(select(prospect.last_outcome_definition_id)
            .where(prospect.archived_at.is_(None))))).order_by(outcome.sort_order, outcome.key))
    return list(intents), [{"value": "prospect:" + key, "label": "Prospect · " + label} for key, label in dispositions]


def _identities(*, filter_files=False):
    return union_all(*_origins(filter_files=filter_files).values()).cte()


def _ranked(raw):
    # Canonical name + business are selected from the same record, with stable
    # ordering even for shared/group addresses. All aliases remain searchable.
    rank = func.row_number().over(partition_by=raw.c.email, order_by=(
        case((func.nullif(func.trim(raw.c.name), "").is_(None), 1), else_=0), raw.c.priority,
        raw.c.name, raw.c.business, raw.c.origin,
    ))
    rows = select(raw, rank.label("position")).where(raw.c.email.is_not(None), raw.c.email != "").subquery()
    return select(rows).where(rows.c.position == 1).subquery()


def _search_emails(raw, search):
    return select(raw.c.email).where(or_(*(
        column.icontains(search, autoescape=True) for column in (raw.c.email, raw.c.name, raw.c.business)
    )))


def selected_ids(filters: AudienceFilters, selection: RecipientSelection | None, *, raw=None):
    query = eligible_ids(filters)
    if selection is None:
        return query
    subscriber = MarketingSubscriber
    included = subscriber.id.in_(selection.included_ids)
    if selection.mode == "explicit":
        query = query.where(included)
    elif selection.search:
        if raw is None:
            raw = _identities()
        query = query.where(or_(subscriber.email.in_(_search_emails(raw, selection.search)), included))
    if selection.excluded_ids:
        query = query.where(subscriber.id.not_in(selection.excluded_ids))
    return query


async def directory_contains(db, email):
    if not valid_email(email):
        return False
    raw = _identities()
    return bool(await db.scalar(select(select(raw.c.email).where(raw.c.email == normalize_email(email)).exists())))


def _raw_matches(raw, filters):
    query = select(raw.c.email)
    if filters.source:
        query = query.where(raw.c.origin == filters.source.removeprefix("database:"))
    if filters.program:
        query = query.where(raw.c.program == filters.program)
    if filters.outcome:
        outcome = filters.outcome
        if filters.source == "database:crm_prospect" and ":" not in outcome:
            outcome = "prospect:" + outcome
        query = query.where(raw.c.stage == outcome[5:] if outcome.startswith("file:") else raw.c.outcome == outcome)
    if filters.locale:
        query = query.where(raw.c.locale == filters.locale)
    return query


async def directory(db, request: DirectoryRequest):
    filters = request.filters
    raw = _identities(filter_files=bool(filters.program or filters.outcome))
    identity = _ranked(raw)
    subscriber = MarketingSubscriber
    # First determine which addresses match selection criteria independently of
    # permissions. Then label holds, withdrawals, and missing consent separately.
    member_ids = matching_subscribers(filters)
    selected = selected_ids(filters, request.selection, raw=raw).cte()
    has_consent = select(MarketingEnrollment.id).where(MarketingEnrollment.subscriber_id == subscriber.id).exists()
    hold = contact_hold_reason(identity.c.email)
    reason = case(
        (subscriber.unsubscribed_at.is_not(None), "Unsubscribed"),
        (hold.is_not(None), hold),
        (or_(subscriber.id.is_(None), ~has_consent), "No recorded marketing email permission"),
    )
    query = select(identity.c.email, identity.c.name, identity.c.origin, subscriber.id, reason.label("reason"),
                   subscriber.id.in_(select(selected.c.id)).label("selected")).outerjoin(subscriber, subscriber.email == identity.c.email)
    if filters.model_dump(exclude_none=True):
        query = query.where(or_(subscriber.id.in_(member_ids), and_(or_(subscriber.id.is_(None), ~has_consent),
                                                               identity.c.email.in_(_raw_matches(raw, filters)))))
    if request.search:
        query = query.where(identity.c.email.in_(_search_emails(raw, request.search)))
    directory_rows = query.subquery()
    hits = select(directory_rows)
    if request.eligibility == "eligible":
        hits = hits.where(directory_rows.c.reason.is_(None))
    elif request.eligibility == "ineligible":
        hits = hits.where(directory_rows.c.reason.is_not(None))
    if request.selected_only:
        hits = hits.where(directory_rows.c.selected.is_(True))
    page = hits.subquery()
    count, eligible = (await db.execute(select(func.count(), func.count(case((page.c.reason.is_(None), 1)))).select_from(page))).one()
    selected_count = await db.scalar(select(func.count()).select_from(selected))
    rows = (await db.execute(select(page).order_by(page.c.email).offset(
        (request.page - 1) * request.page_size).limit(request.page_size))).mappings()
    return {"page": request.page, "page_size": request.page_size, "total_count": count, "eligible_count": eligible,
            "selected_count": selected_count, "rows": [{
                "email": row.email, "name": row.name, "source": ORIGINS[row.origin], "id": row.id,
                "eligible": row.reason is None, "reason": row.reason, "selected": bool(row.selected),
                "can_record_consent": row.reason == "No recorded marketing email permission" and valid_email(row.email),
            } for row in rows]}


async def recipient_context(db, subscriber_id: UUID, program_key: str | None = None):
    empty = dict.fromkeys(("full_name", "first_name", "business_name", "email", "program"), "")
    email = await db.scalar(select(MarketingSubscriber.email).where(MarketingSubscriber.id == subscriber_id))
    if not email:
        return empty
    raw = _identities()
    identity = _ranked(select(raw).where(raw.c.email == email).subquery())
    name, company = (await db.execute(select(identity.c.name, identity.c.business))).one()
    program_query = select(FundingProgramCatalog.name).where(FundingProgramCatalog.status == "active")
    if program_key:
        program_query = program_query.where(FundingProgramCatalog.program_key == program_key)
    else:
        program_query = program_query.join(MarketingEnrollment, MarketingEnrollment.program == FundingProgramCatalog.program_key
                    ).where(MarketingEnrollment.subscriber_id == subscriber_id).order_by(
                        MarketingEnrollment.created_at.desc(), MarketingEnrollment.id)
    name = (name or "").strip()
    return {"full_name": name, "first_name": name.split()[0] if name else "", "business_name": (company or "").strip(),
            "email": email, "program": (await db.scalar(program_query.limit(1))) or ""}
