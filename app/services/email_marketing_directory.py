"""A read-only, normalized email directory. Membership is separate from consent."""
from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import String, and_, case, func, literal, or_, select, union_all

from app.capital_partners.models import CpAccessRequest
from app.dealer_os.models import DealerRepAppointment, DealerRepContact
from app.enums import Role
from app.models.activity import Activity
from app.models.application_profile import ApplicationProfile, ApplicationProgramSelection
from app.models.arf_channel import ARFReferral
from app.models.booking_notification import BookingNotification
from app.models.capital_partner_application import CapitalPartnerApplication
from app.models.client import Client
from app.models.dealer_prospect import DealerProspect, DealerProspectOutcomeDefinition
from app.models.email_marketing import MarketingEnrollment, MarketingSubscriber
from app.models.event import CalendarEvent
from app.models.funding_program import FundingProgramCatalog
from app.models.prequal_request import PrequalRequest
from app.models.professional_partner_application import ProfessionalPartnerApplication
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.resource_assessment import ResourceAssessment
from app.models.user import User
from app.schemas.email_marketing_audience import DirectoryEligibility, RecipientSelection
from app.services.email_marketing import (
    AudienceFilters,
    contact_hold_reason,  # noqa: F401 (used by the consent-evidence endpoint)
    contact_restrictions,
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
    "borrower_prequalification": "Borrower prequalification",
}


# Canonical source and program as recorded by each public intake route.
INTAKE_FORMS = {
    "dealer_ai_intake": ("dealer_intake", "dealer"),
    "equipment_financing_intake": ("equipment_intake", "equipment_financing"),
    "mca_refinance": ("mca_refinance_intake", "mca_refinance"),
    "business_term_intake": ("business_term_intake", "term_loan"),
    "mca_funding_intake": ("mca_funding_intake", "merchant_cash_advance"),
    "funding_review": ("funding_review", "real_estate"),
    "foreclosure_rescue": ("foreclosure_rescue", "foreclosure_rescue"),
    "capital_readiness_diagnostic": ("capital_readiness_diagnostic", "main_street"),
}
PUBLIC_FORMS = {
    **{source: "loan_application" for source, _ in INTAKE_FORMS.values()},
    "mca_lead": "crm_client",
    **{key: key for key in ("public_booking", "support_inquiry", "business_funding_check",
       "business_capital_referral", "capital_partner_application", "foreclosure_partner_application",
       "capital_access_request", "borrower_prequalification")},
}


class DirectoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    filters: AudienceFilters = Field(default_factory=AudienceFilters)
    search: str = Field("", max_length=200)
    eligibility: DirectoryEligibility = "all"
    selected_only: bool = False
    selection: RecipientSelection = Field(default_factory=lambda: RecipientSelection(mode="explicit"))
    page: int = Field(1, ge=1, le=10000000)
    page_size: int = Field(25, ge=1, le=100)


def _text(value):
    return literal(value, String()) if value is None or isinstance(value, str) else value


def _record(email, name, company, origin, priority, *, locale=None, program=None, outcome=None, stage=None,
            form_source=None, entry_outcome=None):
    return select(func.lower(func.trim(email)).label("email"), _text(name).label("name"), _text(company).label("business"),
                  literal(origin).label("origin"), literal(priority).label("priority"), _text(locale).label("locale"),
                  _text(program).label("program"), _text(outcome).label("outcome"), _text(stage).label("stage"),
                  _text(form_source).label("form_source"), _text(entry_outcome).label("entry_outcome"))


def _intake_form(intake):
    source = intake.intake_state["source"].as_string()
    intent = func.replace(func.replace(func.lower(func.trim(intake.intake_state["funding_intent"].as_string())),
                                       "-", "_"), " ", "_")
    equipment = or_(intake.intake_state["equipment_financing_details"].as_string().is_not(None),
                    intake.intake_state["dealer_details"]["financing_equipment_or_vehicle"].as_boolean().is_(True),
                    intent.in_(("equipment", "equipment_finance", "equipment_financing", "machinery", "vehicle_financing")))
    claim_user = intake.intake_state["capital_readiness_claim"]["user_id"].as_string()
    claimed = select(Client.id).where(Client.id == intake.client_id,
        func.replace(Client.user_id.cast(String), "-", "") == func.replace(claim_user, "-", "")).correlate(intake).exists()
    accepted = intake.intake_state["legal_acceptance"]
    public = or_(intake.source_kind == "public_form", and_(intake.source_kind.is_(None),
        accepted["terms_accepted"].as_boolean().is_(True), accepted["privacy_accepted"].as_boolean().is_(True)))
    submitted_rescue = and_(intake.source_kind == "partner", source == "foreclosure_rescue",
                            accepted["entry_method"].as_string() == "public_submission")
    return case((and_(intake.source_kind == "client_capital_readiness", claimed), "capital_readiness_diagnostic"),
                (and_(public, source == "dealer_ai_intake", equipment), "equipment_financing_intake"),
                (and_(or_(public, submitted_rescue), source != "capital_readiness_diagnostic"), source))


