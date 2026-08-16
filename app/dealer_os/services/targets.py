"""AI target proposals — deterministic, explainable, per-dealer.

Stream 1 ships the default rubric (no financial periods exist yet for a new
dealer). Once periods land (Stream 2+), proposals derive from the dealer's own
trailing data (fixed-obligation load, tier bands) with the same shape. Hard
rule enforced in the router: an admin override is never overwritten by a
re-proposal.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DealerBusiness, DealerFinancialPeriod, DealerMetricTarget
from .paths import monthly_payment_for_goal

# metric_key -> (default value, rationale template)
DEFAULT_RUBRIC: dict[str, tuple[float, str]] = {
    "adb_target": (500_000, "Tier 1 banking band default; refines to ~1.6× monthly fixed obligations once statements land"),
    "adb_floor": (250_000, "Covers ~30 days of fixed obligations for a dealer at this profile"),
    "dscr_target": (1.35, "Tier 1 requirement + 0.10x cushion over the 1.25x lender floor"),
    "dscr_floor": (1.25, "Common bank underwriting floor"),
    "ebitda_target": (1_250_000, "Supports a $1M facility at 1.35x after typical lender haircut"),
    "liquidity_operating_floor": (150_000, "Two payroll cycles plus rent and insurance"),
    "liquidity_debt_reserve": (100_000, "Next 60 days of scheduled debt service"),
    "nsf_tolerance": (0, "Lender clean-activity requirement"),
    "reconcile_sla_days": (5, "Statement freshness required for lender packages"),
}


async def propose_targets(db: AsyncSession, dealer: DealerBusiness) -> list[DealerMetricTarget]:
    """Upsert AI proposals for every metric. Never touches admin_value; an
    overridden row keeps status='overridden' and just gets a fresh proposal."""
    now = datetime.now(timezone.utc)
    existing = {
        t.metric_key: t
        for t in (
            await db.execute(select(DealerMetricTarget).where(DealerMetricTarget.dealer_id == dealer.id))
        ).scalars()
    }
    # Data-aware refinement: if periods exist, scale ADB targets off observed
    # debt service (1.6× monthly fixed obligations, min the rubric default floor).
    periods = (
        await db.execute(
            select(DealerFinancialPeriod)
            .where(DealerFinancialPeriod.dealer_id == dealer.id)
            .order_by(DealerFinancialPeriod.period.desc())
            .limit(6)
        )
    ).scalars().all()
    rubric = dict(DEFAULT_RUBRIC)
    ds = [float(p.debt_service) for p in periods if p.debt_service is not None]
    monthly_ds = sum(ds) / len(ds) if ds else 0.0
    if ds:
        rubric["adb_target"] = (
            max(rubric["adb_target"][0], round(monthly_ds * 1.6, -3)),
            f"1.6× observed avg monthly debt service (${monthly_ds:,.0f}) across {len(ds)} months",
        )

    # 0119: a stated funding goal reshapes the proposals — the targets should
    # describe the bank profile that SUPPORTS the goal, not just today's book.
    # Same typical-case assumptions the program sizing uses; admin overrides
    # keep absolute precedence (this only ever touches ai_proposed_value).
    goal = float(dealer.funding_goal) if dealer.funding_goal is not None else 0.0
    goal_payment = monthly_payment_for_goal(goal) if goal > 0 else None
    if goal_payment is not None:
        obligations = monthly_ds + goal_payment
        rubric["adb_target"] = (
            max(rubric["adb_target"][0], round(obligations * 1.6, -3)),
            (
                f"1.6× fixed obligations including ~${goal_payment:,.0f}/mo for the proposed "
                f"facility — sized to support the ${goal:,.0f} goal"
            ),
        )
        rubric["dscr_target"] = (
            rubric["dscr_target"][0],
            (
                "Typical underwriting DSCR for the requested facility — "
                f"sized to support the ${goal:,.0f} goal"
            ),
        )
        rubric["liquidity_debt_reserve"] = (
            max(rubric["liquidity_debt_reserve"][0], round(2 * obligations, -3)),
            (
                "Next 60 days of scheduled debt service including the proposed facility — "
                f"sized to support the ${goal:,.0f} goal"
            ),
        )

    rows: list[DealerMetricTarget] = []
    for key, (value, rationale) in rubric.items():
        row = existing.get(key)
        if row is None:
            row = DealerMetricTarget(dealer_id=dealer.id, metric_key=key)
            db.add(row)
        row.ai_proposed_value = value
        row.ai_rationale = rationale
        row.ai_proposed_at = now
        if row.admin_value is None and row.status != "accepted":
            row.status = "ai_proposed"
        rows.append(row)
    await db.flush()
    return rows
