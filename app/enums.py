"""Module 7 — Strict enum values for dropdowns and stage tracking.

This file is the single source of truth for every dropdown in the system.
TypeScript types are codegen'd from these via scripts/gen_ts_enums.py.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    SUPER_ADMIN = "super_admin"
    BROKER = "broker"
    LOAN_EXEC = "loan_exec"
    CLIENT = "client"


class PropertyType(StrEnum):
    SFR = "single_family"
    UNITS_2_4 = "2_4_units"
    UNITS_5_8 = "5_8_units"
    MIXED_USE = "mixed_use"
    COMMERCIAL = "commercial"


class LoanPurpose(StrEnum):
    PURCHASE = "purchase"
    RATE_TERM_REFI = "rate_term_refi"
    CASH_OUT_REFI = "cash_out_refi"


class EntityType(StrEnum):
    INDIVIDUAL = "individual"
    LLC = "llc"
    CORPORATION = "corporation"
    TRUST = "trust"


class ExperienceTier(StrEnum):
    """Fix & Flip experience in last 36 months."""
    NONE = "0_flips"
    LIGHT = "1_2_flips"
    MID = "3_5_flips"
    HEAVY = "5_plus_flips"


class PrepayPenalty(StrEnum):
    """DSCR loans only. 'NONE' increases base rate."""
    P_5_4_3_2_1 = "5_4_3_2_1"
    P_3_2_1 = "3_2_1"
    P_2_YEAR = "2_year"
    P_1_YEAR = "1_year"
    NONE = "none"


class AmortizationStyle(StrEnum):
    """How principal is paid down over the term.

    fully_amortizing — standard P&I payment that retires the balance by term-end.
    interest_only    — borrower pays interest only each month, full principal
                       balloons at maturity. Default for F&F / Bridge / Ground Up.
    """
    FULLY_AMORTIZING = "fully_amortizing"
    INTEREST_ONLY = "interest_only"


class ExitStrategy(StrEnum):
    """How the borrower intends to retire the loan at term-end. Used on
    bridge / fix-and-flip / ground-up files where the lender underwrites
    the exit, not just the entry."""
    SALE = "sale"
    REFINANCE = "refinance"
    HOLD = "hold"


class LoanType(StrEnum):
    DSCR = "dscr"
    FIX_AND_FLIP = "fix_and_flip"
    GROUND_UP = "ground_up"
    BRIDGE = "bridge"
    PORTFOLIO = "portfolio"
    CASH_OUT_REFI = "cash_out_refi"


class LoanSide(StrEnum):
    """Which side of the real-estate transaction the borrower is on.
    Drives doc-checklist filtering — buyer-side and seller-side
    items are tagged on `DocChecklistItem.side` and the cron only
    materializes items whose side matches (or 'both'). Default is
    `buyer` — current pipeline is dominated by purchase loans."""
    BUYER = "buyer"
    SELLER = "seller"


class LoanStage(StrEnum):
    """6-stage canonical pipeline (chat2.md final state)."""
    PREQUALIFIED = "prequalified"
    COLLECTING_DOCS = "collecting_docs"
    LENDER_CONNECTED = "lender_connected"
    PROCESSING = "processing"
    CLOSING = "closing"
    FUNDED = "funded"


# Ordered list — frontend stage steppers iterate this
LOAN_STAGE_ORDER: list[LoanStage] = [
    LoanStage.PREQUALIFIED,
    LoanStage.COLLECTING_DOCS,
    LoanStage.LENDER_CONNECTED,
    LoanStage.PROCESSING,
    LoanStage.CLOSING,
    LoanStage.FUNDED,
]


def stage_index(stage: LoanStage) -> int:
    return LOAN_STAGE_ORDER.index(stage)


class DocStatus(StrEnum):
    PENDING = "pending"
    REQUESTED = "requested"
    RECEIVED = "received"
    FLAGGED = "flagged"
    VERIFIED = "verified"
    # Operator/agent removed this doc from the AI's collection plan
    # for THIS specific loan. Cron skips over it; vault hides by
    # default; can be flipped back to REQUESTED via PATCH /documents/{id}.
    SKIPPED = "skipped"


class AITaskPriority(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AITaskStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DISMISSED = "dismissed"
    EXECUTED = "executed"


class AITaskSource(StrEnum):
    UNDERWRITING = "underwriting"
    MESSAGES = "messages"
    RISK = "risk"
    CALENDAR = "calendar"
    DOCUMENTS = "documents"
    PIPELINE = "pipeline"
    RATES = "rates"
    BROKER_SUGGESTION = "broker_suggestion"


class DealChatRole(StrEnum):
    """Author role on a loan_chat_messages row. Distinct from MessageFrom
    because broker_internal turns (Q&A) never reach the client."""
    AI = "ai"
    SUPER_ADMIN = "super_admin"
    BROKER_INTERNAL = "broker_internal"
    CLIENT = "client"


class DealChatMode(StrEnum):
    """The mode field on POST /loans/{id}/chat — drives routing in the
    chat handler (persist as message vs instruction vs broker suggestion)."""
    CHAT = "chat"
    INSTRUCT = "instruct"
    BROKER_QUESTION = "broker_question"
    BROKER_SUGGESTION = "broker_suggestion"


class FeedbackOutputType(StrEnum):
    """Polymorphic target for ai_feedback rows. Only AI_TASK is exercised
    in this pass; the others are reserved for future surfaces."""
    AI_TASK = "ai_task"
    CHAT_REPLY = "chat_reply"
    EMAIL_DRAFT = "email_draft"
    SUMMARY = "summary"


class FeedbackRating(StrEnum):
    UP = "up"
    DOWN = "down"


class MessageFrom(StrEnum):
    CLIENT = "client"
    BROKER = "broker"
    AI = "ai"
    LENDER = "lender"


class CalendarEventKind(StrEnum):
    CALL = "call"
    DOC = "doc"
    AI = "ai"
    INSPECT = "inspect"
    MILESTONE = "milestone"
    LOCK = "lock"
    PAY = "pay"
    CLOSING = "closing"


class CalendarEventStatus(StrEnum):
    """Lifecycle of a calendar entry. We never delete — operators
    cancel or mark done so the audit trail (and any AI feedback) lives."""
    PENDING = "pending"
    DONE = "done"
    CANCELLED = "cancelled"


class CalendarEventSource(StrEnum):
    """Where the event came from. Drives audience scoping —
    borrowers see manual + auto, never raw ai (must be approved
    via AITask flow first)."""
    MANUAL = "manual"   # operator typed it in the UI
    AUTO = "auto"       # lifecycle emitter (loan stage, doc due, etc.)
    AI = "ai"           # LLM-suggested via summarizer next_actions


class CalendarExternalRefKind(StrEnum):
    """Idempotency namespace for auto/ai events. Paired with
    external_ref_id (string) to give us ON CONFLICT upserts via the
    partial unique index ix_calendar_events_external."""
    LOAN_STAGE = "loan_stage"
    LOAN_CLOSE = "loan_close"
    CREDIT_PULL = "credit_pull"
    CREDIT_EXPIRY = "credit_expiry"
    DOCUMENT_DUE = "document_due"
    PREQUAL_CLOSE = "prequal_close"
    AI_ACTION = "ai_action"
    STALLED_LOAN = "stalled_loan"


class BrokerTier(StrEnum):
    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"
    PLATINUM = "platinum"


class Urgency(StrEnum):
    OVERDUE = "overdue"
    TODAY = "today"
    SOON = "soon"        # within 3 days
    WEEK = "week"        # within 7 days
    ON_TRACK = "on_track"


class CreditPullStatus(StrEnum):
    NONE = "none"
    PENDING = "pending"
    COMPLETED = "completed"
    EXPIRED = "expired"
    REVOKED = "revoked"


class DealHealth(StrEnum):
    """Living Loan File health indicator. Updated by the AI summarizer."""
    ON_TRACK = "on_track"      # green
    AT_RISK = "at_risk"        # amber — slowdowns, missing pieces, soft blockers
    STUCK = "stuck"            # red — hard blocker, broker action required


class ClientStage(StrEnum):
    """Where the client is in the agent's pipeline (alembic 0024).

    Lead-stage funnel (agent-owned):
      LEAD              — added, no outreach yet
      CONTACTED         — agent reached out (call, email, text)
      VERIFIED          — ID + soft credit verified

    Funding-stage funnel (firm/funding-team owned):
      READY_FOR_LENDING — handed off to funding team
      PROCESSING        — active loan in flight
      FUNDED            — closed loan(s) on file
      LOST              — walked away or no response (terminal)
    """
    LEAD = "lead"
    CONTACTED = "contacted"
    VERIFIED = "verified"
    READY_FOR_LENDING = "ready_for_lending"
    PROCESSING = "processing"
    FUNDED = "funded"
    LOST = "lost"


class ParticipantRole(StrEnum):
    """A loan thread participant. Drives the Fintech Orchestrator privacy
    rules — Lenders are masked from Brokers/Clients on inbound, and Super
    Admins are auto-BCC'd on outbound mail."""
    LENDER = "lender"
    BROKER = "broker"
    CLIENT = "client"
    SUPER_ADMIN = "super_admin"


