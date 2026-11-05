"""The request and answer shapes of the debt schedule line review.

The review lists every line a file's debt schedule could carry — the lines the
AI read off an uploaded schedule and the rows already on the file — so the desk
can take off what is not a loan and answer the three questions a lender asks
of every line: the interest rate, the amount originally approved and what is
still owed.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

RemoveReason = Literal["not_a_loan", "duplicate", "paid_off", "other_business", "other"]
Cadence = Literal["daily", "weekly", "biweekly", "monthly"]
LineState = Literal["read", "on_schedule", "removed"]
PaymentStatus = Literal["current", "delinquent"]

#: The figures on the file are in dollars; nothing a debt schedule carries is a billion.
_MONEY_CEILING = 1_000_000_000


class DebtReviewHint(BaseModel):
    """Why a line may not be a loan. Advice only: nothing is ever removed for it."""

    kind: str
    label: str
    reason: str


class DebtReviewSourceRead(BaseModel):
    """One uploaded schedule the lines were read from."""

    file_id: str
    file_name: str
    uploaded_by: str | None = None
    uploaded_at: str | None = None
    entity: str | None = None
    schedule_date: str | None = None
    #: What the document says about itself ("Preliminary; not lender-certified").
    status_note: str | None = None
    scope_note: str | None = None
    source_basis: str | None = None
    line_count: int = 0


class DebtReviewLineRead(BaseModel):
    #: Stable within one reading: the lender's slug (with -2, -3 for repeats), or row:<id>.
    key: str
    row_id: str | None = None
    state: LineState
    origin: str | None = None
    origin_label: str
    file_id: str | None = None
    file_name: str | None = None
    lender: str
    #: A slug of the closed debt-type list, text kept as typed, or "" for not stated.
    debt_type: str = ""
    advance: bool = False
    original_amount: float | None = None
    balance: float | None = None
    #: A percent a year. A factor rate is never stored here.
    rate: float | None = None
    factor_rate: float | None = None
    payment_amount: float | None = None
    payment_frequency: Cadence | None = None
    monthly_payment: float | None = None
    payment_status: PaymentStatus | None = None
    count_in_dscr: bool = True
    #: What the file said about the balance and the payment, to sit under their boxes.
    balance_note: str | None = None
    payment_note: str | None = None
    notes_from_file: list[str] = Field(default_factory=list)
    hint: DebtReviewHint | None = None
    #: Which answers the line still lacks: debt_type, rate (factor_rate for an
    #: advance), original_amount, balance, payment, payment_status.
    missing: list[str] = Field(default_factory=list)
    removed_reason: RemoveReason | None = None
    removed_by: str | None = None
    removed_at: str | None = None


class DebtScheduleReviewRead(BaseModel):
    profile_id: str
    sources: list[DebtReviewSourceRead] = Field(default_factory=list)
    lines: list[DebtReviewLineRead] = Field(default_factory=list)
    removed: list[DebtReviewLineRead] = Field(default_factory=list)
    #: read / on_schedule / removed lines, and the interest, original amount and
    #: balance answers still missing across the lines that are staying.
    counts: dict[str, int] = Field(default_factory=dict)


class DebtReviewLineWrite(BaseModel):
    """One line's decision. Every field the person can change rides along; the
    server treats the payload as the answer, so a line the reading has since
    moved on from is still saved as sent."""

    key: str = Field(min_length=1, max_length=200)
    row_id: UUID | None = None
    keep: bool = True
    remove_reason: RemoveReason | None = None
    remove_note: str | None = Field(default=None, max_length=300)
    file_id: UUID | None = None
    lender: str = Field(min_length=1, max_length=180)
    debt_type: str | None = Field(default=None, max_length=40)
    original_amount: float | None = Field(default=None, ge=0, le=_MONEY_CEILING)
    balance: float | None = Field(default=None, ge=0, le=_MONEY_CEILING)
    rate: float | None = Field(default=None, ge=0, le=999.999)
    factor_rate: float | None = Field(default=None, ge=1, le=5)
    payment_amount: float | None = Field(default=None, ge=0, le=_MONEY_CEILING)
    payment_frequency: Cadence | None = None
    monthly_payment: float | None = Field(default=None, ge=0, le=_MONEY_CEILING)
    payment_status: PaymentStatus | None = None

    @model_validator(mode="after")
    def _a_payment_says_how_often(self) -> DebtReviewLineWrite:
        # The cockpit reads a per-payment amount with no cadence as a monthly
        # one, which would understate a daily advance twenty-fold.
        if (
            self.keep
            and self.payment_amount is not None
            and self.payment_frequency is None
            and self.monthly_payment is None
        ):
            raise ValueError(f"Say how often {self.lender} is paid, or clear the payment amount.")
        return self


class DebtScheduleReviewWrite(BaseModel):
    lines: list[DebtReviewLineWrite] = Field(max_length=80)


class DebtScheduleReviewSaved(BaseModel):
    kept: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    restored: int = 0


class DebtScheduleReviewResult(BaseModel):
    review: DebtScheduleReviewRead
    saved: DebtScheduleReviewSaved
