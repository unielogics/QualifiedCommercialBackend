"""Refinance workbench core — pure functions, no DB.

The center of the feature: simulate REMOVING selected debts from the
dealer's financial picture by replaying the observed ledger without those
lenders' debits, then layering the replacement note on top.

Three effects move together when a lender's debits leave the statements
(the same period-dict contract as services/simulate.apply_levers — deltas
apply only to observed non-None fields and clamp at >= 0):

  1. debt_service falls by that lender's OBSERVED monthly debits (the
     contract's monthly equivalent is the fallback for months with no
     ledger match);
  2. ebitda_reported rises by the financing-cost share those payments
     embedded in operating cash (factor cost on an MCA, interest on a
     card/loan) — principal is never an EBITDA item;
  3. balance fields rise because the debits stop draining the account
     (uniform-debit estimate: monthly amount held on average half the
     month — flagged as an estimate in the API response).

Guardrail: this models genuine payoff-and-replace refinancing under
written terms — floorplan working lines are not refinance targets and
are rejected upstream.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any, Iterable

from .paths import _payment_for_principal, _principal_for_payment  # shared amortization
from .vendors import normalize_vendor

# Contract cadence → monthly equivalent (business-day convention; observed
# ledger debits win over this whenever the lender is matched).
FREQUENCY_MONTHLY_MULT: dict[str, float] = {
    "daily": 21.0,
    "weekly": 4.33,
    "biweekly": 2.165,
    "monthly": 1.0,
}

# Categories that are never refinance targets (working lines fund inventory;
# retiring them isn't a consolidation move).
NEVER_REFI_CATEGORIES = frozenset({"floorplan"})

# Uniform-debit assumption: cash that stops leaving through the month is held,
# on average, half the month.
_ADB_LIFT_SHARE = 0.5

_BALANCE_FIELDS = ("avg_daily_balance", "ending_balance", "low_balance")


def monthly_equivalent(debt: Any) -> float:
    """The debt's monthly cash-out. monthly_payment (the engine's number)
    wins; otherwise derive it from the contract's native cadence."""
    if getattr(debt, "monthly_payment", None) is not None:
        return float(debt.monthly_payment)
    amount = getattr(debt, "payment_amount", None)
    freq = getattr(debt, "payment_frequency", None) or "monthly"
    if amount is None:
        return 0.0
    return round(float(amount) * FREQUENCY_MONTHLY_MULT.get(freq, 1.0), 2)


def financing_cost_monthly(debt: Any) -> float:
    """The monthly financing-cost share embedded in the debt's payments —
    the EBITDA add-back when the debt leaves the statements.

    MCA (factor_rate > 1): cost share of every dollar remitted is
    (1 - 1/factor). Rate-priced debt: balance x monthly rate. Rates stored
    as percents (8.9) or fractions (0.089) both work."""
    eq = monthly_equivalent(debt)
    factor = getattr(debt, "factor_rate", None)
    if factor is not None and float(factor) > 1.0:
        return round(eq * (1.0 - 1.0 / float(factor)), 2)
    rate = getattr(debt, "rate", None)
    balance = getattr(debt, "balance", None) or getattr(debt, "payoff_amount", None)
    if rate is not None and balance is not None:
        frac = float(rate) / 100.0 if float(rate) > 1.0 else float(rate)
        if 0.0 < frac < 1.0:
            return round(float(balance) * frac / 12.0, 2)
    return 0.0


def payoff_estimate(debt: Any) -> float:
    """payoff_amount when the contract states one, else the tracked balance."""
    for field in ("payoff_amount", "balance"):
        v = getattr(debt, field, None)
        if v is not None:
            return float(v)
    return 0.0


_MIN_MATCH_LEN = 6


def key_matches(debt_key: str | None, event_key: str) -> bool:
    """Vendor-identity match tolerant of the contract-name vs statement-
    descriptor gap: a contract says "Forward Financing" while the ledger
    normalizes to "ACH FORWARD FINANCING". Exact match first, then
    containment either way — guarded by a minimum length so generic
    fragments ("CAPITAL") never cross-match."""
    if not debt_key or not event_key:
        return False
    if debt_key == event_key:
        return True
    shorter, longer = sorted((debt_key, event_key), key=len)
    return len(shorter) >= _MIN_MATCH_LEN and shorter in longer