class EmailDraftStatus(StrEnum):
    """Auto-drafted outbound emails awaiting broker approval."""
    PENDING = "pending"
    APPROVED = "approved"
    SENT = "sent"
    DISMISSED = "dismissed"


# -------------------------------------------------------------------------
# AI Deal Secretary enums (alembic 0038).
#
# These power the "Assign to AI" workflow: who owns each task, which
# outreach channels are allowed, how strict the approval gate is, and
# what state the per-task workflow is in. The full mental model is in
# the plan file — four orthogonal axes:
#
#   Owner          who is responsible        TaskOwnerType
#   Outreach       can AI contact borrower   OutreachMode (file-level)
#   Channel        where AI may contact      OutreachChannel (per task)
#   Approval mode  do messages go live       ApprovalMode (per task)
#
# Funding-locked items are pinned to their default owner — agents cannot
# move them; only underwriters can.
# -------------------------------------------------------------------------


class RequirementCategory(StrEnum):
    """Single closed taxonomy used by Settings, the Deal Secretary picker,
    pipeline filters, and AI Inbox source chips. No free-text categories
    anywhere in the system — adding a category is a code change.

    Old `category` values on AICollectionRequirement (fact / document /
    appointment / agreement / task) are mapped onto this taxonomy by the
    0038 data migration."""
    BORROWER_INFO = "borrower_info"             # name, contact, entity type, identification
    PROPERTY_DATA = "property_data"             # address, type, beds/baths/sqft, year, units
    FINANCIALS = "financials"                   # bank statements, tax returns, PFS, rent roll
    CREDIT = "credit"                           # soft pull, hard pull, FICO authorization
    AGREEMENTS = "agreements"                   # buyer agency, listing, purchase contract, op agreement
    INSURANCE = "insurance"                     # hazard, flood, liability quotes / binders
    TITLE_AND_ESCROW = "title_and_escrow"       # title commitment, prelim, escrow instructions
    APPRAISAL_AND_INSPECTION = "appraisal_and_inspection"  # appraisal order, inspection, walkthrough
    SCHEDULING = "scheduling"                   # showings, picture day, walkthrough, consultation
    COMPLIANCE = "compliance"                   # disclosures, RESPA, TRID, state-specific forms
    COMMUNICATION = "communication"             # outreach drafts, reminders, follow-ups
    AI_INTERNAL = "ai_internal"                 # AI-only working state (never borrower-visible)


