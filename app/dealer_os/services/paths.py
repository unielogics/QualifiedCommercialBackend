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
                # 0119: additive program sizing (PROVISIONAL — see the sizing
                # block below). Callers with deposit history inject
                # metrics["deposits_monthly_avg"] before calling.
                **size_program(key, metrics, targets),
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


# --- Program sizing + goal inversion (0119) -----------------------------------
#
# PROVISIONAL — pending lending-desk sign-off. Every DSCR band, term, rate,
# deposit multiple and ceiling below is a product placeholder in the same
# spirit as the path boxes above — shaped like typical lender programs, NOT
# credit policy. Replace with per-lender programs when the network lands.

PATH_KEYS: tuple[str, ...] = (
    "sba",
    "conventional",
    "loc",
    "cre",
    "equipment",
    "working_capital",
    "floorplan",
)

# DSCR-capacity paths: principal is what the bankable-EBITDA-per-month can
# carry at a DSCR floor, amortized. Triplets run conservative -> typical ->
# aggressive, so amounts come out min <= typical <= max by construction.
_SIZING_DSCR: dict[str, dict] = {
    "sba": {
        "dscr": (1.50, 1.35, 1.25),
        "term_months": (84, 120, 120),
        "annual_rate": 0.105,
        "ceiling": 5_000_000,  # SBA 7(a) statutory program maximum
    },
    "conventional": {
        "dscr": (1.60, 1.40, 1.25),
        "term_months": (48, 60, 84),
        "annual_rate": 0.095,
        "ceiling": 10_000_000,
    },
    "equipment": {
        "dscr": (1.40, 1.25, 1.15),
        "term_months": (36, 48, 60),
        "annual_rate": 0.115,
        "ceiling": 2_000_000,
    },
}

# Deposit-multiple paths: revenue-based sizing off observed bank activity.
_SIZING_DEPOSIT: dict[str, dict] = {
    # ~0.6-1.2x average monthly deposits.
    "working_capital": {"input": "deposits_monthly_avg", "multiples": (0.6, 0.9, 1.2), "ceiling": 500_000},
    # ~1-2x average daily balance.
    "loc": {"input": "adb", "multiples": (1.0, 1.5, 2.0), "ceiling": 1_000_000},
}

# Collateral paths cannot be sized from bank/EBITDA data alone — name what is
# missing rather than inventing a number.
_SIZING_COLLATERAL_MISSING: dict[str, list[str]] = {
    "cre": [
        "property value or purchase price not on file",
        "appraisal / NOI not available",
    ],
    "floorplan": [
        "inventory schedule not on file",
        "advance-rate and curtailment terms unknown",
    ],
}

# Months of normalized statements a lender file needs per program (mirrors the
# _req_history thresholds in the path boxes above).
_STATEMENT_MONTHS_REQUIRED: dict[str, int] = {
    "sba": 6,
    "conventional": 6,
    "loc": 3,
    "equipment": 3,
    "working_capital": 3,
}

# Relative tolerance when checking a requirement against the current value —
# absorbs the round-to-$1k on sized amounts so a goal equal to the computed
# capacity round-trips to met==True.
_REQ_EPSILON = 1e-3


def _principal_for_payment(payment: float, annual_rate: float, months: int) -> float:
    """Loan size a monthly P&I supports. READ-ONLY reuse of the intake
    pipeline's amortization inverse (function-level import — the same posture
    services/handoff.py uses for dealer_ai_intake helpers)."""
    from app.routers.dealer_ai_intake import _dscr_principal_for_payment  # noqa: PLC0415

    return _dscr_principal_for_payment(payment, annual_rate, months)


def _payment_for_principal(principal: float, annual_rate: float, months: int) -> float:
    """Monthly P&I for a principal — derived from the same imported inverse so
    the two directions can never drift apart."""
    return principal / _principal_for_payment(1.0, annual_rate, months)


