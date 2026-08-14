"""Funding paths + credit ladder — Stream 4.

Pure, deterministic readers over a Stream-3 metrics dict and the dealer's
effective targets. compute_paths grades the dealer against 7 product paths;
compute_ladder places the dealer on the 5-rung credit ladder from the
prototype (Alternative -> Tier 3 -> Tier 2 -> Tier 1 Prime -> Expanded
Credit).

ALL thresholds here are PROVISIONAL product heuristics — placeholders shaped
like typical lender boxes, not actual credit policy. Replace with per-lender
programs when the lender network integration lands. Requirement builders are
shared across paths so a threshold change stays a one-line edit.
"""

from __future__ import annotations


def _f(v) -> float | None:
    return float(v) if v is not None else None


def _values(metrics: dict, targets: dict) -> dict:
    """Flatten the metric tree + effective targets into one lookup."""
    dscr = metrics.get("dscr") or {}
    adb = metrics.get("adb") or {}
    ebitda = metrics.get("ebitda") or {}
    liquidity = metrics.get("liquidity") or {}
    nsf = metrics.get("nsf") or {}
    return {
        "score": _f(metrics.get("score")),
        "dscr": _f(dscr.get("current")),
        "dscr_target": _f(targets.get("dscr_target")) or _f(dscr.get("target")),
        "dscr_floor": _f(targets.get("dscr_floor")) or _f(dscr.get("floor")),
        "adb": _f(adb.get("current")),
        "adb_target": _f(targets.get("adb_target")) or _f(adb.get("target")),
        "ebitda": _f(ebitda.get("bankable")),
        "liquidity": _f(liquidity.get("current")),
        "liquidity_floor": _f(targets.get("liquidity_operating_floor")) or _f(liquidity.get("floor")),
        "nsf": int(nsf.get("count_6mo") or 0),
        "periods_used": int(metrics.get("periods_used") or 0),
    }


# --- shared requirement builders (label, met, detail) ------------------------


def _req(label: str, met: bool, detail: str) -> dict:
    return {"label": label, "met": bool(met), "detail": detail}


def _req_dscr(v: dict, minimum: float) -> dict:
    cur = v["dscr"]
    met = cur is not None and cur >= minimum
    detail = f"DSCR {cur:.2f}x vs {minimum:.2f}x minimum" if cur is not None else "DSCR not yet computable"
    return _req(f"DSCR ≥ {minimum:.2f}x", met, detail)


def _req_ebitda(v: dict, minimum: float) -> dict:
    cur = v["ebitda"]
    met = cur is not None and cur >= minimum
    detail = (
        f"Bankable EBITDA ${cur:,.0f} vs ${minimum:,.0f} minimum"
        if cur is not None
        else "Bankable EBITDA not yet computable"
    )
    return _req(f"Bankable EBITDA ≥ ${minimum:,.0f}", met, detail)


def _req_adb(v: dict, minimum: float) -> dict:
    cur = v["adb"]
    met = cur is not None and cur >= minimum
    detail = (
        f"Average daily balance ${cur:,.0f} vs ${minimum:,.0f} minimum"
        if cur is not None
        else "No balance data yet"
    )
    return _req(f"Avg daily balance ≥ ${minimum:,.0f}", met, detail)


def _req_liquidity(v: dict, minimum: float) -> dict:
    cur = v["liquidity"]
    met = cur is not None and cur >= minimum
    detail = (
        f"Operating liquidity ${cur:,.0f} vs ${minimum:,.0f} minimum"
        if cur is not None
        else "No liquidity data yet"
    )
    return _req(f"Liquidity ≥ ${minimum:,.0f}", met, detail)


def _req_nsf(v: dict, tolerance: int) -> dict:
    cur = v["nsf"]
    met = cur <= tolerance
    return _req(
        f"NSF events ≤ {tolerance} (6 mo)", met, f"{cur} NSF event(s) in the trailing 6 months"
    )


def _req_history(v: dict, months: int) -> dict:
    cur = v["periods_used"]
    met = cur >= months
    return _req(
        f"≥ {months} months of financials", met, f"{cur} month(s) of normalized financials on file"
    )


def _req_score(v: dict, minimum: float) -> dict:
    cur = v["score"]
    met = cur is not None and cur >= minimum
    detail = f"Health score {cur:.1f} vs {minimum:.0f} minimum" if cur is not None else "No score yet"
    return _req(f"Health score ≥ {minimum:.0f}", met, detail)


# --- 7 funding paths (PROVISIONAL boxes) -------------------------------------