class TaskOwnerType(StrEnum):
    """Who is responsible for moving a requirement to completion.

    `funding_locked` is the special case — UW set it; agents cannot waive
    or reassign. Enforced by the resolver + by the `can_agent_override`
    flag on the catalog row."""
    HUMAN = "human"
    AI = "ai"
    SHARED = "shared"
    FUNDING_LOCKED = "funding_locked"


class OutreachChannel(StrEnum):
    """Per-assignment list of channels the AI is allowed to use on this
    specific task. Subset of the file-level OutreachMode capabilities."""
    PORTAL = "portal"
    EMAIL = "email"
    SMS = "sms"
    VOICE = "voice"


class OutreachMode(StrEnum):
    """File-level kill switch / mode selector for AI outreach.

    Stored on ClientAIPlan.ai_secretary_settings.outreach_mode. The
    cadence engine consults this BEFORE firing any borrower-visible
    action — see cadence_engine._evaluate_rule().

    - off              : nothing sends; AI may still track + draft internally
    - draft_first      : drafts land in AI Inbox; agent reviews + sends manually
    - portal_auto      : portal messages auto-send; email/SMS still draft-first
    - portal_email     : portal + email auto-send; SMS still draft-first
    - portal_email_sms : everything auto-sends (consent + quiet hours still apply)
    """
    OFF = "off"
    DRAFT_FIRST = "draft_first"
    PORTAL_AUTO = "portal_auto"
    PORTAL_EMAIL = "portal_email"
    PORTAL_EMAIL_SMS = "portal_email_sms"


