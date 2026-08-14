"""Forecast engine — Stream 4.

compute_forecast is a PURE, deterministic function (no IO) that projects the
latest Stream-3 metric snapshot 12 months forward twice:

* baseline  — status quo, a flat linear drift of +0.3%/month on
              score / dscr / ebitda_bankable / adb (liquidity held flat);
* adjusted  — baseline plus the modeled effect of every OPEN plan action
              (status != 'done'), ramped in linearly over RAMP_MONTHS months
              starting at the action's due month (or month 2 when no due_on).

MODELED_DELTAS are PROVISIONAL heuristics (category -> metric deltas), chosen
to mirror the prototype's advertised effects ("DSCR +0.09x", etc.). They are
not underwriting guidance; replace with fitted values when real outcome data
exists. Every modeling choice is echoed back in the 'assumptions' list so the
UI can render the forecast as explainable, not oracular.
"""

from __future__ import annotations

from datetime import date

# PROVISIONAL: modeled per-category effects of a completed plan action on the
# metric tree. Keys are DealerPlanAction.category values; inner keys are
# forecast series names ('ebitda' feeds the ebitda_bankable series).
MODELED_DELTAS: dict[str, dict[str, float]] = {
    "dscr": {"dscr": 0.09},
    "ebitda": {"ebitda": 137000, "dscr": 0.06},
    "liquidity": {"adb": 70000, "liquidity": 100000},
    "docs": {"score": 5},
}

HORIZON_MONTHS = 12
DRIFT_PER_MONTH = 0.003  # +0.3%/month, linear (not compounded)
RAMP_MONTHS = 3  # an action's effect phases in linearly over this many months
DEFAULT_START_MONTH = 2  # actions without a due_on start ramping at month 2

# series drifting in the baseline; 'liquidity' is held flat
_DRIFTING = ("score", "dscr", "ebitda_bankable", "adb")

# MODELED_DELTAS metric key -> forecast series key
_DELTA_SERIES = {
    "dscr": "dscr",
    "ebitda": "ebitda_bankable",
    "adb": "adb",
    "liquidity": "liquidity",
    "score": "score",
}

_ROUNDING = {"score": 1, "dscr": 3, "ebitda_bankable": 2, "adb": 2, "liquidity": 2}


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + (d.month - 1) + n, 12)
    return date(y, m + 1, 1)


def _month_index(start: date, due: date) -> int:
    """0-based horizon index of due's month, clamped below at 0 (overdue
    actions start immediately). May exceed the horizon — callers skip those."""
    return max(0, (due.year - start.year) * 12 + (due.month - start.month))


def _current_values(metrics: dict) -> dict[str, float | None]:
    def f(v) -> float | None:
        return float(v) if v is not None else None

    return {
        "score": f(metrics.get("score")),
        "dscr": f((metrics.get("dscr") or {}).get("current")),
        "ebitda_bankable": f((metrics.get("ebitda") or {}).get("bankable")),
        "adb": f((metrics.get("adb") or {}).get("current")),
        "liquidity": f((metrics.get("liquidity") or {}).get("current")),
    }


def compute_forecast(metrics: dict, plan_actions: list[dict]) -> dict:
    """Project the metric snapshot 12 months out, with and without the plan.

    ``metrics`` is a Stream-3 compute_metrics dict (snapshot.metrics).
    ``plan_actions`` is a list of dicts with at least category/status/due_on
    (due_on a date or None). Deterministic for a fixed today's date.
    """
    start = date.today().replace(day=1)
    months = [_add_months(start, i) for i in range(HORIZON_MONTHS)]
    current = _current_values(metrics)

    # --- baseline: flat linear drift on the drifting series ----------------
    baseline: dict[str, list[float | None]] = {}
    for key, value in current.items():
        if value is None:
            baseline[key] = [None] * HORIZON_MONTHS
        elif key in _DRIFTING:
            baseline[key] = [value * (1.0 + DRIFT_PER_MONTH * i) for i in range(HORIZON_MONTHS)]
        else:
            baseline[key] = [value] * HORIZON_MONTHS

    # --- adjusted: baseline + ramped deltas of open actions ----------------
    open_actions = [a for a in plan_actions if (a.get("status") or "todo") != "done"]
    adjusted: dict[str, list[float | None]] = {k: list(v) for k, v in baseline.items()}
    for action in open_actions:
        deltas = MODELED_DELTAS.get(action.get("category") or "")
        if not deltas:
            continue
        due = action.get("due_on")
        start_idx = _month_index(start, due) if due is not None else DEFAULT_START_MONTH
        if start_idx >= HORIZON_MONTHS:
            continue  # due beyond the horizon — no effect within 12 months
        for metric_key, delta in deltas.items():
            series = _DELTA_SERIES.get(metric_key)
            if series is None:
                continue
            for i in range(start_idx, HORIZON_MONTHS):
                if adjusted[series][i] is None:
                    continue  # no observed base value — nothing to adjust
                ramp = min(1.0, (i - start_idx + 1) / RAMP_MONTHS)
                adjusted[series][i] += delta * ramp

    # score is bounded 0-100 in both worlds
    for series_map in (baseline, adjusted):
        series_map["score"] = [
            None if v is None else min(100.0, max(0.0, v)) for v in series_map["score"]
        ]

    # --- fundable month: first month adjusted dscr & adb clear targets -----
    dscr_target = (metrics.get("dscr") or {}).get("target")
    adb_target = (metrics.get("adb") or {}).get("target")
    fundable_month: str | None = None
    if dscr_target is not None and adb_target is not None:
        for i in range(HORIZON_MONTHS):
            d, a = adjusted["dscr"][i], adjusted["adb"][i]
            if d is not None and a is not None and d >= float(dscr_target) and a >= float(adb_target):
                fundable_month = months[i].isoformat()
                break

    # --- uplift: plan vs status-quo bankable EBITDA at the horizon ---------
    base_end = baseline["ebitda_bankable"][-1]
    adj_end = adjusted["ebitda_bankable"][-1]
    uplift_pct: float | None = None
    if base_end is not None and adj_end is not None and base_end != 0:
        uplift_pct = round((adj_end / base_end - 1.0) * 100.0, 1)

    def _rounded(series_map: dict[str, list[float | None]]) -> dict[str, list[float | None]]:
        return {
            key: [None if v is None else round(v, _ROUNDING[key]) for v in vals]
            for key, vals in series_map.items()
        }

    assumptions = [
        f"Baseline drifts score/DSCR/bankable EBITDA/ADB linearly at +{DRIFT_PER_MONTH:.1%}/month; liquidity is held flat.",
        "Only OPEN plan actions (status != done) contribute modeled effects; completed actions are assumed already reflected in the snapshot.",
        f"Each action's effect ramps in linearly over {RAMP_MONTHS} months starting at its due month (month {DEFAULT_START_MONTH} when no due date is set).",
        "Modeled per-category deltas are PROVISIONAL heuristics, not underwriting guidance: "
        + "; ".join(
            f"{cat}: " + ", ".join(f"{m} {'+' if d >= 0 else ''}{d:g}" for m, d in deltas.items())
            for cat, deltas in MODELED_DELTAS.items()
        )
        + ".",
        "Fundable month is the first month the adjusted DSCR and ADB both clear their targets.",
        "Uplift is the horizon-end bankable EBITDA of the plan scenario vs the status quo.",
    ]

    return {
        "months": [m.isoformat() for m in months],
        "baseline": _rounded(baseline),
        "adjusted": _rounded(adjusted),
        "fundable_month": fundable_month,
        "uplift_pct": uplift_pct,
        "assumptions": assumptions,
    }
