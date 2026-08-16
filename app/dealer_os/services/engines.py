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

from datetime import date
from typing import Iterable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    DealerAddback,
    DealerAlert,
    DealerFinancialPeriod,
    DealerMetricLineage,
    DealerMetricSnapshot,
    DealerMetricTarget,
    DealerProgramSetting,
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
    monthly_ds = _avg(p.get("debt_service") for p in periods)
    ds_source = "periods"
    if monthly_ds is None:
        drafted = fallbacks.get("debt_schedule_monthly")
        if drafted is not None and float(drafted) > 0:
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


async def recompute_snapshot(db: AsyncSession, dealer_id: UUID) -> DealerMetricSnapshot:
    """Recompute and persist a new metric snapshot with full lineage + alerts.

    Flushes but does NOT commit — the caller owns the transaction boundary.
    """
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
    from ..models import DealerAccount

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
    for row in raw_rows:
        by_month.setdefault(row.period, []).append(row)
    period_rows = []
    for month in sorted(by_month, reverse=True)[:6]:
        rows = sorted(by_month[month], key=_rank)
        merged = rows[0]
        if len(rows) > 1:
            import types

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
                select(DealerAddback).where(
                    DealerAddback.dealer_id == dealer_id, DealerAddback.status == "verified"
                )
            )
        )
        .scalars()
        .all()
    )
    addbacks_annual_verified = sum(
        float(a.annual_amount) for a in addback_rows if a.annual_amount is not None
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
            "ebitda_reported": _f(p.ebitda_reported),
            "debt_service": _f(p.debt_service),
            "avg_daily_balance": _f(p.avg_daily_balance),
            "ending_balance": _f(p.ending_balance),
            "low_balance": _f(p.low_balance),
            "nsf_count": int(p.nsf_count or 0),
            "deposits": _f(p.deposits),
        }
        for p in period_rows
    ]

    # Fallbacks for figures bank statements cannot carry. Observed values
    # always win; these only fill a gap that would otherwise leave the metric
    # permanently null.
    from ..models import DealerDebt, DealerTaxFiling

    drafted_monthly = float(
        (
            await db.execute(
                select(func.coalesce(func.sum(DealerDebt.monthly_payment), 0)).where(
                    DealerDebt.dealer_id == dealer_id, DealerDebt.status == "active"
                )
            )
        ).scalar_one()
        or 0
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

    metrics = compute_metrics(
        periods,
        addbacks_annual_verified,
        targets,
        fallbacks={
            "debt_schedule_monthly": drafted_monthly or None,
            "tax_ebitda_annual": _tax_ebitda(latest_filing),
        },
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
    for path in compute_paths(metrics, targets, settings=merged_settings(program_rows)):
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
