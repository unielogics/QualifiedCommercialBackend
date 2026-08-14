"""Ingestion normalization — Stream 2.

Pure classification of raw statement lines into cash-event categories/flags,
plus the period rebuild that keeps dos_financial_periods consistent with the
event ledger. classify_event is a pure function (unit-testable, no IO);
rebuild_periods is the single reconcile choke point every ingestion path
(uploads now, Plaid/QBO later) calls after touching events.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DealerCashEvent, DealerFinancialPeriod

# Ordered keyword rules — first match wins. Case-insensitive substring match.
_KEYWORD_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("floorplan", ("nextgear", "floorplan", "afc", "westlake")),
    ("debt_service", ("mca", "daily debit", "advance", "loan pmt")),
    ("payroll", ("payroll", "adp", "gusto")),
    ("tax", ("irs", "tax", "treasury", "dept of revenue")),
    ("insurance", ("insurance",)),
    ("rent", ("rent", "cam", "landlord")),
    ("transfer", ("transfer", "xfer", "to savings", "zelle to own")),
    ("inventory", ("auction", "manheim", "adesa", "wholesale", "floor purchase")),
    ("owner_personal", ("owner", "personal", "distribution", "draw")),
    ("one_time", ("settlement", "legal", "one-time")),
]

_FIXED_CATEGORIES = {"floorplan", "debt_service", "payroll", "tax", "insurance", "rent"}
_ADDBACK_CATEGORIES = {"owner_personal", "one_time"}


def classify_event(description: str, amount: float) -> tuple[str, dict]:
    """Classify one statement line. Returns (category, flags).

    Pure function: keyword rules first (ordered, case-insensitive), then
    'deposit' / sign-of-amount fallbacks. Flags mark fixed obligations,
    add-back candidates, and internal transfers for the downstream engines.
    """
    desc = (description or "").lower()
    category: str | None = None
    for cat, keywords in _KEYWORD_RULES:
        if any(k in desc for k in keywords):
            category = cat
            break
    if category is None:
        if "deposit" in desc or amount > 0:
            category = "revenue"
        else:
            category = "vendor"

    flags: dict = {}
    if category in _FIXED_CATEGORIES:
        flags["fixed"] = True
    elif category in _ADDBACK_CATEGORIES:
        flags["addback_candidate"] = True
        flags["irregular"] = True
    elif category == "transfer":
        flags["transfer"] = True
    return category, flags


def period_of(d: date) -> date:
    """First day of the month containing d — the monthly-grain period key."""
    return d.replace(day=1)


async def rebuild_periods(db: AsyncSession, dealer_id: UUID, periods: set[date]) -> int:
    """Recompute deposits/withdrawals for each period from the event ledger
    and upsert dos_financial_periods rows.

    Rules:
    - deposits  = sum of positive amounts, excluding internal transfers
    - withdrawals = abs sum of negative amounts, excluding internal transfers
    - deposits/withdrawals are ALWAYS recomputed (event ledger is truth)
    - balance/ebitda/revenue fields already set on a source='manual' row are
      never overwritten here (manual wins; this function does not touch them)
    - new rows are created with source='upload'

    Returns the number of periods touched. Does not commit — callers own the
    transaction boundary.
    """
    touched = 0
    for period in sorted(periods):
        rows = (
            await db.execute(
                select(DealerCashEvent.amount, DealerCashEvent.category).where(
                    DealerCashEvent.dealer_id == dealer_id,
                    DealerCashEvent.period == period,
                )
            )
        ).all()
        deposits = round(sum(float(a) for a, c in rows if a is not None and a > 0 and c != "transfer"), 2)
        withdrawals = round(sum(-float(a) for a, c in rows if a is not None and a < 0 and c != "transfer"), 2)

        fp = (
            await db.execute(
                select(DealerFinancialPeriod).where(
                    DealerFinancialPeriod.dealer_id == dealer_id,
                    DealerFinancialPeriod.period == period,
                )
            )
        ).scalar_one_or_none()
        if fp is None:
            fp = DealerFinancialPeriod(dealer_id=dealer_id, period=period, source="upload")
            db.add(fp)
        # Always recomputed from events; all other fields untouched so a
        # manual row keeps its manually-entered balances/EBITDA/revenue.
        fp.deposits = deposits
        fp.withdrawals = withdrawals
        touched += 1
    await db.flush()
    return touched
