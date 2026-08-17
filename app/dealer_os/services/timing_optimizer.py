"""Timing optimizer — deterministic shift drafts, 0121 desk admin.

PURE proposal engine over the payment-timing analysis: which recurring
outflows could legitimately move later (under real vendor terms) and which
recurring deposits could legitimately land earlier (a real receivables
change — never statement-date window dressing), ranked by first-order ADB
impact. No IO here — the router loads the timing rows and cutoffs and
passes them in; nothing is persisted, ever. Same house style as the target
proposals: deterministic rules, f-string rationales embedding the numbers.
"""

from __future__ import annotations

from .payment_timing import adb_impact

# Categories whose payment date is contractually rigid or where slipping a
# date has consequences far beyond ADB (late floorplan, missed payroll,
# lapsed insurance). NEVER proposed, whatever the numbers say.
NEVER_MOVE: frozenset[str] = frozenset(
    {"floorplan", "loan", "credit_card", "payroll", "tax", "insurance", "rent"}
)
# Not a real counterparty payment at all — nothing to renegotiate.
NON_ACTIONABLE: frozenset[str] = frozenset({"transfer", "bank_fees"})

_MIN_AMOUNT = 1000.0  # below this the ADB impact is noise, not a move
# Inflow categories that are NOT receivables — never deposit-acceleration
# candidates (loan/floorplan proceeds, owner money, internal transfers).
_NON_RECEIVABLE_IN = frozenset({"loan", "floorplan", "credit_card", "owner_draw", "transfer", "bank_fees"})
_MAX_MOVES = 6

# Outflows already paid after this day gain too little from moving; deposits
# landing before it have nothing meaningful to accelerate.
_EARLY_OUT_DAY = 10
_LATE_IN_DAY = 10
_MAX_IN_SPREAD = 10.0  # a deposit day wobbling >10 days has no real terms to tighten
_MIN_IN_DAY = 3  # never propose collecting before day 3 — month-start noise
_SAFE_MARGIN = 3  # land payments this many days before the earliest cutoff
_DEFAULT_SAFE_DAY = 26  # no cutoff signal: late-month but clear of month-end
_MIN_SAFE_DAY = 5


def shift_key(direction: str, vendor_key: str | None, label: str) -> str:
    """Stable identity of a proposed move — vendor key when the rollup has
    one, the label otherwise, always direction-prefixed so an inflow and an
    outflow against the same counterparty never collide."""
    return f"{direction}:{vendor_key or 'label:' + (label or '')}"


def _safe_day(cutoffs: list[dict]) -> int | None:
    """Latest day an outflow can safely move to: a few days clear of the
    earliest statement cutoff, never past day 26. None when no safe landing
    day exists (a first-week cutoff) — drafting a payment onto the cutoff
    day itself would be the statement-date timing the product forbids."""
    days = [int(c["cutoff_day"]) for c in cutoffs if c.get("cutoff_day") is not None]
    safe = min(min(days) - _SAFE_MARGIN, _DEFAULT_SAFE_DAY) if days else _DEFAULT_SAFE_DAY
    return safe if safe >= _MIN_SAFE_DAY else None


def draft_optimized_shifts(
    recurring: list[dict],
    cutoffs: list[dict],
    staged_keys: frozenset[str] = frozenset(),
) -> list[dict]:
    """Draft up to _MAX_MOVES payment/deposit shifts from analyze_timing's
    recurring rows + cutoff_days' rows. staged_keys (shift_key strings of
    moves already on the dealer's table) are skipped, not re-proposed.

    Outflows: early-month, >= $1k/mo, movable category (UNKNOWN categories
    ARE movable — the safe default is to let the desk judge), proposed to
    safe_day only when that is actually later. Deposits: mid/late-month,
    tightly clustered (real terms to tighten), proposed earlier only.
    Returns dicts per the /timing/optimize contract, ranked |impact| desc."""
    safe_day = _safe_day(cutoffs)
    drafts: list[dict] = []
    for row in recurring:
        direction = row.get("direction") or "out"
        label = row.get("label") or ""
        if shift_key(direction, row.get("vendor_key"), label) in staged_keys:
            continue
        amt = float(row.get("monthly_amount") or 0.0)
        td = int(row.get("typical_day") or 0)
        if td < 1:
            continue

        if direction == "out":
            if safe_day is None:
                continue  # no landing day clear of the cutoff exists
            category = row.get("category")
            if category in NEVER_MOVE or category in NON_ACTIONABLE:
                continue
            if td > _EARLY_OUT_DAY or amt < _MIN_AMOUNT:
                continue
            to = safe_day
            if to <= td:
                continue
            est = adb_impact(amt, td, to, direction="out")
            rationale = (
                f"{label} leaves ~day {td}; paying day {to} under vendor terms "
                f"keeps ${amt:,.0f} in the account {to - td} extra days "
                f"(≈ +${est:,.0f} ADB)."
            )
        elif direction == "in":
            # Only genuine receivables accelerate: inbound loan/floorplan
            # proceeds, owner contributions and transfers are not
            # collections and must never be "tightened".
            if row.get("category") in _NON_RECEIVABLE_IN:
                continue
            if td < _LATE_IN_DAY or amt < _MIN_AMOUNT:
                continue
            spread = row.get("day_spread") or []
            if len(spread) < 2 or float(spread[1]) - float(spread[0]) > _MAX_IN_SPREAD:
                continue
            to = max(_MIN_IN_DAY, int(spread[0]))
            if to >= td:
                continue
            est = adb_impact(amt, td, to, direction="in")
            rationale = (
                f"Recurring deposit {label} (~${amt:,.0f}/mo) lands ~day {td}; "
                f"tightening collection terms to ~day {to} is a real receivables "
                f"change — never statement-date timing — worth ≈ +${est:,.0f} ADB."
            )
        else:
            continue

        drafts.append(
            {
                "vendor_key": row.get("vendor_key"),
                "label": label,
                "direction": direction,
                "from_day": td,
                "to_day": to,
                "monthly_amount": round(amt, 2),
                "est_adb_impact": est,
                "rationale": rationale,
            }
        )

    drafts.sort(key=lambda d: -abs(d["est_adb_impact"]))  # stable: rollup order breaks ties
    return drafts[:_MAX_MOVES]