def _origins(*, filter_files=False):
    c, i, p, rep = Client, PublicUnderwritingIntake, DealerProspect, DealerRepContact
    a, b, r = Activity, BookingNotification, ResourceAssessment
    cap, professional, access = CapitalPartnerApplication, ProfessionalPartnerApplication, CpAccessRequest
    # Third-party foreclosure forms identify the referring professional. Display
    # that contact without assigning the owner's company or lending outcome.
    referred = and_(i.variant == FORECLOSURE_RESCUE_VARIANT, func.coalesce(i.referral_source, "") != "owner_direct")
    form = _intake_form(i)
    form_source = case({k: v[0] for k, v in INTAKE_FORMS.items()}, value=form)
    default_program = case({k: v[1] for k, v in INTAKE_FORMS.items()}, value=form, else_=i.variant)
    file_program = func.coalesce(ApplicationProgramSelection.program_key, default_program) if filter_files else default_program
    rescue_locale = i.intake_state["foreclosure_rescue"]["locale"].as_string()
    language = case((and_(i.variant == FORECLOSURE_RESCUE_VARIANT, rescue_locale.in_(("en", "es"))), rescue_locale),
                    else_=i.preferred_language)
    intakes = _record(i.email, i.full_name, case((~referred, i.business_name)), "loan_application", 10,
                      locale=language, program=case((~referred, file_program)),
                      outcome=case((~referred, i.outcome_status)), stage=case((~referred, ApplicationProfile.underwriting_status))
                      if filter_files else None, form_source=form_source,
                      entry_outcome=case((and_(~referred, form.in_(INTAKE_FORMS)), case(
                          (form == "foreclosure_rescue", "new_rescue"), else_="application_started")))
                      ).where(i.archived_at.is_(None))
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
                              outcome=c.lead_intake["review_status"].as_string(),
                              form_source=case((and_(c.source_channel == "mca_funding",
                                  c.lead_intake["consent_version"].as_string() == "mca-funding-review-v2"), "mca_lead"))),
        "loan_application": intakes, "crm_prospect": prospects,
        "crm_contact": _record(rep.email, rep.full_name, rep.company, "crm_contact", 25).where(rep.archived_at.is_(None)),
        "business_funding_check": _record(r.contact["email"].as_string(), r.contact["name"].as_string(),
             r.contact["business_name"].as_string(), "business_funding_check", 15, locale=r.locale,
             program=r.goal, outcome="resource_requested", form_source=case(
                 (r.resource_id == "business_funding_check", "business_funding_check"))),
        "public_booking": _record(b.invitee_email, b.invitee_name, None, "public_booking", 50, outcome="booked",
                                 form_source=case((CalendarEvent.external_ref_kind == "public_booking", "public_booking")),
                                 program=case((DealerRepAppointment.funding_intent == "mca_refinance", "mca_refinance"),
                                              else_=DealerRepAppointment.lead_type)).select_from(b).outerjoin(
                                     CalendarEvent, CalendarEvent.id == b.event_id).outerjoin(
                                     DealerRepAppointment, DealerRepAppointment.calendar_event_id == b.event_id),
        "support_inquiry": _record(a.payload["email"].as_string(), a.payload["full_name"].as_string(), None,
                                   "support_inquiry", 50, outcome="inquiry_received", form_source="support_inquiry",
                                   locale=a.payload["locale"].as_string()).where(a.kind == "support.inquiry"),
        "business_capital_referral": _record(ARFReferral.email, ARFReferral.full_name, ARFReferral.business_name,
                                    "business_capital_referral", 18, program=ARFReferral.program_key, outcome="inquiry_received",
                                    form_source="business_capital_referral"),
        "capital_partner_application": _record(cap.contact_email, cap.contact_name, cap.company_name, "capital_partner_application",
                                               35, outcome=cap.status, form_source="capital_partner_application"),
        "foreclosure_partner_application": _record(professional.contact_email, professional.contact_name, professional.company_name,
                                                    "foreclosure_partner_application", 35, outcome=professional.status,
                                                    form_source="foreclosure_partner_application"),
        "capital_access_request": _record(access.email, access.full_name, access.entity_name, "capital_access_request",
                                          35, locale=access.locale, outcome=access.status, entry_outcome="inquiry_received",
                                          form_source="capital_access_request"),
        "borrower_prequalification": _record(User.email, User.name, PrequalRequest.borrower_entity, "borrower_prequalification",
                                          15, program=PrequalRequest.loan_type, outcome=PrequalRequest.status,
                                          entry_outcome="prequalification_requested", form_source="borrower_prequalification"
                                          ).select_from(PrequalRequest).join(User, User.id == PrequalRequest.requester_id).where(
                                              User.role == Role.CLIENT, PrequalRequest.client_id.is_(None),
                                              PrequalRequest.parent_prequal_request_id.is_(None)),
        "subscription": _record(MarketingSubscriber.email, None, None, "subscription", 100, locale=MarketingSubscriber.locale),
    }


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


