"""Commercial foreclosure-rescue program rules and lifecycle helpers."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

FORECLOSURE_RESCUE_VARIANT = "commercial_foreclosure_bailout_v1"

NOTE_RATE_PCT = Decimal("12.99")
TERM_MONTHS = 24
AMORTIZATION_MONTHS = 480
MAX_LTV_PCT = Decimal("75")
PROCEEDS_POLICY = "payoff_only"


def program_terms() -> dict[str, str | int | float]:
    """One serializable source for disclosures, calculations, and documents."""

    return {
        "note_rate_pct": float(NOTE_RATE_PCT),
        "term_months": TERM_MONTHS,
        "amortization_months": AMORTIZATION_MONTHS,
        "max_ltv_pct": float(MAX_LTV_PCT),
        "proceeds_policy": PROCEEDS_POLICY,
    }


def maximum_rescue_amount(estimated_market_value: Decimal | float | str) -> Decimal:
    """Preliminary collateral ceiling before file-specific underwriting adjustments."""

    value = Decimal(str(estimated_market_value))
    return (value * MAX_LTV_PCT / Decimal("100")).quantize(Decimal("0.01"))


def minimum_value_for_payoff(payoff_amount: Decimal | float | str) -> Decimal:
    """Minimum indicated value needed for a payoff at the published LTV ceiling."""

    payoff = Decimal(str(payoff_amount))
    return (payoff / (MAX_LTV_PCT / Decimal("100"))).quantize(Decimal("0.01"))


def validate_term_sheet_note_rate(note_rate_pct: Decimal | float | str) -> None:
    """Reject rescue pricing that conflicts with the published fixed rate."""

    if Decimal(str(note_rate_pct)) != NOTE_RATE_PCT:
        raise ValueError(f"Foreclosure-rescue note rate must be exactly {NOTE_RATE_PCT}%")

RESCUE_STATUSES = (
    "new_rescue",
    "initial_docs_pending",
    "ready_for_initial_review",
    "in_underwriting",
    "term_sheet_issued",
    "closing_docs_pending",
    "clear_to_close",
    "funded",
    "declined",
    "withdrawn_expired",
)

TERMINAL_RESCUE_STATUSES = {"funded", "declined", "withdrawn_expired"}

STATUS_LABELS = {
    "new_rescue": "New Rescue",
    "initial_docs_pending": "Initial Docs Pending",
    "ready_for_initial_review": "Ready for Initial Review",
    "in_underwriting": "In Underwriting",
    "term_sheet_issued": "Term Sheet Issued",
    "closing_docs_pending": "Closing Docs Pending",
    "clear_to_close": "Clear to Close",
    "funded": "Funded",
    "declined": "Declined",
    "withdrawn_expired": "Withdrawn / Deadline Passed",
}

UNIFIED_STATUS_MAP = {
    "new_rescue": "submitted",
    "initial_docs_pending": "collecting_docs",
    "ready_for_initial_review": "in_underwriting",
    "in_underwriting": "in_underwriting",
    "term_sheet_issued": "term_sheet_provided",
    "closing_docs_pending": "approved",
    "clear_to_close": "approved",
    "funded": "closed_won",
    "declined": "denied",
    "withdrawn_expired": "closed_lost",
}

INITIAL_REVIEW_DOCUMENTS = (
    {
        "name": "Payoff demand or default notice",
        "description": "Current lender demand showing principal, delinquent interest, legal fees, and per-diem charges.",
        "required": True,
    },
    {
        "name": "Current rent roll or occupancy support",
        "description": "Current tenant, rent, lease-term, occupancy, and arrears detail; use an occupancy statement for a vacant or owner-used asset.",
        "required": True,
    },
    {
        "name": "Trailing-12 operating statement",
        "description": "Actual property revenue and operating expenses for the latest twelve consecutive months, or the closest available alternative.",
        "required": True,
    },
    {
        "name": "Foreclosure, court, or bankruptcy documents",
        "description": "Applicable complaint, docket summary, sale notice, lis pendens, or Chapter 11/Subchapter V petition.",
        "required": True,
    },
    {
        "name": "Property valuation",
        "description": "Recent appraisal, broker opinion of value, tax assessment, or other value support, if available.",
        "required": False,
    },
)

CLOSING_DOCUMENTS = (
    "Articles of organization or formation",
    "Operating agreement and signing authority",
    "Certificate of good standing",
    "Property tax, municipal charge, and utility status",
    "Government-issued IDs for 20%+ owners and guarantors",
    "Preliminary title commitment",
    "Additional title, lien, insurance, appraisal, or lender conditions",
)


def urgency_for(sale_date: date | None, *, today: date | None = None) -> dict[str, str | int | None]:
    """Return the deterministic deadline band used by every UI."""

    if sale_date is None:
        return {"key": "standard", "label": "Standard", "days_remaining": None, "warning": "Sale date not provided"}
    days = (sale_date - (today or date.today())).days
    if days <= 7:
        label = "Critical"
        key = "critical"
    elif days <= 14:
        label = "Urgent"
        key = "urgent"
    elif days <= 30:
        label = "Time Sensitive"
        key = "time_sensitive"
    else:
        label = "Standard"
        key = "standard"
    warning = "Sale date has passed — confirm the current deadline" if days < 0 else None
    return {"key": key, "label": label, "days_remaining": days, "warning": warning}


def monthly_principal_and_interest(principal: Decimal) -> Decimal:
    """Payment on 480-month amortization at the fixed 12.99% note rate."""

    monthly_rate = NOTE_RATE_PCT / Decimal("100") / Decimal("12")
    factor = (Decimal("1") + monthly_rate) ** AMORTIZATION_MONTHS
    payment = principal * monthly_rate * factor / (factor - Decimal("1"))
    return payment.quantize(Decimal("0.01"))


def balloon_balance(principal: Decimal) -> Decimal:
    """Remaining scheduled principal after the 24th payment."""

    principal = Decimal(principal)
    monthly_rate = NOTE_RATE_PCT / Decimal("100") / Decimal("12")
    amortization_factor = (Decimal("1") + monthly_rate) ** AMORTIZATION_MONTHS
    # Keep full precision for the balance fixture; round only the displayed
    # monthly payment and final balloon amount.
    payment = principal * monthly_rate * amortization_factor / (amortization_factor - Decimal("1"))
    factor = (Decimal("1") + monthly_rate) ** TERM_MONTHS
    balance = principal * factor - payment * ((factor - Decimal("1")) / monthly_rate)
    return balance.quantize(Decimal("0.01"))
