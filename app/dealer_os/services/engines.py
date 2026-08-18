"""Metric engines — Stream 3.

compute_metrics is a PURE function (no IO, unit-testable) that turns trailing
monthly periods + verified add-backs + effective targets into the full metric
tree, score and tier. recompute_snapshot is the DB choke point: it loads the
inputs, calls the pure engine, persists a new immutable DealerMetricSnapshot,
writes lineage edges (which periods/add-backs/targets fed each metric), and
raises floor alerts. It flushes but never commits — callers own the
transaction boundary.
"""

from __future__ import annotations

import types
from dataclasses import dataclass
from datetime import date
from typing import Iterable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    DealerAccount,
    DealerBusiness,
    DealerAddback,
    DealerCashEvent,
    DealerAlert,
    DealerDebt,
    DealerFinancialPeriod,
    DealerMetricLineage,
    DealerMetricSnapshot,
    DealerMetricTarget,
    DealerProgramSetting,
    DealerTaxFiling,
)

# 4% lender haircut: bankable EBITDA = adjusted * 0.96
BANKABLE_FACTOR = 0.96

# A funding path at or above this readiness raises a fundability_<key> alert.
FUNDABILITY_READINESS_PCT = 90.0

# metric_key of a target row -> metric family it feeds (for lineage edges).
# Targets outside this map (e.g. reconcile_sla_days) are not consumed here.
_TARGET_LINEAGE: dict[str, str] = {
    "ebitda_target": "ebitda",
    "dscr_target": "dscr",
    "dscr_floor": "dscr",
    "adb_target": "adb",
    "adb_floor": "adb",
    "liquidity_operating_floor": "liquidity",
    "nsf_tolerance": "nsf",
}


def _avg(values: Iterable[float | None]) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _round2(v: float | None) -> float | None:
    return round(v, 2) if v is not None else None


def _gap(target: float | None, current: float | None) -> float | None:
    if target is None or current is None:
        return None
    return round(max(0.0, target - current), 2)



# EBITDA components as they appear on a business return. Names vary by form and
# preparer, so each maps to a list of accepted keys.
_TAX_EBITDA_FIELDS: dict[str, tuple[str, ...]] = {
    "base": ("ordinary_business_income", "net_income", "taxable_income", "book_income"),
    "interest": ("interest_expense", "floor_plan_interest_expense", "mortgage_interest"),
    "depreciation": ("depreciation", "depreciation_expense", "depreciation_amortization"),
    "amortization": ("amortization", "amortization_expense"),
    "taxes": ("taxes_paid", "income_tax_expense"),
}


def _tax_ebitda(filing) -> float | None:
    """Rebuild annual EBITDA from a business tax return's stored figures.

    EBITDA = ordinary business income + interest + taxes + depreciation +
    amortization. Officer compensation is deliberately NOT added back here:
    it is an add-back only when the owner's pay is above market, which is a
    human judgement that belongs in the add-back editor with evidence — not a
    silent boost to the headline number a lender sees.

    Returns None when the filing carries no usable base figure, so the metric
    stays honestly null rather than showing a fabricated zero."""
    if filing is None or not isinstance(getattr(filing, "detail", None), dict):
        return None
    detail = filing.detail

    def pick(keys: tuple[str, ...]) -> float | None:
        for k in keys:
            v = detail.get(k)
            if v is None:
                continue
            try:
                return float(str(v).replace(",", "").replace("$", ""))
            except (TypeError, ValueError):
                continue
        return None

    base = pick(_TAX_EBITDA_FIELDS["base"])
    if base is None:
        return None
    total = base
    for part in ("interest", "depreciation", "amortization", "taxes"):
        v = pick(_TAX_EBITDA_FIELDS[part])
        if v is not None:
            total += abs(v)
    return round(total, 2)


