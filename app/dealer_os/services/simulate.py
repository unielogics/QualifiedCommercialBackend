"""What-if simulator — pure lever application over metric-engine inputs.

apply_levers rewrites the trailing period dicts under the cash levers (ADB,
monthly debt service, monthly deposits, NSF) without touching the originals.
The EBITDA lever deliberately does NOT appear here: annual EBITDA deltas —
including "verify every candidate add-back" — enter compute_metrics through
addbacks_annual_verified, exactly the channel verified add-backs use in
production, so a scenario can never disagree with what verifying would
actually do to the bankable number.

Everything in this module is pure and IO-free; the router owns loading (it
reuses engines.load_metric_inputs, the same loader the snapshot engine uses)
and persists NOTHING — simulation is read-only by contract.
"""

from __future__ import annotations

from typing import Iterable

# Periods fields the ADB lever moves together: holding more cash all month
# lifts the average, the month-end print and the intramonth low alike.
_BALANCE_FIELDS = ("avg_daily_balance", "ending_balance", "low_balance")


def _shift(value: float | None, delta: float) -> float | None:
    """value + delta floored at zero; None stays None — a lever must never
    fabricate an observation the statements do not carry."""
    if value is None:
        return None
    return max(0.0, float(value) + delta)


def apply_levers(
    periods: list[dict],
    *,
    adb_delta: float = 0.0,
    debt_service_monthly_delta: float = 0.0,
    deposits_monthly_delta: float = 0.0,
    nsf_zero: bool = False,
) -> list[dict]:
    """Return NEW period dicts with the levers applied (inputs untouched).

    All-zero levers return dicts equal to the inputs, so a no-op scenario
    reproduces the baseline exactly. Deltas apply only to observed (non-None)
    fields and clamp at >= 0.
    """
    out: list[dict] = []
    for p in periods:
        q = dict(p)
        if adb_delta:
            for field in _BALANCE_FIELDS:
                q[field] = _shift(q.get(field), adb_delta)
        if debt_service_monthly_delta:
            q["debt_service"] = _shift(q.get("debt_service"), debt_service_monthly_delta)
        if deposits_monthly_delta:
            q["deposits"] = _shift(q.get("deposits"), deposits_monthly_delta)
        if nsf_zero:
            q["nsf_count"] = 0
        out.append(q)
    return out


def adjusted_fallbacks(
    fallbacks: dict | None, debt_service_monthly_delta: float = 0.0
) -> dict:
    """Mirror the debt-service lever into the debt-schedule fallback.

    A statements-only dealer has debt_service None on every period, so the
    engine reads dos_debts via fallbacks['debt_schedule_monthly'] — without
    this, the lever would silently do nothing for exactly those dealers. The
    fallback is only consulted when NO period carries observed debt service,
    so shifting both can never double-count."""
    out = dict(fallbacks or {})
    drafted = out.get("debt_schedule_monthly")
    if debt_service_monthly_delta and drafted is not None:
        out["debt_schedule_monthly"] = max(0.0, float(drafted) + debt_service_monthly_delta)
    return out


def unverified_addbacks_annual(addback_rows: Iterable) -> float:
    """Annual EBITDA lift if every open add-back were verified.

    Counts rows that are neither verified (already in the baseline pool) nor
    excluded (a human said no) — i.e. candidate/review. annual_amount wins;
    a row carrying only monthly_amount contributes monthly * 12."""
    total = 0.0
    for a in addback_rows:
        if getattr(a, "status", None) in ("verified", "excluded"):
            continue
        annual = getattr(a, "annual_amount", None)
        monthly = getattr(a, "monthly_amount", None)
        if annual is not None:
            total += float(annual)
        elif monthly is not None:
            total += float(monthly) * 12.0
    return round(total, 2)


def proposed_shifts_adb(shifts: Iterable) -> float:
    """Summed est_adb_impact of proposed AND done payment shifts — a change
    the client has executed keeps counting until real statements absorb it;
    declined/dismissed rows drop out immediately."""
    return round(
        sum(
            float(s.est_adb_impact)
            for s in shifts
            if getattr(s, "status", None) in ("proposed", "done")
            and getattr(s, "est_adb_impact", None) is not None
        ),
        2,
    )


