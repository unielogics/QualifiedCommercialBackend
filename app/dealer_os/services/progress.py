"""Snapshot-over-time progress diff — Phase 3 Wave 2.

compute_progress is a PURE function (no IO, unit-testable) comparing two
metric snapshots roughly a month apart: the four headline metrics plus the
health score become from/to/delta triples and deterministic improved/slipped
strings ("DSCR +0.09x", "Bankable EBITDA -$12,000"). The router picks the
snapshot pair (latest vs the closest one >= 21 days older, falling back to
the two latest) and fills actions_completed from the plan table.
"""

from __future__ import annotations

from datetime import date
from typing import Any

# metric key -> (path into the snapshot metrics dict, human label, format kind)
_METRICS: list[tuple[str, tuple[str, str], str, str]] = [
    ("ebitda_bankable", ("ebitda", "bankable"), "Bankable EBITDA", "money"),
    ("dscr", ("dscr", "current"), "DSCR", "ratio"),
    ("adb", ("adb", "current"), "Average daily balance", "money"),
    ("liquidity", ("liquidity", "current"), "Operating liquidity", "money"),
]

# Deltas smaller than this (per format kind) are noise, not movement.
_EPSILON = {"money": 0.5, "ratio": 0.0005, "score": 0.05}


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _dig(metrics: dict, path: tuple[str, str]) -> float | None:
    family = metrics.get(path[0]) or {}
    return _f(family.get(path[1]) if isinstance(family, dict) else None)


def _fmt_delta(delta: float, kind: str) -> str:
    sign = "+" if delta >= 0 else "-"
    mag = abs(delta)
    if kind == "money":
        return f"{sign}${mag:,.0f}"
    if kind == "ratio":
        return f"{sign}{mag:.2f}x"
    return f"{sign}{mag:.1f}"  # score


def compute_progress(
    from_date: date,
    to_date: date,
    from_metrics: dict,
    to_metrics: dict,
    score_from: float | None,
    score_to: float | None,
) -> dict:
    """Deterministic diff of two snapshot metric dicts.

    Returns {from_date, to_date, score_from, score_to,
             deltas: {key: {from, to, delta}}, improved: [str], slipped: [str]}
    (dates ISO-formatted; actions_completed is the caller's to fill).
    """
    deltas: dict[str, dict[str, float | None]] = {}
    improved: list[str] = []
    slipped: list[str] = []

    def _consider(label: str, kind: str, v_from: float | None, v_to: float | None) -> float | None:
        if v_from is None or v_to is None:
            return None
        ndigits = 3 if kind == "ratio" else 1 if kind == "score" else 2
        delta = round(v_to - v_from, ndigits)
        if abs(delta) > _EPSILON[kind]:
            line = f"{label} {_fmt_delta(delta, kind)}"
            (improved if delta > 0 else slipped).append(line)
        return delta

    for key, path, label, kind in _METRICS:
        v_from, v_to = _dig(from_metrics, path), _dig(to_metrics, path)
        deltas[key] = {
            "from": v_from,
            "to": v_to,
            "delta": _consider(label, kind, v_from, v_to),
        }

    _consider("Health score", "score", _f(score_from), _f(score_to))

    return {
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "score_from": _f(score_from),
        "score_to": _f(score_to),
        "deltas": deltas,
        "improved": improved,
        "slipped": slipped,
    }
