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


# --- 7 funding paths (PROVISIONAL boxes, data-driven since 0120) --------------
#
# 0120: the inline path specs became DEFAULT_REQUIREMENTS / DEFAULT_SIZING data
# tables so the lending desk can override any program (dos_program_settings)
# without a deploy. A stored override replaces the default WHOLESALE per path
# (never a deep merge); absent row = these code defaults, unchanged behavior.

PATH_KEYS: tuple[str, ...] = (
    "sba",
    "conventional",
    "loc",
    "cre",
    "equipment",
    "working_capital",
    "floorplan",
)

PATH_LABELS: dict[str, str] = {
    "sba": "SBA 7(a)",
    "conventional": "Conventional term loan",
    "loc": "Business line of credit",
    "cre": "Commercial real estate",
    "equipment": "Equipment financing",
    "working_capital": "Working capital",
    "floorplan": "Floorplan line",
}

# ReqSpec kind -> builder. nsf/history thresholds are integer-valued.
_BUILDERS = {
    "dscr": _req_dscr,
    "ebitda": _req_ebitda,
    "adb": _req_adb,
    "nsf": _req_nsf,
    "liquidity": _req_liquidity,
    "score": _req_score,
    "history": _req_history,
}
_INT_KINDS = frozenset({"nsf", "history"})
_TARGET_KEYS = frozenset({"dscr_target", "adb_target", "liquidity_floor"})

# Default ReqSpecs per path: {kind, threshold, target_key?}. A spec carrying a
# target_key resolves its threshold from the dealer's effective targets first,
# with the literal threshold as the fallback — the same "v[key] or fallback"
# the inline specs used before 0120.
DEFAULT_REQUIREMENTS: dict[str, list[dict]] = {
    "sba": [
        {"kind": "dscr", "threshold": 1.25},
        {"kind": "ebitda", "threshold": 250_000},
        {"kind": "nsf", "threshold": 2},
        {"kind": "liquidity", "threshold": 50_000},
        {"kind": "history", "threshold": 6},
    ],
    "conventional": [
        {"kind": "dscr", "threshold": 1.35, "target_key": "dscr_target"},
        {"kind": "ebitda", "threshold": 500_000},
        {"kind": "adb", "threshold": 500_000, "target_key": "adb_target"},
        {"kind": "nsf", "threshold": 0},
        {"kind": "score", "threshold": 85},
    ],
    "loc": [
        {"kind": "dscr", "threshold": 1.20},
        {"kind": "adb", "threshold": 250_000},
        {"kind": "nsf", "threshold": 1},
        {"kind": "liquidity", "threshold": 100_000, "target_key": "liquidity_floor"},
        {"kind": "score", "threshold": 75},
    ],
    "cre": [
        {"kind": "dscr", "threshold": 1.25},
        {"kind": "ebitda", "threshold": 750_000},
        {"kind": "liquidity", "threshold": 150_000},
        {"kind": "score", "threshold": 80},
        {"kind": "history", "threshold": 6},
    ],
    "equipment": [
        {"kind": "dscr", "threshold": 1.15},
        {"kind": "ebitda", "threshold": 100_000},
        {"kind": "adb", "threshold": 100_000},
        {"kind": "nsf", "threshold": 3},
        {"kind": "history", "threshold": 3},
    ],
    "working_capital": [
        {"kind": "dscr", "threshold": 1.05},
        {"kind": "adb", "threshold": 75_000},
        {"kind": "nsf", "threshold": 3},
        {"kind": "liquidity", "threshold": 25_000},
        {"kind": "history", "threshold": 3},
    ],
    "floorplan": [
        {"kind": "dscr", "threshold": 1.20},
        {"kind": "adb", "threshold": 200_000},
        {"kind": "nsf", "threshold": 1},
        {"kind": "liquidity", "threshold": 100_000},
        {"kind": "score", "threshold": 70},
    ],
}


