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
