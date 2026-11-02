"""Vocabulary for the Capital Partners program.

Kept inside the package (not app.enums) so the isolation contract holds; the
frontends mirror these as string unions by hand, the way QCRep and QCDealerOS
do for the dealer-OS vocabulary.
"""

from __future__ import annotations

from enum import StrEnum


class InvestingAs(StrEnum):
    INDIVIDUAL = "individual"
    FAMILY_OFFICE = "family_office"
    ENTITY = "entity"


class MemberStatus(StrEnum):
    #: Signed up or invited; nothing submitted yet.
    REQUESTED = "requested"
    #: Questionnaire started, not submitted.
    QUESTIONNAIRE = "questionnaire"
    #: Questionnaire submitted; the desk is reviewing it.
    UNDER_REVIEW = "under_review"
    #: Approved; the waiting period has not ended.
    WAITING = "waiting"
    #: Approved and past the waiting period: offerings are visible.
    APPROVED = "approved"
    SUSPENDED = "suspended"


class EntityKind(StrEnum):
    INDIVIDUAL = "individual"
    TRUST = "trust"
    LLC = "llc"
    PARTNERSHIP = "partnership"
    FAMILY_OFFICE = "family_office"
    OTHER = "other"


class AccessRequestStatus(StrEnum):
    NEW = "new"
    CONTACTED = "contacted"
    INVITED = "invited"
    DECLINED = "declined"


class QuestionnaireOutcome(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    NEEDS_CHANGES = "needs_changes"
    REJECTED = "rejected"


class ListingSourceKind(StrEnum):
    LOAN = "loan"
    PROFILE = "profile"
    MANUAL = "manual"


class Vertical(StrEnum):
    REAL_ESTATE = "real_estate"
    AUTO = "auto"
    BUSINESS = "business"


VERTICAL_LABELS: dict[str, str] = {
    Vertical.REAL_ESTATE: "Real estate",
    Vertical.AUTO: "Auto",
    Vertical.BUSINESS: "Business",
}

#: The listing-reference prefix per vertical: QC-RE-0001, QC-AU-0001, QC-BZ-0001.
VERTICAL_REF_PREFIX: dict[str, str] = {
    Vertical.REAL_ESTATE: "RE",
    Vertical.AUTO: "AU",
    Vertical.BUSINESS: "BZ",
}


class LienPosition(StrEnum):
    FIRST = "first"
    SECOND = "second"


class LtvKind(StrEnum):
    LTV = "ltv"
    LTC = "ltc"
    CLTV = "cltv"


class PaymentFrequency(StrEnum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    AT_MATURITY = "at_maturity"


class ListingStatus(StrEnum):
    DRAFT = "draft"
    OPEN = "open"
    CLOSING = "closing"
    FULLY_SUBSCRIBED = "fully_subscribed"
    FUNDED = "funded"
    REPAID = "repaid"
    WORKOUT = "workout"
    WITHDRAWN = "withdrawn"


#: Statuses a member may see in the marketplace (open to subscribe).
MARKET_STATUSES: frozenset[str] = frozenset({ListingStatus.OPEN, ListingStatus.CLOSING, ListingStatus.FULLY_SUBSCRIBED})
#: Statuses that appear under "Sold and repaid".
SOLD_STATUSES: frozenset[str] = frozenset(
    {ListingStatus.FULLY_SUBSCRIBED, ListingStatus.FUNDED, ListingStatus.REPAID, ListingStatus.WORKOUT}
)
#: Statuses a member may subscribe to.
SUBSCRIBABLE_STATUSES: frozenset[str] = frozenset({ListingStatus.OPEN, ListingStatus.CLOSING})


class ListingOutcome(StrEnum):
    PERFORMING = "performing"
    REPAID_IN_FULL = "repaid_in_full"
    REPAID_AFTER_EXTENSION = "repaid_after_extension"
    IN_WORKOUT = "in_workout"


class DocumentKind(StrEnum):
    PPM = "ppm"
    SUBSCRIPTION_AGREEMENT = "subscription_agreement"
    NOTE = "note"
    VALUATION = "valuation"
    TITLE = "title"
    SERVICING_SUMMARY = "servicing_summary"
    OTHER = "other"


DOCUMENT_KIND_LABELS: dict[str, str] = {
    DocumentKind.PPM: "Private placement memorandum",
    DocumentKind.SUBSCRIPTION_AGREEMENT: "Participation and subscription agreement",
    DocumentKind.NOTE: "Promissory note and security instrument",
    DocumentKind.VALUATION: "Valuation report",
    DocumentKind.TITLE: "Title and lien search",
    DocumentKind.SERVICING_SUMMARY: "Servicing agreement summary",
    DocumentKind.OTHER: "Other document",
}


class DocumentStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    SUPERSEDED = "superseded"


class SubscriptionStatus(StrEnum):
    REQUESTED = "requested"
    DOCUMENTS_SENT = "documents_sent"
    #: Binding. This is the "sale" for Form D purposes.
    SIGNED = "signed"
    FUNDED = "funded"
    CANCELLED = "cancelled"
    DECLINED = "declined"


#: Subscriptions that hold an allocation against the listing's offered amount.
COMMITTED_STATUSES: frozenset[str] = frozenset(
    {SubscriptionStatus.REQUESTED, SubscriptionStatus.DOCUMENTS_SENT, SubscriptionStatus.SIGNED, SubscriptionStatus.FUNDED}
)
#: Subscriptions that count as sales (binding or funded).
SOLD_SUBSCRIPTION_STATUSES: frozenset[str] = frozenset({SubscriptionStatus.SIGNED, SubscriptionStatus.FUNDED})


class ServicerStatus(StrEnum):
    CURRENT = "current"
    LATE = "late"
    PAID_IN_FULL = "paid_in_full"
    IN_WORKOUT = "in_workout"


class TransferStatus(StrEnum):
    LISTED = "listed"
    REQUESTED = "requested"
    PENDING_CONSENT = "pending_consent"
    APPROVED = "approved"
    COMPLETED = "completed"
    WITHDRAWN = "withdrawn"
    DECLINED = "declined"


class FilingKind(StrEnum):
    FORM_D = "form_d"
    FORM_D_AMENDMENT = "form_d_amendment"
    STATE_NOTICE = "state_notice"
    BAD_ACTOR_CHECK = "bad_actor_check"


class FilingStatus(StrEnum):
    DUE = "due"
    FILED = "filed"
    NOT_REQUIRED = "not_required"
    CLEARED = "cleared"
    PENDING = "pending"


#: Calendar days the issuer has to file Form D after the first sale (Rule 503).
FORM_D_DAYS = 15
#: Calendar days most states allow for a Rule 506 notice after the first sale
#: to a resident. States vary; the tracker records the deadline per row and
#: counsel confirms it before filing.
STATE_NOTICE_DAYS = 15
