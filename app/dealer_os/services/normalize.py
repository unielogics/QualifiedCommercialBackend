"""Ingestion normalization — Stream 2 (+ Phase 3 rules & per-account scoping).

Pure classification of raw statement lines into cash-event categories/flags,
plus the period rebuild that keeps dos_financial_periods consistent with the
event ledger. classify_event is a pure function (unit-testable, no IO);
rebuild_periods is the single reconcile choke point every ingestion path
(uploads now, Plaid/QBO later) calls after touching events.

Phase 3 adds:
- category rules (dos_category_rules): admin-authored substring rules applied
  BEFORE the keyword heuristics — first matching active rule wins, and
  dealer-scoped rules are checked before global ones. load_active_rules loads
  a dealer's effective rule list once per request; classify_with_rules is the
  pure application wrapper.
- per-account rebuilds: rebuild_periods is scoped to one (dealer, account)
  pair per call (account_id=None = the legacy null-account scope), so an
  account-tagged ingest never clobbers legacy blended rows and vice versa.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DealerCashEvent, DealerCategoryRule, DealerFinancialPeriod

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


def flags_for(category: str) -> dict:
    """Flags implied by a category — shared by heuristics, rules, and
    retro-recategorization so every path stamps consistent flags."""
    flags: dict = {}
    if category in _FIXED_CATEGORIES:
        flags["fixed"] = True
    elif category in _ADDBACK_CATEGORIES:
        flags["addback_candidate"] = True
        flags["irregular"] = True
    elif category == "transfer":
        flags["transfer"] = True
    return flags


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
    return category, flags_for(category)


def match_rule(rules: list[dict], description: str) -> dict | None:
    """First active rule whose (lowercase substring) pattern hits — rules must
    already be ordered dealer-scoped-first (load_active_rules does this)."""
    desc = (description or "").lower()
    for rule in rules:
        pattern = (rule.get("pattern") or "").lower().strip()
        if pattern and pattern in desc:
            return rule
    return None


def classify_with_rules(rules: list[dict], description: str, amount: float) -> tuple[str, dict, bool]:
    """Rules-aware classification: admin/global category rules are applied
    BEFORE the built-in heuristics (first matching rule wins). Returns
    (category, flags, rule_matched) so callers can stamp categorized_by='rule'
    when a rule decided the category."""
    rule = match_rule(rules, description)
    if rule is not None:
        category = rule["category"]
        return category, flags_for(category), True
    category, flags = classify_event(description, amount)
    return category, flags, False


async def load_active_rules(db: AsyncSession, dealer_id: UUID) -> list[dict]:
    """The dealer's effective rule list, ordered so the first match wins:
    dealer-scoped rules before global (dealer_id NULL) rules, newest first
    within each scope. Load once per request and pass around as plain dicts."""
    rows = (
        await db.execute(
            select(DealerCategoryRule)
            .where(
                DealerCategoryRule.active.is_(True),
                (DealerCategoryRule.dealer_id == dealer_id) | (DealerCategoryRule.dealer_id.is_(None)),
            )
            .order_by(
                DealerCategoryRule.dealer_id.is_(None).asc(),  # dealer-scoped first
                DealerCategoryRule.created_at.desc(),
            )
        )
    ).scalars().all()
    return [
        {"id": r.id, "pattern": r.pattern, "category": r.category, "dealer_id": r.dealer_id}
        for r in rows
    ]


def period_of(d: date) -> date:
    """First day of the month containing d — the monthly-grain period key."""
    return d.replace(day=1)


async def rebuild_periods(
    db: AsyncSession, dealer_id: UUID, periods: set[date], account_id: UUID | None = None
) -> int:
    """Recompute deposits/withdrawals for each period from the event ledger
    and upsert dos_financial_periods rows — scoped to ONE (dealer, account)
    pair per call (Phase 3). account_id=None is the legacy null-account scope:
    only null-account events feed it and only the null-account period row is
    touched, so account-tagged and legacy blended rows never clobber each
    other.

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
    account_clause = (
        DealerCashEvent.account_id == account_id
        if account_id is not None
        else DealerCashEvent.account_id.is_(None)
    )
    period_account_clause = (
        DealerFinancialPeriod.account_id == account_id
        if account_id is not None
        else DealerFinancialPeriod.account_id.is_(None)
    )
    touched = 0
    for period in sorted(periods):
        rows = (
            await db.execute(
                select(DealerCashEvent.amount, DealerCashEvent.category).where(
                    DealerCashEvent.dealer_id == dealer_id,
                    DealerCashEvent.period == period,
                    account_clause,
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
                    period_account_clause,
                )
            )
        ).scalar_one_or_none()
        if fp is None:
            fp = DealerFinancialPeriod(
                dealer_id=dealer_id, period=period, account_id=account_id, source="upload"
            )
            db.add(fp)
        # Always recomputed from events; all other fields untouched so a
        # manual row keeps its manually-entered balances/EBITDA/revenue.
        fp.deposits = deposits
        fp.withdrawals = withdrawals
        touched += 1
    await db.flush()
    return touched
