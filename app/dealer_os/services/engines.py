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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    DealerAddback,
    DealerAlert,
    DealerFinancialPeriod,
    DealerMetricLineage,
    DealerMetricSnapshot,
    DealerMetricTarget,
)

# 4% lender haircut: bankable EBITDA = adjusted * 0.96
BANKABLE_FACTOR = 0.96

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


def compute_metrics(
    periods: list[dict],
    addbacks_annual_verified: float,
    targets: dict[str, float | None],
) -> dict:
    """Pure metric engine over up to 6 trailing monthly periods.

    ``periods`` is most-recent-first; each dict carries ebitda_reported,
    debt_service, avg_daily_balance, ending_balance, low_balance, nsf_count,
    deposits (floats or None). Deterministic — same inputs, same output.
    """
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
    ebitda_adjusted = (
        _round2(ebitda_reported_ttm + float(addbacks_annual_verified or 0.0))
        if ebitda_reported_ttm is not None
        else None
    )
    ebitda_bankable = _round2(ebitda_adjusted * BANKABLE_FACTOR) if ebitda_adjusted is not None else None

    # --- DSCR --------------------------------------------------------------
    monthly_ds = _avg(p.get("debt_service") for p in periods)
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
            "reported_ttm": ebitda_reported_ttm,
            "adjusted": ebitda_adjusted,
            "bankable": ebitda_bankable,
            "target": ebitda_target,
            "gap": _gap(ebitda_target, ebitda_bankable),
        },
        "dscr": {
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
    period_rows = (
        (
            await db.execute(
                select(DealerFinancialPeriod)
                .where(DealerFinancialPeriod.dealer_id == dealer_id)
                .order_by(DealerFinancialPeriod.period.desc())
                .limit(6)
            )
        )
        .scalars()
        .all()
    )
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

    metrics = compute_metrics(periods, addbacks_annual_verified, targets)

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

    await db.flush()
    return snapshot