def _status_matches(status, eligibility):
    if eligibility == "all":
        return literal(True)
    if eligibility == "ineligible":
        return status != "eligible"
    return status == eligibility


def _selection_match(email, subscriber_id, status, selection, raw=None):
    included = or_(email.in_(selection.included_emails),
                   and_(subscriber_id.is_not(None), subscriber_id.in_(selection.included_ids)))
    chosen = included
    if selection.mode == "all":
        all_matches = _status_matches(status, selection.eligibility)
        if selection.search:
            all_matches = and_(all_matches, email.in_(_search_emails(
                _identities() if raw is None else raw, selection.search)))
        chosen = or_(all_matches, included)
    if selection.excluded_ids:
        chosen = and_(chosen, or_(subscriber_id.is_(None), subscriber_id.not_in(selection.excluded_ids)))
    if selection.excluded_emails:
        chosen = and_(chosen, email.not_in(selection.excluded_emails))
    return chosen


def selected_ids(filters: AudienceFilters, selection: RecipientSelection | None, *, raw=None):
    """Campaign previews and deliveries intersect the organizer's selection with active consent."""
    query = eligible_ids(filters).where(_valid_address(MarketingSubscriber.email))
    if selection is None:
        return query
    return query.where(_selection_match(MarketingSubscriber.email, MarketingSubscriber.id, literal("eligible"), selection, raw))


async def directory_contains(db, email):
    if not valid_email(email):
        return False
    raw = _identities()
    return bool(await db.scalar(select(select(raw.c.email).where(raw.c.email == normalize_email(email)).exists())))


def scoped_forms(filters):
    """A single persisted form/CRM record supplies every dimension in the filter."""
    if filters.source in PUBLIC_FORMS:
        origin = PUBLIC_FORMS[filters.source]
    elif filters.source and filters.source.startswith("database:"):
        origin = filters.source.removeprefix("database:")
    else:
        origin = None
    origins = _origins(filter_files=bool(filters.program or filters.outcome))
    if origin is None:
        return union_all(*origins.values()).cte()
    if origin not in origins:
        return None
    return origins[origin].cte()


def _raw_matches(raw, filters, *, subscriber_locale=None):
    query = select(raw.c.email)
    if filters.source:
        if filters.source.startswith("database:"):
            query = query.where(raw.c.origin == filters.source.removeprefix("database:"))
        else:
            query = query.where(raw.c.form_source == filters.source)
    if filters.program:
        query = query.where(raw.c.program == filters.program)
    if filters.outcome:
        outcome = filters.outcome
        if filters.source == "database:crm_prospect" and ":" not in outcome:
            outcome = "prospect:" + outcome
        query = query.where(raw.c.stage == outcome[5:] if outcome.startswith("file:") else
                            or_(raw.c.outcome == outcome, raw.c.entry_outcome == outcome))
    if filters.locale:
        locale = raw.c.locale if subscriber_locale is None else func.coalesce(raw.c.locale, subscriber_locale)
        query = query.where(locale == filters.locale)
    return query


async def observed_options(db):
    rows = _identities(filter_files=True)
    observed = await db.execute(select(rows.c.form_source, rows.c.program, rows.c.outcome, rows.c.entry_outcome)
        .where(rows.c.email.is_not(None), rows.c.email != "").distinct())
    sources, programs, outcomes = set(), set(), set()
    for source, program, outcome, entry in observed:
        if source:
            sources.add(source)
        if program:
            programs.add(program)
        outcomes.update(value for value in (outcome, entry) if value and not value.startswith("prospect:"))
    return sources, programs, outcomes


