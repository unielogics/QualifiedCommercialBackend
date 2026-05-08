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