def observed_monthly(
    events: Iterable, debts: list, key_for: Any = None
) -> dict[Any, dict[date, float]]:
    """Per-debt observed OUTFLOW totals by calendar month, matched on the
    debt's ledger identity via the shared vendor-identity normalizer
    (containment-tolerant, see key_matches). key_for overrides how a row's
    identity is derived (default: its vendor_key). Rows with no identity
    simply get no observed data — the contract monthly equivalent covers
    them."""
    get_key = key_for or (lambda d: getattr(d, "vendor_key", None))
    keyed = [(k, d) for k, d in ((get_key(d), d) for d in debts) if k]
    out: dict[Any, dict[date, float]] = {d.id: defaultdict(float) for d in debts}
    if not keyed:
        return {k: dict(v) for k, v in out.items()}
    for row in events:
        amount = float(row.amount or 0)
        if amount >= 0:  # outflows only
            continue
        event_key = normalize_vendor(row.description or "")
        for debt_key, debt in keyed:
            if key_matches(debt_key, event_key):
                month = date(row.occurred_on.year, row.occurred_on.month, 1)
                out[debt.id][month] += -amount
                break
    return {k: dict(v) for k, v in out.items()}


def removal_effects(
    periods: list[dict],
    removed: list,
    observed: dict[Any, dict[date, float]],
    new_payment_monthly: float = 0.0,
) -> list[dict]:
    """NEW period dicts with the selected debts' debits replayed OUT of the
    months and the replacement note's payment layered in. Inputs untouched;
    zero removals + zero payment reproduces the baseline exactly."""
    out: list[dict] = []
    for p in periods:
        q = dict(p)
        month = q.get("period")
        month_key = date(month.year, month.month, 1) if month else None
        ds_removed = 0.0
        ebitda_back = 0.0
        adb_lift = 0.0
        for d in removed:
            by_month = observed.get(d.id) or {}
            month_amt = by_month.get(month_key) if month_key else None
            amt = float(month_amt) if month_amt else monthly_equivalent(d)
            ds_removed += amt
            ebitda_back += financing_cost_monthly(d)
            adb_lift += amt * _ADB_LIFT_SHARE
        if ds_removed and q.get("debt_service") is not None:
            q["debt_service"] = max(0.0, float(q["debt_service"]) - ds_removed)
        if new_payment_monthly and q.get("debt_service") is not None:
            q["debt_service"] = float(q["debt_service"]) + new_payment_monthly
        if ebitda_back and q.get("ebitda_reported") is not None:
            q["ebitda_reported"] = float(q["ebitda_reported"]) + ebitda_back
        if adb_lift:
            for field in _BALANCE_FIELDS:
                if q.get(field) is not None:
                    q[field] = max(0.0, float(q[field]) + adb_lift)
        out.append(q)
    return out


def new_loan_payment(amount: float, annual_rate_pct: float, term_months: int) -> float:
    """Amortized monthly payment — the exact inverse the sizing engine uses."""
    if amount <= 0 or term_months < 1:
        return 0.0
    return round(_payment_for_principal(amount, annual_rate_pct / 100.0, term_months), 2)


def max_principal(
    ebitda_bankable_annual: float,
    retained_ds_monthly: float,
    dscr_floor: float,
    annual_rate_pct: float,
    term_months: int,
    ceiling: float | None = None,
) -> float:
    """Reverse-engineer the largest note the scenario supports: the payment
    budget at the DSCR floor, less what the dealer keeps paying, converted
    to principal at the quoted rate/term. Floored to $1k like size_program."""
    if dscr_floor <= 0 or term_months < 1:
        return 0.0
    budget = ebitda_bankable_annual / 12.0 / dscr_floor - retained_ds_monthly
    if budget <= 0:
        return 0.0
    principal = _principal_for_payment(budget, annual_rate_pct / 100.0, term_months)
    if ceiling is not None:
        principal = min(principal, float(ceiling))
    return float(max(0, int(principal // 1000) * 1000))