def _valid_address(email):
    return and_(func.length(email) <= 320, email.regexp_match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$"))


def _contacts(filters, raw, *, address=None, ignore_prior_request=False):
    from app.models.email_marketing import MarketingPermissionRequest

    identity = _ranked(raw if address is None else select(raw).where(raw.c.email == address).subquery())
    subscriber = MarketingSubscriber
    permission = MarketingPermissionRequest
    has_consent = select(MarketingEnrollment.id).where(MarketingEnrollment.subscriber_id == subscriber.id).exists()
    # Rank the small set of held addresses once per directory query, instead
    # of rescanning every client's files for each row on a database-wide selection.
    holds = contact_restrictions()
    ranked_holds = select(holds, func.row_number().over(
        partition_by=holds.c.email, order_by=holds.c.priority).label("priority_rank")).subquery()
    base_reason = case(
        (subscriber.unsubscribed_at.is_not(None), "Unsubscribed"),
        (ranked_holds.c.reason.is_not(None), ranked_holds.c.reason),
        (~_valid_address(identity.c.email), "Invalid email address"),
    )
    contacts = select(identity.c.email, identity.c.name, identity.c.origin, subscriber.id,
        func.coalesce(subscriber.locale, identity.c.locale).label("locale"), has_consent.label("consented"),
        base_reason.label("hold"), permission.status.label("permission_status"), permission.task_event_id,
    ).outerjoin(subscriber, subscriber.email == identity.c.email).outerjoin(permission, permission.email == identity.c.email
    ).outerjoin(ranked_holds, and_(ranked_holds.c.email == identity.c.email, ranked_holds.c.priority_rank == 1))
    if filters.model_dump(exclude_none=True):
        contacts = contacts.where(or_(subscriber.id.in_(matching_subscribers(filters, directory_rows=raw)),
                                      and_(~has_consent, identity.c.email.in_(_raw_matches(raw, filters)))))
    c = contacts.subquery().c
    prior_reason = case(
        (c.permission_status.in_(("queued", "sending")), "Permission request already queued"),
        (c.permission_status.is_not(None) & (c.permission_status != "task_only"),
         literal("Previous permission request: ") + c.permission_status),
    )
    status = case((c.hold.is_not(None), "blocked"), (c.consented, "eligible"),
                  (prior_reason.is_not(None) & literal(not ignore_prior_request), "blocked"), else_="requestable")
    reason = case((c.hold.is_not(None), c.hold), (c.consented, literal(None, String())),
                  (prior_reason.is_not(None) & literal(not ignore_prior_request), prior_reason),
                  else_="No recorded marketing email permission")
    return select(c.email, c.name, c.origin, c.id, c.locale, c.permission_status, c.task_event_id,
                  status.label("status"), reason.label("reason"),
                  and_(c.hold.is_(None), ~c.consented).label("can_record_consent")).subquery()


def _directory_rows(filters, selection, raw):
    contacts = _contacts(filters, raw)
    chosen = _selection_match(contacts.c.email, contacts.c.id, contacts.c.status, selection, raw)
    return select(contacts, chosen.label("selected")).subquery()


def selected_contacts(filters: AudienceFilters, selection: RecipientSelection):
    """Frozen audience for organization and permission actions. Revalidate before any handoff."""
    raw = _identities(filter_files=bool(filters.program or filters.outcome))
    rows = _directory_rows(filters, selection, raw)
    return select(rows).where(rows.c.selected.is_(True))


async def requestable_contact(db, email, *, ignore_prior_request=False):
    address = normalize_email(email)
    if len(address) > 320 or not valid_email(address):
        return False
    rows = _contacts(AudienceFilters(), _identities(), address=address, ignore_prior_request=ignore_prior_request)
    return bool(await db.scalar(select(rows.c.status == "requestable").where(rows.c.email == address)))


def _counts(rows):
    counts = [func.count(case((rows.c.status == state, 1))) for state in ("eligible", "requestable", "blocked")]
    return select(func.count(), *counts).select_from(rows)


async def directory(db, request: DirectoryRequest):
    filters = request.filters
    raw = _identities(filter_files=bool(filters.program or filters.outcome))
    rows = _directory_rows(filters, request.selection, raw)
    hits = select(rows).where(_status_matches(rows.c.status, request.eligibility))
    if request.search:
        hits = hits.where(rows.c.email.in_(_search_emails(raw, request.search)))
    if request.selected_only:
        hits = hits.where(rows.c.selected.is_(True))
    page = hits.subquery()
    count, eligible, requestable, blocked = (await db.execute(_counts(page))).one()
    selected_count, selected_eligible, selected_requestable, selected_blocked = (await db.execute(
        _counts(rows).where(rows.c.selected.is_(True)))).one()
    records = (await db.execute(select(page).order_by(page.c.email).offset(
        (request.page - 1) * request.page_size).limit(request.page_size))).mappings()
    return {"page": request.page, "page_size": request.page_size, "total_count": count, "eligible_count": eligible,
            "requestable_count": requestable, "blocked_count": blocked, "selected_count": selected_count,
            "selected_eligible_count": selected_eligible, "selected_requestable_count": selected_requestable,
            "selected_blocked_count": selected_blocked, "rows": [{
                "email": row.email, "name": row.name, "source": ORIGINS[row.origin], "id": row.id,
                "eligible": row.status == "eligible", "reason": row.reason, "selected": bool(row.selected),
                "status": row.status, "permission_status": row.permission_status, "task_event_id": row.task_event_id,
                "can_record_consent": bool(row.can_record_consent),
            } for row in records]}


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
