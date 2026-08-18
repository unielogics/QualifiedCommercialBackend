"""Payment-timing analysis — 0120 desk admin.

PURE core over the cash-event ledger + the vendor rollup: when in the month
money leaves (per-day aggregates), which days are the big outflow days, and
which recurring/debt-like vendors could legitimately move later in the month
to raise average daily balance. No IO here — the router loads events and the
rollup and passes them in.

The ADB math is deliberately first-order: shifting a payment of $A from day
d1 to day d2 keeps the cash in the account (d2 - d1) extra days, so ADB over
a month_days-day month rises by ~A * (d2 - d1) / month_days. Real vendor
terms only — never statement-date window dressing (same guardrail as the AI
analyst).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from statistics import median, quantiles
from typing import Iterable, Sequence

from .vendors import VendorRollup, normalize_vendor

# How many big outflow days / top vendor labels per day to surface.
_BIG_DAYS = 5
_TOP_VENDORS_PER_DAY = 3

# Team-authored shift lifecycle: draft (team-only) -> proposed (dealer sees
# it) -> done | dismissed. Terminal states can only be reopened to proposed;
# proposed can be pulled back to draft.
SHIFT_STATUSES: frozenset[str] = frozenset({"draft", "proposed", "done", "dismissed"})
SHIFT_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"proposed"}),
    "proposed": frozenset({"done", "dismissed", "draft"}),
    "done": frozenset({"proposed"}),
    "dismissed": frozenset({"proposed"}),
}


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in SHIFT_TRANSITIONS.get(from_status, frozenset())


def adb_impact(
    amount: float,
    from_day: int,
    to_day: int,
    month_days: int = 30,
    direction: str = "out",
) -> float:
    """First-order ADB gain of moving a monthly payment from_day -> to_day.

    direction 'out' (default): paying later holds the cash longer, so a
    later to_day is positive. direction 'in': collecting a deposit EARLIER
    means the cash arrives sooner, so an earlier to_day is positive — the
    sign flips (-1) so out+later and in+earlier both read as gains."""
    sign = -1.0 if direction == "in" else 1.0
    return round(sign * float(amount) * (int(to_day) - int(from_day)) / month_days, 2)


def _clamp_day(day: int) -> int:
    return max(1, min(31, int(day)))


def _spread(days: Sequence[int]) -> list[float]:
    """[p25, p75] of the observed days-of-month (inclusive quartiles)."""
    if len(days) == 1:
        d = float(days[0])
        return [d, d]
    q = quantiles(sorted(days), n=4, method="inclusive")
    return [round(float(q[0]), 1), round(float(q[2]), 1)]


# Median last-activity day at or past this reads as a month-end statement cut.
_MONTH_END_CUTOFF_DAY = 28


def cutoff_days(events: Iterable) -> list[dict]:
    """PURE per-account statement-cutoff estimate.

    Preferred signal: statement DOCUMENT boundaries — each statement's last
    transaction day IS its cycle close, and this survives continuous
    coverage (where the calendar-month fallback always reads ~month-end
    because the next cycle's events fill the back half of every interior
    month). Per account: group events by document_id, take each document's
    max(occurred_on).day, cutoff_day = median across >= 2 documents.

    Fallback (no/one document — legacy rows without provenance): per
    (account, calendar month) max last-activity day, median across months,
    flagged "(est.)" since it is coverage-shaped, not cycle-shaped.

    basis reads "calendar month-end" at day >= 28 else "mid-cycle ~day N".
    account_id None is the unattributed bucket — emitted only when it has
    events. account_name is left None here; the router joins names."""
    # Per document track the max DATE (its cycle close) — the max day-of-month
    # would let an earlier month's day-28 line beat the true day-14 close.
    by_acct_doc: dict[object, dict[object, object]] = defaultdict(dict)
    last_activity: dict[object, dict[tuple[int, int], int]] = defaultdict(dict)
    months_seen: dict[object, set] = defaultdict(set)
    for r in events:
        acct = getattr(r, "account_id", None)
        day = _clamp_day(r.occurred_on.day)
        months_seen[acct].add((r.occurred_on.year, r.occurred_on.month))
        doc = getattr(r, "document_id", None)
        if doc is not None:
            prev = by_acct_doc[acct].get(doc)
            if prev is None or r.occurred_on > prev:
                by_acct_doc[acct][doc] = r.occurred_on
        month = (r.occurred_on.year, r.occurred_on.month)
        if day > last_activity[acct].get(month, 0):
            last_activity[acct][month] = day

    rows = []
    for acct in months_seen:
        doc_ends = [_clamp_day(d.day) for d in by_acct_doc.get(acct, {}).values()]
        estimated = False
        if len(doc_ends) >= 2:
            cutoff = _clamp_day(round(median(doc_ends)))
        else:
            months = last_activity.get(acct) or {}
            if not months:
                continue
            cutoff = _clamp_day(round(median(months.values())))
            estimated = True
        basis = (
            "calendar month-end"
            if cutoff >= _MONTH_END_CUTOFF_DAY
            else f"mid-cycle ~day {cutoff}"
        )
        if estimated:
            basis += " (est.)"
        rows.append(
            {
                "account_id": acct,
                "account_name": None,
                "cutoff_day": cutoff,
                "basis": basis,
                "months_observed": len(months_seen[acct]),
            }
        )
    rows.sort(key=lambda r: (r["account_id"] is None, str(r["account_id"] or "")))
    return rows


def analyze_timing(
    events: Iterable,
    vendors_rollup: Sequence[VendorRollup],
    months_window: int = 6,
    force_recurring_keys: set | None = None,
) -> dict:
    """events: cash-event rows (occurred_on, description, amount, account_id?)
    already windowed by the caller; vendors_rollup: the full-ledger rollup.

    Returns the payment-timing dict: per-day aggregates, the big outflow
    days with their dominant vendors, and the recurring/debt-like outflow
    vendors with their typical day-of-month and spread."""
    rows = list(events)

    # --- per-day aggregates over the window --------------------------------
    day_out: dict[int, float] = defaultdict(float)
    day_in: dict[int, float] = defaultdict(float)
    day_count: dict[int, int] = defaultdict(int)
    day_vendor_out: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    months_seen: set[tuple[int, int]] = set()
    vendor_days: dict[str, list[int]] = defaultdict(list)
    vendor_accounts: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    vendor_days_in: dict[str, list[int]] = defaultdict(list)
    vendor_accounts_in: dict[str, dict] = defaultdict(lambda: defaultdict(int))

    for r in rows:
        amount = float(r.amount or 0)
        if amount == 0:
            continue
        day = _clamp_day(r.occurred_on.day)
        months_seen.add((r.occurred_on.year, r.occurred_on.month))
        day_count[day] += 1
        key = normalize_vendor(r.description or "")
        if amount > 0:
            day_in[day] += amount
            if key:
                vendor_days_in[key].append(day)
                vendor_accounts_in[key][getattr(r, "account_id", None)] += 1
        else:
            day_out[day] += -amount
            if key:
                day_vendor_out[day][key] += -amount
                vendor_days[key].append(day)
                vendor_accounts[key][getattr(r, "account_id", None)] += 1

    months_observed = max(1, len(months_seen))
    days = [
        {
            "day": day,
            "out_total": round(day_out.get(day, 0.0), 2),
            "out_avg_month": round(day_out.get(day, 0.0) / months_observed, 2),
            "in_total": round(day_in.get(day, 0.0), 2),
            "in_avg_month": round(day_in.get(day, 0.0) / months_observed, 2),
            "count": day_count.get(day, 0),
        }
        for day in range(1, 32)
    ]

    # --- big outflow days, with the vendors that make them big -------------
    labels = {v.key: v.sample_description for v in vendors_rollup}
    big_days = []
    for row in sorted(days, key=lambda d: (-d["out_avg_month"], d["day"]))[:_BIG_DAYS]:
        if row["out_avg_month"] <= 0:
            continue
        by_vendor = day_vendor_out.get(row["day"], {})
        top = sorted(by_vendor.items(), key=lambda kv: -kv[1])[:_TOP_VENDORS_PER_DAY]
        big_days.append(
            {
                "day": row["day"],
                "out_avg_month": row["out_avg_month"],
                "top_vendors": [labels.get(key, key) for key, _ in top],
            }
        )

    # --- big deposit days + monthly deposit volume -------------------------
    big_deposit_days = []
    for row in sorted(days, key=lambda d: (-d["in_avg_month"], d["day"]))[:_BIG_DAYS]:
        if row["in_avg_month"] <= 0:
            continue
        big_deposit_days.append({"day": row["day"], "in_avg_month": row["in_avg_month"]})
    deposits_monthly_total = round(sum(d["in_avg_month"] for d in days), 2)

    # --- recurring outflow vendors (shift candidates) ----------------------
    recurring = []
    for v in vendors_rollup:  # rollup order = |total| desc, the read order
        forced = bool(force_recurring_keys and v.key in force_recurring_keys)
        if v.direction != -1 or not (v.debt_like or v.is_recurring or forced):
            continue
        observed = vendor_days.get(v.key)
        if not observed:
            continue  # no outflow events inside the window
        accounts = vendor_accounts.get(v.key, {})
        account_id = max(accounts, key=accounts.get) if accounts else None
        recurring.append(
            {
                "vendor_key": v.key,
                "label": v.sample_description,
                "direction": "out",
                "category": v.category,
                "cadence": v.cadence or None,
                "typical_day": _clamp_day(round(median(observed))),
                "day_spread": _spread(observed),
                "monthly_amount": round(abs(v.monthly_average), 2),
                "account_id": account_id,
            }
        )

    # --- recurring inflow vendors (deposit-acceleration candidates, 0121) --
    # Same shape as the outflow rows, direction 'in'. Transfers are excluded
    # outright — moving money between the dealer's own accounts is never a
    # receivables change.
    for v in vendors_rollup:
        # Deposit candidates are receivable-ish inflows only — loan/floorplan
        # proceeds, owner money and transfers are not collections.
        if v.direction != 1 or not v.is_recurring or v.category in (
            "transfer", "loan", "floorplan", "credit_card", "owner_draw", "bank_fees"
        ):
            continue
        observed = vendor_days_in.get(v.key)
        if not observed:
            continue  # no inflow events inside the window
        accounts = vendor_accounts_in.get(v.key, {})
        account_id = max(accounts, key=accounts.get) if accounts else None
        recurring.append(
            {
                "vendor_key": v.key,
                "label": v.sample_description,
                "direction": "in",
                "category": v.category,
                "cadence": v.cadence or None,
                "typical_day": _clamp_day(round(median(observed))),
                "day_spread": _spread(observed),
                "monthly_amount": round(abs(v.monthly_average), 2),
                "account_id": account_id,
            }
        )

    return {
        "days": days,
        "big_deposit_days": big_deposit_days,
        "deposits_monthly_total": deposits_monthly_total,
        "big_days": big_days,
        "recurring": recurring,
        "window_months": months_window,
        "computed_at": datetime.now(timezone.utc),
    }