def summarize(metrics: dict) -> dict:
    """Metric tree -> the flat scalar block the simulate contract exposes."""
    return {
        "score": metrics.get("score"),
        "dscr": (metrics.get("dscr") or {}).get("current"),
        "ebitda_bankable": (metrics.get("ebitda") or {}).get("bankable"),
        "adb": (metrics.get("adb") or {}).get("current"),
        "liquidity": (metrics.get("liquidity") or {}).get("current"),
        "nsf_6mo": int((metrics.get("nsf") or {}).get("count_6mo") or 0),
    }


# --- daily balance curve (the ADB visual) ------------------------------------

_CURVE_DAYS = 31


def daily_curve(
    events,
    baseline_adb: float | None,
    scenario_adb: float | None,
    shifts: list | None = None,
) -> dict | None:
    """Typical intra-month balance curve, baseline vs scenario. PURE.

    Shape: average net flow per day-of-month across the observed months,
    cumulated into a balance path. Each curve is then ANCHORED so its mean
    equals the engine's ADB for that side — the picture and the number can
    never disagree. The scenario side additionally shows WHERE the lift
    lands: each applied payment shift keeps its amount in the account from
    its old payment day until its new one (days from_day..to_day-1 higher
    by the amount; reversed sign when moved earlier).

    Returns None when there is no flow history or no baseline ADB to anchor
    to (a curve invented from nothing would be decoration, not information).
    """
    if baseline_adb is None or scenario_adb is None:
        return None
    per_month: dict[tuple[int, int], dict[int, float]] = {}
    for r in events:
        day = min(_CURVE_DAYS, max(1, r.occurred_on.day))
        month = (r.occurred_on.year, r.occurred_on.month)
        per_month.setdefault(month, {})
        per_month[month][day] = per_month[month].get(day, 0.0) + float(r.amount or 0.0)
    if not per_month:
        return None

    n_months = len(per_month)
    avg_net = [0.0] * (_CURVE_DAYS + 1)  # 1-indexed
    for flows in per_month.values():
        for day, net in flows.items():
            avg_net[day] += net / n_months

    shape = [0.0] * (_CURVE_DAYS + 1)
    run = 0.0
    for day in range(1, _CURVE_DAYS + 1):
        run += avg_net[day]
        shape[day] = run

    def anchored(curve: list[float], target_mean: float) -> list[float]:
        mean = sum(curve[1:]) / _CURVE_DAYS
        offset = target_mean - mean
        return [round(v + offset, 2) for v in curve]

    baseline_curve = anchored(shape, float(baseline_adb))

    scenario_shape = list(shape)
    for sh in shifts or []:
        amount = float(getattr(sh, "monthly_amount", None) or 0.0)
        a = int(getattr(sh, "from_day", 0) or 0)
        b = int(getattr(sh, "to_day", 0) or 0)
        if amount <= 0 or a == b or not (1 <= a <= _CURVE_DAYS and 1 <= b <= _CURVE_DAYS):
            continue
        # Direction-aware lift (0121): an outflow paid LATER is held over
        # from_day..to_day-1 (+); an inflow collected EARLIER arrives over
        # to_day..from_day-1 (+). The mismatched pairs subtract.
        direction = getattr(sh, "direction", "out") or "out"
        moved_later = b > a
        sign = 1.0 if (moved_later == (direction == "out")) else -1.0
        for day in range(min(a, b), max(a, b)):
            scenario_shape[day] += sign * amount
    scenario_curve = anchored(scenario_shape, float(scenario_adb))

    return {
        "days": [
            {"day": d, "baseline": baseline_curve[d], "scenario": scenario_curve[d]}
            for d in range(1, _CURVE_DAYS + 1)
        ],
        "adb_baseline": round(float(baseline_adb), 2),
        "adb_scenario": round(float(scenario_adb), 2),
    }
