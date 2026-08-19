"""Statement-only readiness — the MCA-underwriter lens over ~6 months.

Revenue/velocity analysis only: no NOI, no tax returns, no add-backs. The
firm structures these as term loans; the mechanics mirror how a
daily-remittance funder reads the account. ALL CONSTANTS ARE PROVISIONAL
LENDING JUDGMENTS pending desk sign-off.
"""

from __future__ import annotations

from typing import Any, Iterable

# --- provisional desk constants ------------------------------------------------
ADVANCE_PCT_RANGE = (0.70, 1.20)   # of Average Monthly Revenue
ADVANCE_PCT_TYPICAL = 0.80
# Firm pricing: 25-40% APR converts to ~1.25-1.40x factors over the
# standard 14-month term (user-specified).
FACTOR_RATE = 1.30
FACTOR_RANGE = (1.25, 1.40)
TERM_BUSINESS_DAYS = 14 * 21       # 14-month standard term
MAX_TERM_BUSINESS_DAYS = 14 * 21   # never stretch past 14 months — reduce instead
BIZ_DAYS_PER_MONTH = 21
MAX_DAILY_PULL_PCT = 0.15          # existing + new pulls vs daily revenue
MIN_DEPOSITS_PER_MONTH = 5
MIN_ADB = 1_000.0
MAX_NSF_PER_MONTH = 3.0

# Inflow categories that are NOT revenue (scrubbing).
_NON_REVENUE_IN = frozenset({"transfer", "loan", "owner_draw"})
# Cadences that imply a daily/weekly ACH pull (an existing position).
_PULL_CADENCES = frozenset({"weekly", "continuous", "semi_monthly"})


def compute_mca_readiness(
    periods: list[dict],
    rolled: Iterable,
    adb_current: float | None,
    *,
    advance_pct: float | None = None,
    factor_rate: float | None = None,
    term_days: int | None = None,
) -> dict[str, Any]:
    adv_pct = advance_pct if advance_pct else ADVANCE_PCT_TYPICAL
    factor = factor_rate if factor_rate else FACTOR_RATE
    base_term = term_days if term_days else TERM_BUSINESS_DAYS
    months = [p for p in periods if p.get("period") and p.get("deposits") is not None]
    n = max(len(months), 1)

    # 1) AMR — scrubbed revenue: total deposits minus non-revenue inflows.
    total_deposits = sum(float(p["deposits"]) for p in months if p.get("deposits") is not None)
    non_revenue_monthly = sum(
        abs(float(v.monthly_average)) for v in rolled
        if v.direction > 0 and v.category in _NON_REVENUE_IN
    )
    amr = round(max(0.0, total_deposits / n - non_revenue_monthly), 2)
    daily_revenue = round(amr / BIZ_DAYS_PER_MONTH, 2)

    # 2) Health checks.
    deposit_freq = round(
        sum(v.count for v in rolled if v.direction > 0 and v.category not in _NON_REVENUE_IN)
        / n, 1,
    )
    nsf_avg = round(sum(int(p.get("nsf_count") or 0) for p in months) / n, 2)
    existing_daily_pull = round(
        sum(
            abs(float(v.monthly_average)) for v in rolled
            if v.direction < 0 and v.debt_like and v.is_recurring and v.cadence in _PULL_CADENCES
        ) / BIZ_DAYS_PER_MONTH, 2,
    )
    existing_pull_pct = round(existing_daily_pull / daily_revenue, 4) if daily_revenue > 0 else None

    checks = [
        {
            "key": "deposit_frequency", "label": "Deposit frequency",
            "value": deposit_freq, "threshold": MIN_DEPOSITS_PER_MONTH,
            "unit": "deposits/mo", "passed": deposit_freq >= MIN_DEPOSITS_PER_MONTH,
            "detail": "Steady daily sales beat one or two big invoices a month.",
        },
        {
            "key": "adb", "label": "Average daily balance",
            "value": adb_current, "threshold": MIN_ADB,
            "unit": "$", "passed": adb_current is not None and adb_current >= MIN_ADB,
            "detail": "Buffer to absorb a daily ACH without bouncing.",
        },
        {
            "key": "nsf", "label": "NSFs / overdrafts",
            "value": nsf_avg, "threshold": MAX_NSF_PER_MONTH,
            "unit": "per month", "passed": nsf_avg <= MAX_NSF_PER_MONTH,
            "detail": "Bounces mean the account is riding the float.",
        },
        {
            "key": "stacking", "label": "Existing daily/weekly positions",
            "value": round((existing_pull_pct or 0.0) * 100, 1),
            "threshold": MAX_DAILY_PULL_PCT * 100,
            "unit": "% of daily revenue",
            "passed": existing_pull_pct is None or existing_pull_pct <= MAX_DAILY_PULL_PCT,
            "detail": "Other funders already pulling from the account.",
        },
    ]

    # 3) Back into the offer with the daily-pull stress test.
    offer: dict[str, Any] = {}
    if amr > 0:
        advance = round(amr * adv_pct, -2)
        payback = round(advance * factor, 2)
        term = base_term
        daily_payment = round(payback / term, 2)
        pull_pct = (existing_daily_pull + daily_payment) / daily_revenue if daily_revenue > 0 else 1.0
        stretched = reduced = False
        if pull_pct > MAX_DAILY_PULL_PCT and daily_revenue > 0:
            budget = daily_revenue * MAX_DAILY_PULL_PCT - existing_daily_pull
            if budget > 0:
                needed_term = int(payback / budget) + 1
                if needed_term <= max(MAX_TERM_BUSINESS_DAYS, base_term):
                    term, stretched = needed_term, True
                else:
                    term = max(MAX_TERM_BUSINESS_DAYS, base_term)
                    payback = round(budget * term, 2)
                    advance = round(payback / factor, -2)
                    stretched = reduced = True
                daily_payment = round(payback / term, 2)
                pull_pct = (existing_daily_pull + daily_payment) / daily_revenue
            else:
                advance = 0.0  # existing pulls already exceed the budget
        offer = {
            "advance": advance,
            "advance_range": [round(amr * ADVANCE_PCT_RANGE[0], -2), round(amr * ADVANCE_PCT_RANGE[1], -2)],
            "factor_rate": factor,
            "payback": payback if advance else 0.0,
            "term_business_days": term,
            "daily_payment": daily_payment if advance else 0.0,
            "pull_pct": round(pull_pct * 100, 1) if advance else None,
            "stretched": stretched,
            "reduced": reduced,
        }

    failed = [c["key"] for c in checks if not c["passed"]]
    verdict = (
        "not_yet" if (amr <= 0 or (offer and not offer.get("advance")))
        else "eligible" if not failed
        else "conditional"
    )
    return {
        "amr": amr,
        "daily_revenue": daily_revenue,
        "months_used": len(months),
        "existing_daily_pull": existing_daily_pull,
        "checks": checks,
        "offer": offer,
        "verdict": verdict,
        "failed_checks": failed,
    }