def _build_requirement(v: dict, spec: dict) -> dict | None:
    """One ReqSpec -> the graded {label, met, detail} row (None = unknown kind
    in a stored override — skipped rather than crashing a live console)."""
    kind = spec.get("kind")
    builder = _BUILDERS.get(kind)
    if builder is None:
        return None
    threshold = spec.get("threshold")
    target_key = spec.get("target_key")
    if target_key in _TARGET_KEYS:
        threshold = v.get(target_key) or threshold
    if threshold is None:
        return None
    return builder(v, int(threshold) if kind in _INT_KINDS else float(threshold))


def compute_paths(metrics: dict, targets: dict, settings: dict | None = None) -> list[dict]:
    """Grade the dealer against the 7 product paths. Readiness = % of
    requirements met. settings is a merged_settings() dict (None = the code
    defaults); each path's requirement list and sizing come from it wholesale."""
    v = _values(metrics, targets)
    out: list[dict] = []
    for key in PATH_KEYS:
        specs = _requirement_specs(key, settings)
        requirements = [r for r in (_build_requirement(v, s) for s in specs) if r is not None]
        met = sum(1 for r in requirements if r["met"])
        out.append(
            {
                "key": key,
                "label": PATH_LABELS[key],
                "readiness_pct": round(100.0 * met / max(len(requirements), 1), 0),
                "requirements": requirements,
                # 0119: additive program sizing (PROVISIONAL — see the sizing
                # block below). Callers with deposit history inject
                # metrics["deposits_monthly_avg"] before calling.
                **size_program(key, metrics, targets, settings=settings),
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

# Default sizing model per path (0120: JSON-shaped so a stored override is the
# same object shape).
# - dscr model: principal is what the bankable-EBITDA-per-month can carry at a
#   DSCR floor, amortized. Triplets run conservative -> typical -> aggressive,
#   so amounts come out min <= typical <= max by construction.
# - deposit model: revenue-based sizing off observed bank activity.
DEFAULT_SIZING: dict[str, dict] = {
    "sba": {
        "model": "dscr",
        "dscr": [1.50, 1.35, 1.25],
        "term_months": [84, 120, 120],
        "annual_rate": 0.105,
        "ceiling": 5_000_000,  # SBA 7(a) statutory program maximum
    },
    "conventional": {
        "model": "dscr",
        "dscr": [1.60, 1.40, 1.25],
        "term_months": [48, 60, 84],
        "annual_rate": 0.095,
        "ceiling": 10_000_000,
    },
    "equipment": {
        "model": "dscr",
        "dscr": [1.40, 1.25, 1.15],
        "term_months": [36, 48, 60],
        "annual_rate": 0.115,
        "ceiling": 2_000_000,
    },
    # ~0.6-1.2x average monthly deposits.
    "working_capital": {
        "model": "deposit",
        "input": "deposits_monthly_avg",
        "multiples": [0.6, 0.9, 1.2],
        "ceiling": 500_000,
    },
    # ~1-2x average daily balance.
    "loc": {"model": "deposit", "input": "adb", "multiples": [1.0, 1.5, 2.0], "ceiling": 1_000_000},
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


def path_model(path_key: str) -> str:
    """dscr | deposit | collateral — which sizing family a path belongs to."""
    spec = DEFAULT_SIZING.get(path_key)
    return spec["model"] if spec is not None else "collateral"


def merged_settings(rows) -> dict[str, dict]:
    """PURE: dos_program_settings rows -> {path_key: {sizing, requirements}}.

    A stored override replaces the code default WHOLESALE for that field of
    that path (no deep merge); paths without a row (or with a null field) keep
    the defaults. Rows may be ORM objects or plain dicts."""

    def _get(row, key):
        return row.get(key) if isinstance(row, dict) else getattr(row, key, None)

    out = {
        key: {
            "sizing": DEFAULT_SIZING.get(key),
            "requirements": DEFAULT_REQUIREMENTS.get(key, []),
        }
        for key in PATH_KEYS
    }
    for row in rows or ():
        entry = out.get(_get(row, "path_key"))
        if entry is None:
            continue
        sizing = _get(row, "sizing")
        requirements = _get(row, "requirements")
        if sizing is not None:
            entry["sizing"] = sizing
        if requirements is not None:
            entry["requirements"] = requirements
    return out


def _sizing_spec(path_key: str, settings: dict | None) -> dict | None:
    if settings is None:
        return DEFAULT_SIZING.get(path_key)
    return (settings.get(path_key) or {}).get("sizing")


def _requirement_specs(path_key: str, settings: dict | None) -> list[dict]:
    if settings is None:
        return DEFAULT_REQUIREMENTS.get(path_key, [])
    return (settings.get(path_key) or {}).get("requirements") or []


# --- Desk-override validation (0120) ------------------------------------------
#
# Pure shape validators for PUT /program-settings payloads. They raise
# ValueError (the router maps it to a 422) and return the NORMALIZED object
# that gets stored — always carrying its "model" so a stored override can
# never be ambiguous about which sizing family it belongs to.

_DEPOSIT_INPUTS = frozenset({"deposits_monthly_avg", "adb"})
_MAX_TERM_MONTHS = 360


def _number(value, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if positive and value <= 0:
        raise ValueError(f"{name} must be > 0")
    return float(value)


def _triplet(value, name: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{name} must be a list of exactly 3 numbers")
    return [_number(item, name, positive=True) for item in value]


def validate_sizing(path_key: str, sizing) -> dict:
    """Validate + normalize one sizing override. Raises ValueError on any
    shape violation; collateral paths (cre, floorplan) accept none at all."""
    model = path_model(path_key)
    if model == "collateral":
        raise ValueError(f"'{path_key}' is collateral-sized — sizing overrides are not accepted")
    if not isinstance(sizing, dict):
        raise ValueError("sizing must be an object")
    declared = sizing.get("model", model)
    if declared != model:
        raise ValueError(f"sizing model must be '{model}' for '{path_key}'")

    if model == "dscr":
        allowed = {"model", "dscr", "term_months", "annual_rate", "ceiling"}
        unknown = set(sizing) - allowed
        if unknown:
            raise ValueError(f"unknown sizing keys: {sorted(unknown)}")
        dscr = _triplet(sizing.get("dscr"), "dscr")
        if not (dscr[0] >= dscr[1] >= dscr[2]):
            raise ValueError("dscr triplet must run conservative >= typical >= aggressive")
        terms = sizing.get("term_months")
        if not isinstance(terms, (list, tuple)) or len(terms) != 3:
            raise ValueError("term_months must be a list of exactly 3 integers")
        term_months: list[int] = []
        for t in terms:
            if isinstance(t, bool) or not isinstance(t, int):
                raise ValueError("term_months must be integers")
            if not 0 < t <= _MAX_TERM_MONTHS:
                raise ValueError(f"term_months must be in 1..{_MAX_TERM_MONTHS}")
            term_months.append(t)
        if not (term_months[0] <= term_months[1] <= term_months[2]):
            raise ValueError(
                "term_months must be non-decreasing (conservative <= typical <= aggressive) — "
                "with the descending DSCR triplet this keeps min <= typical <= max"
            )
        annual_rate = _number(sizing.get("annual_rate"), "annual_rate")
        if not 0 < annual_rate < 0.5:
            raise ValueError("annual_rate must be between 0 and 0.5 (exclusive)")
        ceiling = _number(sizing.get("ceiling"), "ceiling", positive=True)
        return {
            "model": "dscr",
            "dscr": dscr,
            "term_months": term_months,
            "annual_rate": annual_rate,
            "ceiling": ceiling,
        }

    allowed = {"model", "input", "multiples", "ceiling"}
    unknown = set(sizing) - allowed
    if unknown:
        raise ValueError(f"unknown sizing keys: {sorted(unknown)}")
    source = sizing.get("input")
    if source not in _DEPOSIT_INPUTS:
        raise ValueError(f"input must be one of {sorted(_DEPOSIT_INPUTS)}")
    multiples = _triplet(sizing.get("multiples"), "multiples")
    if not (multiples[0] <= multiples[1] <= multiples[2]):
        raise ValueError("multiples must be positive and ascending")
    ceiling = _number(sizing.get("ceiling"), "ceiling", positive=True)
    return {"model": "deposit", "input": source, "multiples": multiples, "ceiling": ceiling}


def validate_requirements(specs) -> list[dict]:
    """Validate + normalize a ReqSpec list override. Raises ValueError."""
    if not isinstance(specs, (list, tuple)) or not specs:
        raise ValueError("requirements must be a non-empty list")
    out: list[dict] = []
    for spec in specs:
        if not isinstance(spec, dict):
            raise ValueError("each requirement must be an object")
        unknown = set(spec) - {"kind", "threshold", "target_key"}
        if unknown:
            raise ValueError(f"unknown requirement keys: {sorted(unknown)}")
        kind = spec.get("kind")
        if kind not in _BUILDERS:
            raise ValueError(f"unknown requirement kind '{kind}'")
        threshold = _number(spec.get("threshold"), "threshold")
        if threshold < 0:
            raise ValueError("threshold must be >= 0")
        if kind in _INT_KINDS:
            if threshold != int(threshold):
                raise ValueError(f"{kind} threshold must be an integer")
            threshold = int(threshold)
        normalized: dict = {"kind": kind, "threshold": threshold}
        target_key = spec.get("target_key")
        if target_key is not None:
            if target_key not in _TARGET_KEYS:
                raise ValueError(f"unknown target_key '{target_key}'")
            normalized["target_key"] = target_key
        out.append(normalized)
    return out

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


def monthly_payment_for_goal(
    goal: float, path_key: str = "conventional", settings: dict | None = None
) -> float | None:
    """Typical-case monthly P&I a funding goal implies (used by the target
    proposal service to align ADB/liquidity proposals to the goal)."""
    spec = _sizing_spec(path_key, settings)
    if spec is None or spec.get("model") != "dscr" or goal is None or goal <= 0:
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


def size_program(
    path_key: str, metrics: dict, targets: dict, settings: dict | None = None
) -> dict:
    """PURE program sizing: what this dealer could fund on each path today.

    Returns {funding_min, funding_typical, funding_max, sizing_basis,
    sizing_constraints}. Amounts are None when the path cannot be sized from
    the data on file; sizing_basis says which model produced the numbers.
    settings is a merged_settings() dict (None = code defaults).

    metrics is the snapshot metric tree; callers that have it may inject
    metrics["deposits_monthly_avg"] (average monthly deposits) — without it
    the deposit-multiple path reports insufficient data instead of guessing.
    """
    v = _values(metrics, targets)
    spec = _sizing_spec(path_key, settings)
    model = (spec or {}).get("model")

    if model == "dscr":
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

    if model == "deposit":
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


# Statement-months fallback for paths whose requirement list has no history
# spec (their checklist trades history for score) — the goal gate keeps the
# original bar.
_DEFAULT_STATEMENT_MONTHS: dict[str, int] = {"conventional": 6}


def requirements_for_amount(
    path_key: str, goal: float, metrics: dict, settings: dict | None = None
) -> list[dict]:
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
        # Statement history a lender file needs = the path's own history
        # requirement in the merged settings; paths that carry no history
        # spec fall back per-path (conventional demanded 6 pre-refactor —
        # its checklist uses score instead of history, but the funding-plan
        # gate keeps the stricter bar).
        required = _DEFAULT_STATEMENT_MONTHS.get(path_key, 3)
        for spec in _requirement_specs(path_key, settings):
            if spec.get("kind") == "history" and spec.get("threshold") is not None:
                required = int(spec["threshold"])
                break
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

    sizing = _sizing_spec(path_key, settings)
    model = (sizing or {}).get("model")

    if model == "dscr":
        spec = sizing
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

    if model == "deposit":
        spec = sizing
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