def compute_metrics(
    periods: list[dict],
    addbacks_annual_verified: float,
    targets: dict[str, float | None],
    fallbacks: dict[str, float | None] | None = None,
) -> dict:
    """Pure metric engine over up to 6 trailing monthly periods.

    ``periods`` is most-recent-first; each dict carries ebitda_reported,
    debt_service, avg_daily_balance, ending_balance, low_balance, nsf_count,
    deposits (floats or None). Deterministic — same inputs, same output.

    ``fallbacks`` supplies figures bank statements cannot carry, so the two
    headline pillars are not permanently null on a statements-only dealer:

      tax_ebitda_annual     EBITDA rebuilt from the latest business tax return
                            (ordinary income + interest + D&A). Bank statements
                            have no income statement, so without this
                            ebitda_reported is NULL on every period and the
                            whole EBITDA -> DSCR chain never computes.
      debt_schedule_monthly Total monthly payment from the debt schedule
                            (dos_debts), used when no period carries observed
                            debt service.

    A fallback is only consulted when the observed value is missing — anything
    extracted from a statement or set by a human always wins. Each metric
    reports its `source` so the UI can show where the number came from.
    """
    fallbacks = fallbacks or {}
    ebitda_target = targets.get("ebitda_target")
    dscr_target = targets.get("dscr_target")
    dscr_floor = targets.get("dscr_floor")
    adb_target = targets.get("adb_target")
    adb_floor = targets.get("adb_floor")
    liquidity_floor = targets.get("liquidity_operating_floor")
    nsf_tolerance = targets.get("nsf_tolerance")

    # --- EBITDA ladder: reported TTM -> adjusted -> bankable ---------------
    monthly_ebitda = _avg(p.get("ebitda_reported") for p in periods)
    ebitda_reported_ttm = _round2(monthly_ebitda * 12) if monthly_ebitda is not None else None
    ebitda_source = "periods"
    if ebitda_reported_ttm is None:
        tax_ebitda = fallbacks.get("tax_ebitda_annual")
        if tax_ebitda is not None:
            ebitda_reported_ttm = _round2(float(tax_ebitda))
            ebitda_source = "tax_return"
        else:
            ebitda_source = "none"
    ebitda_adjusted = (
        _round2(ebitda_reported_ttm + float(addbacks_annual_verified or 0.0))
        if ebitda_reported_ttm is not None
        else None
    )
    ebitda_bankable = _round2(ebitda_adjusted * BANKABLE_FACTOR) if ebitda_adjusted is not None else None

    # --- DSCR --------------------------------------------------------------
    # Denominator precedence: statement-carried debt service > OBSERVED
    # ledger debits to debt-like lenders (loans/floorplan — credit cards
    # excluded: a card paid monthly is operating spend routed through a
    # card, not debt service, unless an admin confirms a carried balance
    # on the schedule) > the drafted schedule total.
    monthly_ds = _avg(p.get("debt_service") for p in periods)
    ds_source = "periods"
    if monthly_ds is None:
        observed = fallbacks.get("debt_service_observed_monthly")
        drafted = fallbacks.get("debt_schedule_monthly")
        if observed is not None and float(observed) > 0:
            monthly_ds = float(observed)
            ds_source = "observed_ledger"
        elif drafted is not None and float(drafted) > 0:
            monthly_ds = float(drafted)
            ds_source = "debt_schedule"
        else:
            ds_source = "none"
    annual_ds = _round2(monthly_ds * 12) if monthly_ds is not None else None
    dscr = (
        round(ebitda_bankable / annual_ds, 3)
        if ebitda_bankable is not None and annual_ds is not None and annual_ds != 0
        else None
    )

    # Cash-flow cross-check, fully ledger-derived: what the account actually
    # kept each month vs what it paid lenders. (net + debt) / debt — the
    # cash-based DSCR a statement lender computes by hand.
    # DRAFT DSCR: when nothing is confirmed, the system still derives a
    # ratio from IDENTIFIED debt-like activity (cards included) vs the
    # inbound-derived numerator — never a blank tile, never an infinity.
    draft_ds = (fallbacks or {}).get("debt_service_draft_monthly")
    dscr_draft = None
    if draft_ds and float(draft_ds) > 0 and ebitda_bankable is not None:
        dscr_draft = round(ebitda_bankable / (float(draft_ds) * 12.0), 3)

    # DSCR at the funding goal: coverage of (current DS + the goal's implied
    # payment) — defined even at zero current debt.
    goal_payment = (fallbacks or {}).get("goal_monthly_payment")
    dscr_at_goal = None
    if goal_payment and ebitda_bankable is not None:
        goal_ds_annual = ((monthly_ds or 0.0) + float(goal_payment)) * 12.0
        if goal_ds_annual > 0:
            dscr_at_goal = round(ebitda_bankable / goal_ds_annual, 3)

    dep_avg = _avg(p.get("deposits") for p in periods)
    wd_avg = _avg(p.get("withdrawals") for p in periods)
    dscr_cash_flow = None
    net_cash_flow_monthly = None
    if dep_avg is not None and wd_avg is not None:
        net_cash_flow_monthly = _round2(dep_avg - wd_avg)
        if monthly_ds is not None and monthly_ds > 0:
            dscr_cash_flow = round((net_cash_flow_monthly + monthly_ds) / monthly_ds, 3)

    # --- ADB (fall back to avg ending balance when no ADB observed) --------
    adb = _avg(p.get("avg_daily_balance") for p in periods)
    if adb is None:
        adb = _avg(p.get("ending_balance") for p in periods)
    adb = _round2(adb)

    # --- Liquidity: latest observed ending balance -------------------------
    liquidity_current = next(
        (float(p["ending_balance"]) for p in periods if p.get("ending_balance") is not None), None
    )
    liquidity_current = _round2(liquidity_current)
    liquidity_excess = (
        _round2(liquidity_current - liquidity_floor)
        if liquidity_current is not None and liquidity_floor is not None
        else None
    )
    # Deployable cash is computed HERE, once, and read everywhere. The Treasury
    # chart used to derive its own — subtracting the operating floor AND the
    # debt reserve — while this block subtracted only the operating floor, so
    # the same dealer showed "excess $25,851" on one screen and "deployable $0"
    # on another. Deployable is the stricter figure (what is genuinely free
    # after every required reserve) and is floored at zero: you cannot deploy
    # cash you do not have.
    debt_reserve = targets.get("liquidity_debt_reserve")
    required_reserve = _round2(
        sum(v for v in (liquidity_floor, debt_reserve) if v is not None)
    ) if (liquidity_floor is not None or debt_reserve is not None) else None
    deployable = (
        _round2(max(0.0, liquidity_current - (required_reserve or 0.0)))
        if liquidity_current is not None
        else None
    )
    # Negative headroom is the number that actually matters to an operator —
    # how far BELOW the required reserves they are — and clamping hides it.
    reserve_shortfall = (
        _round2(max(0.0, (required_reserve or 0.0) - liquidity_current))
        if liquidity_current is not None and required_reserve is not None
        else None
    )

    # --- NSF ---------------------------------------------------------------
    nsf_6mo = sum(int(p.get("nsf_count") or 0) for p in periods)

    # --- Score: deterministic 0-100 ----------------------------------------
    score: float | None = None
    if periods:
        s = 40.0
        if ebitda_bankable is not None and ebitda_target is not None and ebitda_target > 0:
            s += 20.0 * min(1.0, ebitda_bankable / ebitda_target)
        if dscr is not None and dscr_target is not None and dscr_target > 0:
            s += 25.0 * min(1.0, dscr / dscr_target)
        if adb is not None and adb_target is not None and adb_target > 0:
            s += 10.0 * min(1.0, adb / adb_target)
        if liquidity_current is not None and liquidity_floor is not None and liquidity_current >= liquidity_floor:
            s += 5.0
        if nsf_6mo > 0:
            s -= min(10.0, 3.0 * nsf_6mo)
        score = round(min(100.0, max(0.0, s)), 1)

    # --- Tier --------------------------------------------------------------
    tier: str | None = None
    if (
        dscr is not None
        and dscr_target is not None
        and adb is not None
        and adb_target is not None
        and dscr >= dscr_target
        and adb >= adb_target
    ):
        tier = "Tier 1 ready"
    elif dscr is not None and dscr_floor is not None and dscr >= dscr_floor:
        tier = "Tier 2"
    elif periods:
        tier = "Tier 3"

    return {
        "score": score,
        "tier": tier,
        "ebitda": {
            "source": ebitda_source,
            "reported_ttm": ebitda_reported_ttm,
            "adjusted": ebitda_adjusted,
            "bankable": ebitda_bankable,
            "target": ebitda_target,
            "gap": _gap(ebitda_target, ebitda_bankable),
        },
        "dscr": {
            "source": ds_source,
            "cash_flow": dscr_cash_flow,
            "at_goal": dscr_at_goal,
            "draft": dscr_draft,
            "draft_monthly_ds": _round2(float(draft_ds)) if draft_ds else None,
            "display": (
                "confirmed" if dscr is not None
                else "draft" if dscr_draft is not None
                else "cash_flow" if dscr_cash_flow is not None
                else "insufficient"
            ),
            "net_cash_flow_monthly": net_cash_flow_monthly,
            "ebitda_source": ebitda_source,
            "monthly_debt_service": _round2(monthly_ds),
            "current": dscr,
            "target": dscr_target,
            "floor": dscr_floor,
            "gap": _gap(dscr_target, dscr),
        },
        "adb": {
            "current": adb,
            "target": adb_target,
            "floor": adb_floor,
            "gap": _gap(adb_target, adb),
        },
        "liquidity": {
            "current": liquidity_current,
            "floor": liquidity_floor,
            "excess": liquidity_excess,
            "debt_reserve": debt_reserve,
            "required_reserve": required_reserve,
            "deployable": deployable,
            "reserve_shortfall": reserve_shortfall,
        },
        "nsf": {
            "count_6mo": nsf_6mo,
            "tolerance": nsf_tolerance,
        },
        "periods_used": len(periods),
    }