def monthly_payment_for_goal(goal: float, path_key: str = "conventional") -> float | None:
    """Typical-case monthly P&I a funding goal implies (used by the target
    proposal service to align ADB/liquidity proposals to the goal)."""
    spec = _SIZING_DSCR.get(path_key)
    if spec is None or goal is None or goal <= 0:
        return None
    return round(_payment_for_principal(float(goal), spec["annual_rate"], spec["term_months"][1]), 2)


def _insufficient(constraints: list[str]) -> dict:
    return {
        "funding_min": None,
        "funding_typical": None,
        "funding_max": None,
        "sizing_basis": "insufficient data",
        "sizing_constraints": constraints,
    }


def _floor_1k(x: float) -> float:
    """Round sized amounts DOWN to the $1k grain — a displayed capacity must
    always round-trip as a feasible goal (rounding up would contradict
    requirements_for_amount at small capacities)."""
    return float(int(x // 1000) * 1000)


def size_program(path_key: str, metrics: dict, targets: dict) -> dict:
    """PURE program sizing: what this dealer could fund on each path today.

    Returns {funding_min, funding_typical, funding_max, sizing_basis,
    sizing_constraints}. Amounts are None when the path cannot be sized from
    the data on file; sizing_basis says which model produced the numbers.

    metrics is the snapshot metric tree; callers that have it may inject
    metrics["deposits_monthly_avg"] (average monthly deposits) — without it
    the deposit-multiple path reports insufficient data instead of guessing.
    """
    v = _values(metrics, targets)

    if path_key in _SIZING_DSCR:
        spec = _SIZING_DSCR[path_key]
        ebitda_annual = v["ebitda"]
        if ebitda_annual is None or ebitda_annual <= 0:
            return _insufficient(
                ["bankable EBITDA not yet computable — upload a P&L or business tax return"]
            )
        monthly_ebitda = ebitda_annual / 12.0
        amounts: list[float] = []
        for dscr, term in zip(spec["dscr"], spec["term_months"]):
            payment = monthly_ebitda / dscr
            amounts.append(_principal_for_payment(payment, spec["annual_rate"], term))
        constraints = [
            (
                f"Assumes {spec['term_months'][1]}-month amortization at "
                f"{spec['annual_rate']:.1%}, {spec['dscr'][1]:.2f}x DSCR (typical case)"
            )
        ]
        if any(a > spec["ceiling"] for a in amounts):
            constraints.append(f"Capped at the ${spec['ceiling']:,.0f} program ceiling")
        lo, mid, hi = (min(a, spec["ceiling"]) for a in amounts)
        return {
            "funding_min": _floor_1k(lo),
            "funding_typical": _floor_1k(mid),
            "funding_max": _floor_1k(hi),
            "sizing_basis": "dscr_capacity",
            "sizing_constraints": constraints,
        }

    if path_key in _SIZING_DEPOSIT:
        spec = _SIZING_DEPOSIT[path_key]
        if spec["input"] == "adb":
            base = v["adb"]
            missing = "no balance data yet — average daily balance is not computable"
            basis = "adb_multiple"
            base_label = "average daily balance"
        else:
            base = _f(metrics.get("deposits_monthly_avg"))
            missing = "no deposit history on file yet"
            basis = "deposit_multiple"
            base_label = "average monthly deposits"
        if base is None or base <= 0:
            return _insufficient([missing])
        m_lo, m_mid, m_hi = spec["multiples"]
        amounts = [base * m for m in (m_lo, m_mid, m_hi)]
        constraints = [f"{m_lo:g}-{m_hi:g}x {base_label} (${base:,.0f})"]
        if any(a > spec["ceiling"] for a in amounts):
            constraints.append(f"Capped at the ${spec['ceiling']:,.0f} program ceiling")
        lo, mid, hi = (min(a, spec["ceiling"]) for a in amounts)
        return {
            "funding_min": _floor_1k(lo),
            "funding_typical": _floor_1k(mid),
            "funding_max": _floor_1k(hi),
            "sizing_basis": basis,
            "sizing_constraints": constraints,
        }

    if path_key in _SIZING_COLLATERAL_MISSING:
        return _insufficient(list(_SIZING_COLLATERAL_MISSING[path_key]))

    return _insufficient([f"no sizing model for path '{path_key}'"])


def _requirement(
    metric_key: str, label: str, required: float | None, current: float | None
) -> dict:
    met = (
        required is not None
        and current is not None
        and current >= required * (1 - _REQ_EPSILON) - 0.01
    )
    if required is None:
        gap = None
    elif met or current is None:
        gap = 0.0 if met else round(required, 2)
    else:
        gap = round(max(0.0, required - current), 2)
    return {
        "metric_key": metric_key,
        "label": label,
        "required_value": round(required, 2) if required is not None else None,
        "current_value": round(current, 2) if current is not None else None,
        "gap": gap,
        "met": bool(met),
    }


def requirements_for_amount(path_key: str, goal: float, metrics: dict) -> list[dict]:
    """PURE inversion of size_program: what the metrics would have to be for
    ``goal`` to be fundable on this path (typical-case assumptions).

    Returns [{metric_key, label, required_value, current_value, gap, met}].
    Empty for collateral paths (nothing to invert without collateral inputs)
    and for a missing/non-positive goal.
    """
    if goal is None or goal <= 0:
        return []
    goal = float(goal)
    periods_used = int(metrics.get("periods_used") or 0)
    rows: list[dict] = []

    def months_row() -> dict:
        required = _STATEMENT_MONTHS_REQUIRED.get(path_key, 3)
        return _requirement(
            "months_statements",
            f"Months of statements on file ≥ {required}",
            float(required),
            float(periods_used),
        )

    def ceiling_row(ceiling: float) -> dict:
        # Inverted sense: met while the GOAL stays at or under the ceiling.
        return {
            "metric_key": "program_ceiling",
            "label": f"Goal within the ${ceiling:,.0f} program ceiling",
            "required_value": round(ceiling, 2),
            "current_value": round(goal, 2),
            "gap": round(max(0.0, goal - ceiling), 2),
            "met": goal <= ceiling * (1 + _REQ_EPSILON),
        }

    if path_key in _SIZING_DSCR:
        spec = _SIZING_DSCR[path_key]
        rate, term, dscr = spec["annual_rate"], spec["term_months"][1], spec["dscr"][1]
        payment = _payment_for_principal(goal, rate, term)
        required_ebitda_annual = payment * dscr * 12.0
        ebitda = metrics.get("ebitda") or {}
        rows.append(
            _requirement(
                "ebitda_bankable",
                f"Bankable EBITDA supporting ~${payment:,.0f}/mo at {dscr:.2f}x DSCR",
                required_ebitda_annual,
                _f(ebitda.get("bankable")),
            )
        )
        rows.append(months_row())
        rows.append(ceiling_row(spec["ceiling"]))
        return rows

    if path_key in _SIZING_DEPOSIT:
        spec = _SIZING_DEPOSIT[path_key]
        typical_multiple = spec["multiples"][1]
        required_base = goal / typical_multiple
        if spec["input"] == "adb":
            adb = metrics.get("adb") or {}
            rows.append(
                _requirement(
                    "adb",
                    f"Average daily balance ≥ ${required_base:,.0f} ({typical_multiple:g}x sizing)",
                    required_base,
                    _f(adb.get("current")),
                )
            )
        else:
            rows.append(
                _requirement(
                    "deposits_monthly_avg",
                    f"Average monthly deposits ≥ ${required_base:,.0f} ({typical_multiple:g}x sizing)",
                    required_base,
                    _f(metrics.get("deposits_monthly_avg")),
                )
            )
        rows.append(months_row())
        rows.append(ceiling_row(spec["ceiling"]))
        return rows

    return []