class ApprovalMode(StrEnum):
    """Per-assignment override of the file-level OutreachMode. Sensitive
    tasks (e.g. anything touching closing dates) can opt into stricter
    review even when the file is in portal_auto."""
    DRAFT_FIRST = "draft_first"
    AUTO_SEND_PORTAL = "auto_send_portal"
    REQUIRE_PER_MESSAGE = "require_per_message"


class OutreachEventStatus(StrEnum):
    """Lifecycle states for one row in ai_outreach_events.

    `scheduled` is the queued-but-not-yet-fired state. `drafted` lands in
    AI Inbox. `blocked_by_*` are the legibly-failed states that explain
    why nothing went out — important for debugging cadence gating."""
    SCHEDULED = "scheduled"
    DRAFTED = "drafted"
    SENT = "sent"
    DELIVERED = "delivered"
    RESPONDED = "responded"
    FAILED = "failed"
    BLOCKED_BY_CONSENT = "blocked_by_consent"
    BLOCKED_BY_QUIET_HOURS = "blocked_by_quiet_hours"
    BLOCKED_BY_WORKING_HOURS = "blocked_by_working_hours"
    BLOCKED_BY_POLICY = "blocked_by_policy"
    CANCELLED = "cancelled"


class LinkKind(StrEnum):
    """Icon hint for the optional link_url on a catalog row or
    per-deal assignment. DocuSign is the highest-value case (agents
    paste their pre-built envelope URL); others are generic."""
    DOCUSIGN = "docusign"
    ESIGN = "esign"
    EXTERNAL_FORM = "external_form"
    REFERENCE = "reference"


class CompletionMode(StrEnum):
    """Defines what 'done' means for a task. Critical for the AI to know
    when to stop follow-up vs hand off to a human.

    - ai_can_complete       : when the scanner/chat signal fires, AI flips
                              status='verified' and stops follow-up.
    - requires_human_verify : AI flips status='provided_unverified' and
                              creates an AITask for the underwriter. AI
                              stops follow-up; resumes only on rejection.
    - borrower_self_attest  : borrower confirms in chat. Trust score from
                              fact_priority.py is 'borrower_confirmed'
                              (lower than 'verified_document')."""
    AI_CAN_COMPLETE = "ai_can_complete"
    REQUIRES_HUMAN_VERIFY = "requires_human_verify"
    BORROWER_SELF_ATTEST = "borrower_self_attest"