def compute_paths(metrics: dict, targets: dict) -> list[dict]:
    """Grade the dealer against 7 product paths. Readiness = % of requirements met."""
    v = _values(metrics, targets)
    dscr_target = v["dscr_target"] or 1.35

    specs: list[tuple[str, str, list[dict]]] = [
        (
            "sba",
            "SBA 7(a)",
            [
                _req_dscr(v, 1.25),
                _req_ebitda(v, 250_000),
                _req_nsf(v, 2),
                _req_liquidity(v, 50_000),
                _req_history(v, 6),
            ],
        ),
        (
            "conventional",
            "Conventional term loan",
            [
                _req_dscr(v, dscr_target),
                _req_ebitda(v, 500_000),
                _req_adb(v, v["adb_target"] or 500_000),
                _req_nsf(v, 0),
                _req_score(v, 85),
            ],
        ),
        (
            "loc",
            "Business line of credit",
            [
                _req_dscr(v, 1.20),
                _req_adb(v, 250_000),
                _req_nsf(v, 1),
                _req_liquidity(v, v["liquidity_floor"] or 100_000),
                _req_score(v, 75),
            ],
        ),
        (
            "cre",
            "Commercial real estate",
            [
                _req_dscr(v, 1.25),
                _req_ebitda(v, 750_000),
                _req_liquidity(v, 150_000),
                _req_score(v, 80),
                _req_history(v, 6),
            ],
        ),
        (
            "equipment",
            "Equipment financing",
            [
                _req_dscr(v, 1.15),
                _req_ebitda(v, 100_000),
                _req_adb(v, 100_000),
                _req_nsf(v, 3),
                _req_history(v, 3),
            ],
        ),
        (
            "working_capital",
            "Working capital",
            [
                _req_dscr(v, 1.05),
                _req_adb(v, 75_000),
                _req_nsf(v, 3),
                _req_liquidity(v, 25_000),
                _req_history(v, 3),
            ],
        ),
        (
            "floorplan",
            "Floorplan line",
            [
                _req_dscr(v, 1.20),
                _req_adb(v, 200_000),
                _req_nsf(v, 1),
                _req_liquidity(v, 100_000),
                _req_score(v, 70),
            ],
        ),
    ]

    out: list[dict] = []
    for key, label, requirements in specs:
        met = sum(1 for r in requirements if r["met"])
        out.append(
            {
                "key": key,
                "label": label,
                "readiness_pct": round(100.0 * met / len(requirements), 0),
                "requirements": requirements,
            }
        )
    return out


# --- 5-rung credit ladder (mirrors the prototype) ----------------------------

def compute_ladder(metrics: dict, targets: dict) -> dict:
    """Place the dealer on the Alternative -> Expanded Credit ladder.

    A rung is met when all its requirements are met; the current tier is the
    highest met rung (Alternative when nothing is met yet). Statuses:
    done (below current), current, next (immediately above), future.
    """
    v = _values(metrics, targets)
    dscr_target = v["dscr_target"] or 1.35
    dscr_floor = v["dscr_floor"] or 1.15
    adb_target = v["adb_target"] or 500_000
    liq_floor = v["liquidity_floor"] or 100_000

    tiers_spec: list[tuple[str, list[dict]]] = [
        ("Alternative", [_req_history(v, 1)]),
        ("Tier 3", [_req_dscr(v, 1.00), _req_nsf(v, 4), _req_history(v, 3)]),
        ("Tier 2", [_req_dscr(v, dscr_floor), _req_adb(v, 100_000), _req_nsf(v, 2)]),
        (
            "Tier 1 Prime",
            [
                _req_dscr(v, dscr_target),
                _req_adb(v, adb_target),
                _req_liquidity(v, liq_floor),
                _req_nsf(v, 0),
            ],
        ),
        (
            "Expanded Credit",
            [
                _req_dscr(v, dscr_target + 0.25),
                _req_adb(v, adb_target * 1.5),
                _req_score(v, 95),
                _req_nsf(v, 0),
            ],
        ),
    ]

    met_flags = [all(r["met"] for r in reqs) for _, reqs in tiers_spec]
    current_idx = max((i for i, m in enumerate(met_flags) if m), default=0)

    tiers: list[dict] = []
    for i, (name, reqs) in enumerate(tiers_spec):
        if i < current_idx:
            status = "done"
        elif i == current_idx:
            status = "current"
        elif i == current_idx + 1:
            status = "next"
        else:
            status = "future"
        tiers.append({"name": name, "requirements": reqs, "met": met_flags[i], "status": status})

    return {"current_tier": tiers_spec[current_idx][0], "tiers": tiers}