async def _ensure_alert(
    db: AsyncSession,
    dealer_id: UUID,
    kind: str,
    severity: str,
    message: str,
    ref_kind: str | None = None,
    ref_id: UUID | None = None,
) -> DealerAlert | None:
    """Raise an alert unless an unresolved one of the same kind already exists."""
    existing = (
        await db.execute(
            select(DealerAlert.id)
            .where(
                DealerAlert.dealer_id == dealer_id,
                DealerAlert.kind == kind,
                DealerAlert.resolved_at.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None
    alert = DealerAlert(
        dealer_id=dealer_id,
        kind=kind,
        severity=severity,
        message=message[:320],
        ref_kind=ref_kind,
        ref_id=ref_id,
    )
    db.add(alert)
    return alert


def _f(v) -> float | None:
    return float(v) if v is not None else None


@dataclass(frozen=True)
class MetricInputs:
    """Everything compute_metrics needs for one dealer, loaded once.

    period_rows keep .id/.period for lineage edges; periods are the plain
    dicts the pure engine takes (most-recent-first). addback_rows carry EVERY
    status — recompute_snapshot filters to verified, the read-only simulator
    also wants the candidate/review pool."""

    period_rows: list
    periods: list[dict]
    addback_rows: list
    target_rows: list
    targets: dict[str, float | None]
    fallbacks: dict[str, float | None]

    @property
    def addbacks_annual_verified(self) -> float:
        # annual_amount wins; a monthly-only addback annualizes x12 (it used
        # to contribute 0 — a real bug the DSCR-composition round fixed).
        total = 0.0
        for a in self.addback_rows:
            if a.status != "verified":
                continue
            if a.annual_amount is not None:
                total += float(a.annual_amount)
            elif a.monthly_amount is not None:
                total += float(a.monthly_amount) * 12.0
        return total


async def load_metric_inputs(db: AsyncSession, dealer_id: UUID) -> MetricInputs:
    """Shared input loader for recompute_snapshot and the what-if simulator —
    one place decides how per-account period rows collapse into months and
    which fallbacks apply. Read-only: selects, never writes."""
    # Per-account rows mean one calendar month can span several rows. Fetch a
    # wider window, collapse to one dict per month (flow fields summed across
    # accounts, balance fields preferring the primary operating account, then
    # legacy null-account rows), THEN take the trailing 6 months — so tagged and
    # legacy rows never double-count.
    raw_rows = (
        (
            await db.execute(
                select(DealerFinancialPeriod)
                .where(DealerFinancialPeriod.dealer_id == dealer_id)
                .order_by(DealerFinancialPeriod.period.desc())
                .limit(36)
            )
        )
        .scalars()
        .all()
    )
    primary_ids = {
        r[0]
        for r in (
            await db.execute(
                select(DealerAccount.id).where(
                    DealerAccount.dealer_id == dealer_id,
                    DealerAccount.role == "primary_operating",
                )
            )
        ).all()
    }

    def _rank(row) -> int:  # lower = preferred for balance-type fields
        if row.account_id in primary_ids:
            return 0
        if row.account_id is None:
            return 1
        return 2

    by_month: dict = {}
    seen_flow_sig: set = set()
    for row in raw_rows:
        # Duplicate-attribution guard (user-confirmed data defect): the same
        # statement ingested twice shows as two rows with identical
        # deposits+withdrawals in one month under different accounts. Summing
        # them double-counts every flow metric — keep the first.
        sig = (row.period, row.deposits, row.withdrawals, row.ending_balance)
        if row.account_id is not None and None not in sig[1:] and sig in seen_flow_sig:
            continue
        if row.account_id is not None and None not in sig[1:]:
            seen_flow_sig.add(sig)
        by_month.setdefault(row.period, []).append(row)
    period_rows = []
    for month in sorted(by_month, reverse=True)[:6]:
        rows = sorted(by_month[month], key=_rank)
        merged = rows[0]
        if len(rows) > 1:

            def _sum(field):
                vals = [float(getattr(r, field)) for r in rows if getattr(r, field) is not None]
                return sum(vals) if vals else None

            def _pref(field):
                for r in rows:
                    v = getattr(r, field)
                    if v is not None:
                        return float(v)
                return None

            merged = types.SimpleNamespace(
                id=rows[0].id,
                period=month,
                deposits=_sum("deposits"),
                withdrawals=_sum("withdrawals"),
                nsf_count=sum(int(r.nsf_count or 0) for r in rows),
                ebitda_reported=_pref("ebitda_reported"),
                debt_service=_pref("debt_service"),
                avg_daily_balance=_pref("avg_daily_balance"),
                ending_balance=_pref("ending_balance"),
                low_balance=_pref("low_balance"),
            )
        period_rows.append(merged)
    addback_rows = (
        (
            await db.execute(
                select(DealerAddback).where(DealerAddback.dealer_id == dealer_id)
            )
        )
        .scalars()
        .all()
    )
    target_rows = (
        (
            await db.execute(
                select(DealerMetricTarget).where(DealerMetricTarget.dealer_id == dealer_id)
            )
        )
        .scalars()
        .all()
    )
    targets = {
        t.metric_key: (float(t.effective_value) if t.effective_value is not None else None)
        for t in target_rows
    }

    periods = [
        {
            # calendar month key — consumers that adjust per-month (the
            # refinance replay) key on this; the metric math ignores it.
            "period": p.period,
            "ebitda_reported": _f(p.ebitda_reported),
            "debt_service": _f(p.debt_service),
            "avg_daily_balance": _f(p.avg_daily_balance),
            "ending_balance": _f(p.ending_balance),
            "low_balance": _f(p.low_balance),
            "nsf_count": int(p.nsf_count or 0),
            "deposits": _f(p.deposits),
            "withdrawals": _f(p.withdrawals),
        }
        for p in period_rows
    ]

    # Fallbacks for figures bank statements cannot carry. Observed values
    # always win; these only fill a gap that would otherwise leave the metric
    # permanently null.
    # The debt schedule IS the DSCR denominator ledger (0129): every active
    # row with count_in_dscr contributes. For vendor-linked rows the OBSERVED
    # monthly debits win over the stated figure (primary source = what the
    # statements actually show); stated monthly_payment is the fallback.
    dscr_debt_rows = (
        (
            await db.execute(
                select(DealerDebt).where(
                    DealerDebt.dealer_id == dealer_id,
                    DealerDebt.status == "active",
                    DealerDebt.count_in_dscr.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )

    # OBSERVED debt service: what the ledger actually paid to debt-like
    # lenders (loans/floorplan) in the months the periods cover — credit
    # cards excluded for the same reason as above. Preferred over the
    # drafted schedule; statement-carried debt_service still wins overall.
    observed_ds_monthly = None
    draft_ds_monthly = None
    observed_avg_by_debt: dict = {}
    event_rows = []
    if period_rows:
        from .vendors import DEBT_CATEGORIES, rollup_vendors

        months_covered = {r.period for r in period_rows}
        earliest = min(months_covered)
        event_rows = (
            (
                await db.execute(
                    select(DealerCashEvent)
                    .where(
                        DealerCashEvent.dealer_id == dealer_id,
                        DealerCashEvent.period >= earliest,
                    )
                    .order_by(DealerCashEvent.occurred_on.desc())
                    .limit(8000)
                )
            )
            .scalars()
            .all()
        )
        if event_rows:
            rolled = rollup_vendors(event_rows)
            debt_keys = {
                v.key
                for v in rolled
                if v.debt_like and v.category != "credit_card"
            }
            # DRAFT universe: every debt-like vendor INCLUDING cards —
            # the always-drafts-something tier. Human-excluded schedule rows
            # (origin='admin', count_in_dscr=false) pull their vendors out.
            draft_keys = {v.key for v in rolled if v.debt_like}
            if debt_keys or draft_keys:
                from .vendors import normalize_vendor

                from .refinance import key_matches as _km

                excluded_rows = (
                    (
                        await db.execute(
                            select(DealerDebt.vendor_key).where(
                                DealerDebt.dealer_id == dealer_id,
                                DealerDebt.status == "active",
                                DealerDebt.origin == "admin",
                                DealerDebt.count_in_dscr.is_(False),
                                DealerDebt.vendor_key.is_not(None),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                by_month: dict = {}
                draft_by_month: dict = {}
                for r in event_rows:
                    amt = float(r.amount or 0)
                    if amt >= 0:
                        continue
                    ev_key = normalize_vendor(r.description or "")
                    mkey = date(r.period.year, r.period.month, 1)
                    if ev_key in debt_keys:
                        by_month[mkey] = by_month.get(mkey, 0.0) + (-amt)
                    if ev_key in draft_keys and not any(_km(x, ev_key) for x in excluded_rows):
                        draft_by_month[mkey] = draft_by_month.get(mkey, 0.0) + (-amt)
                # Average over the covered months (months with zero debt
                # debits count as zero — a skipped payment is information).
                if by_month:
                    total = sum(by_month.get(m, 0.0) for m in months_covered)
                    observed_ds_monthly = round(total / max(len(months_covered), 1), 2)
                if draft_by_month:
                    total = sum(draft_by_month.get(m, 0.0) for m in months_covered)
                    draft_ds_monthly = round(total / max(len(months_covered), 1), 2)

        # Per-schedule-row observed averages (containment-tolerant identity —
        # the same matcher the refinance workbench uses).
        if dscr_debt_rows and event_rows:
            from .refinance import observed_monthly as _debt_observed

            per_debt = _debt_observed(event_rows, dscr_debt_rows)
            n_months = max(len(months_covered), 1)
            for row in dscr_debt_rows:
                by_m = per_debt.get(row.id) or {}
                if by_m:
                    observed_avg_by_debt[row.id] = round(
                        sum(by_m.get(m, 0.0) for m in months_covered) / n_months, 2
                    )

    drafted_monthly = 0.0
    for row in dscr_debt_rows:
        observed = observed_avg_by_debt.get(row.id)
        if observed is not None and observed > 0:
            drafted_monthly += observed
        elif row.monthly_payment is not None:
            drafted_monthly += float(row.monthly_payment)
    drafted_monthly = round(drafted_monthly, 2)
    # DSCR-at-goal: the payment the funding goal implies at the desk's
    # conventional terms — gives the tile a meaningful ratio even at zero
    # current debt ("there is always a DSCR ratio").
    goal_payment = None
    goal_row = (
        await db.execute(select(DealerBusiness.funding_goal).where(DealerBusiness.id == dealer_id))
    ).scalar_one_or_none()
    if goal_row:
        from .paths import merged_settings, monthly_payment_for_goal

        setting_rows = (
            (await db.execute(select(DealerProgramSetting))).scalars().all()
        )
        goal_payment = monthly_payment_for_goal(
            float(goal_row), settings=merged_settings(setting_rows)
        )

    latest_filing = (
        await db.execute(
            select(DealerTaxFiling)
            .where(
                DealerTaxFiling.dealer_id == dealer_id,
                DealerTaxFiling.revenue_reported.is_not(None),
            )
            .order_by(DealerTaxFiling.year.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    return MetricInputs(
        period_rows=period_rows,
        periods=periods,
        addback_rows=addback_rows,
        target_rows=target_rows,
        targets=targets,
        fallbacks={
            "debt_service_observed_monthly": observed_ds_monthly or None,
            "debt_service_draft_monthly": draft_ds_monthly or None,
            "debt_schedule_monthly": drafted_monthly or None,
            "goal_monthly_payment": goal_payment,
            "tax_ebitda_annual": _tax_ebitda(latest_filing),
        },
    )


async def recompute_snapshot(db: AsyncSession, dealer_id: UUID) -> DealerMetricSnapshot:
    """Recompute and persist a new metric snapshot with full lineage + alerts.

    Flushes but does NOT commit — the caller owns the transaction boundary.
    """
    inputs = await load_metric_inputs(db, dealer_id)
    period_rows = inputs.period_rows
    # Only verified add-backs feed the persisted snapshot (and its lineage).
    addback_rows = [a for a in inputs.addback_rows if a.status == "verified"]
    target_rows = inputs.target_rows

    metrics = compute_metrics(
        inputs.periods,
        inputs.addbacks_annual_verified,
        inputs.targets,
        fallbacks=inputs.fallbacks,
    )

    snapshot = DealerMetricSnapshot(
        dealer_id=dealer_id,
        as_of=date.today(),
        metrics=metrics,
        score=metrics["score"],
        tier=metrics["tier"],
    )
    db.add(snapshot)
    await db.flush()  # snapshot.id needed for lineage edges

    # --- Lineage: which rows fed which metric ------------------------------
    for metric_key in ("ebitda", "dscr", "adb", "liquidity"):
        for p in period_rows:
            db.add(
                DealerMetricLineage(
                    snapshot_id=snapshot.id,
                    metric_key=metric_key,
                    ref_kind="period",
                    ref_id=p.id,
                    period=p.period,
                )
            )
    for a in addback_rows:
        db.add(
            DealerMetricLineage(
                snapshot_id=snapshot.id, metric_key="ebitda", ref_kind="addback", ref_id=a.id
            )
        )
    for t in target_rows:
        metric_key = _TARGET_LINEAGE.get(t.metric_key)
        if metric_key is not None:
            db.add(
                DealerMetricLineage(
                    snapshot_id=snapshot.id, metric_key=metric_key, ref_kind="target", ref_id=t.id
                )
            )

    # --- Floor alerts (skip when an unresolved alert of the kind exists) ---
    adb_current = metrics["adb"]["current"]
    adb_floor = metrics["adb"]["floor"]
    if adb_current is not None and adb_floor is not None and adb_current < adb_floor:
        await _ensure_alert(
            db,
            dealer_id,
            kind="adb_floor",
            severity="danger",
            message=(
                f"Average daily balance ${adb_current:,.0f} is below the ${adb_floor:,.0f} floor"
            ),
            ref_kind="snapshot",
            ref_id=snapshot.id,
        )
    liq_current = metrics["liquidity"]["current"]
    liq_floor = metrics["liquidity"]["floor"]
    if liq_current is not None and liq_floor is not None and liq_current < liq_floor:
        await _ensure_alert(
            db,
            dealer_id,
            kind="liquidity_floor",
            severity="danger",
            message=(
                f"Operating liquidity ${liq_current:,.0f} is below the ${liq_floor:,.0f} floor"
            ),
            ref_kind="snapshot",
            ref_id=snapshot.id,
        )
    nsf_count = metrics["nsf"]["count_6mo"]
    nsf_tolerance = metrics["nsf"]["tolerance"]
    if nsf_tolerance is not None and nsf_count > nsf_tolerance:
        await _ensure_alert(
            db,
            dealer_id,
            kind="nsf",
            severity="warn",
            message=(
                f"{nsf_count} NSF event(s) in the trailing 6 months exceeds the "
                f"tolerance of {int(nsf_tolerance)}"
            ),
            ref_kind="snapshot",
            ref_id=snapshot.id,
        )

    # --- Fundability alerts (Phase 3 Wave 2): a path at >= 90% readiness is a
    # positive, actionable signal — surface it once (deduped by kind via
    # _ensure_alert) so the team starts a funding file while the window is open.
    from .paths import compute_paths, merged_settings  # local import — paths is pure, no cycle risk

    program_rows = (await db.execute(select(DealerProgramSetting))).scalars().all()
    for path in compute_paths(metrics, inputs.targets, settings=merged_settings(program_rows)):
        readiness = float(path.get("readiness_pct") or 0.0)
        if readiness >= FUNDABILITY_READINESS_PCT:
            await _ensure_alert(
                db,
                dealer_id,
                kind=f"fundability_{path['key']}",
                severity="info",
                message=(
                    f"{path['label']} readiness {readiness:.0f}% — ready to start a funding file"
                ),
                ref_kind="path",
                ref_id=None,
            )

    await db.flush()
    return snapshot
