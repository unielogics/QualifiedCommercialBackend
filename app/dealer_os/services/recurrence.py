"""Deterministic recurrence & document-freshness logic (no AI calls).

Product law: "The AI does all the extraction, the system regulates the data
and then maps it." Extraction stays AI (services/extract.py); everything in
this module is pure date/pattern math layered on top of the event ledger:

- normalize_desc: raw statement description -> stable merchant key
- detect_groups: recurring payment/deposit groups keyed by (merchant key, sign)
- classify_irregular: large one-off outflows outside any recurring group
- merge_recurrence_flags: pure flags-JSONB merge (admin corrections are law)
- stamp_recurrence: persist the detection onto dos_cash_events.flags
  (flushes, never commits — callers own the transaction boundary)
- compute_freshness: coverage date math — expected/missing statement months
  relative to an injected `today` (unit-testable)

Everything except stamp_recurrence is pure (no DB, no IO).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median
from typing import Any, Iterable, NamedTuple, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DealerCashEvent

MAX_EVENTS = 5000            # stamp/detect cap — matches the ledger list cap
MIN_EVENTS_TO_DETECT = 6     # below this there is nothing to detect
MIN_OCCURRENCES = 3          # a "pattern" needs at least 3 hits
STABILITY_MAX_MAD_RATIO = 0.30  # MAD/|median| <= 0.30 -> amount_stable
IRREGULAR_FLOOR = 1500.0     # one-off outflow threshold floor (dollars)
_KEY_MAX_LEN = 60

# (median-interval-days lo, hi, cadence label, normalized cadence length days)
_CADENCE_BANDS: tuple[tuple[int, int, str, int], ...] = (
    (5, 9, "weekly", 7),
    (12, 17, "biweekly", 14),
    (26, 35, "monthly", 30),
    (80, 100, "quarterly", 91),
)

# cadence -> multiplier that normalizes avg_amount to a monthly equivalent
_MONTHLY_FACTOR = {"weekly": 52.0 / 12.0, "biweekly": 26.0 / 12.0, "monthly": 1.0, "quarterly": 1.0 / 3.0}


class EventLite(NamedTuple):
    """The minimal event shape the pure core operates on — account-agnostic."""

    id: Any
    occurred_on: date
    description: str
    amount: float


def normalize_desc(description: str) -> str:
    """Raw statement description -> stable merchant key.

    Uppercase; digits (dates, reference/check numbers, amounts) and every
    punctuation run collapse to spaces; whitespace collapses; capped length.
    "ACH PMT #4821 07/02 CHASE CARD" and "ACH PMT #9977 08/02 CHASE CARD"
    both map to "ACH PMT CHASE CARD".
    """
    s = (description or "").upper()
    s = re.sub(r"\d+", " ", s)       # dates, reference numbers, amounts
    s = re.sub(r"[^A-Z ]+", " ", s)  # punctuation runs (# / - . etc.)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:_KEY_MAX_LEN].strip()


@dataclass
class RecurringGroup:
    key: str                 # (merchant key; sign is carried by `direction`)
    sample_description: str
    cadence: str             # weekly|biweekly|monthly|quarterly
    occurrences: int
    avg_amount: float        # signed mean
    median_amount: float     # signed median
    amount_stable: bool      # MAD/|median| <= STABILITY_MAX_MAD_RATIO
    first_seen: date
    last_seen: date
    next_expected_on: date   # last_seen + normalized cadence days
    direction: str           # inflow|outflow
    event_ids: list = field(default_factory=list)

    @property
    def monthly_equivalent(self) -> float:
        return round(self.avg_amount * _MONTHLY_FACTOR[self.cadence], 2)


# Descriptions that reduce to a bare payment instrument carry no payee
# identity — "CHECK 1001" / "CHECK 1042" are different payees. Never group.
_INSTRUMENT_KEYS = {
    "CHECK", "CHK", "ACH", "ACH PMT", "ACH PAYMENT", "ACH DEBIT", "ACH CREDIT",
    "WIRE", "WIRE OUT", "WIRE IN", "WIRE TRANSFER", "DEBIT", "DEBIT CARD",
    "DEBIT CARD PURCHASE", "CREDIT", "WITHDRAWAL", "DEPOSIT", "ATM",
    "ATM WITHDRAWAL", "POS", "POS PURCHASE", "TRANSFER", "ONLINE TRANSFER",
    "ONLINE PAYMENT", "BILL PAY", "BILLPAY", "PAYMENT", "PMT", "ZELLE",
    "CASH", "CASH DEPOSIT", "COUNTER WITHDRAWAL",
}


def detect_groups(events: Iterable[EventLite]) -> list[RecurringGroup]:
    """Group events by (merchant_key, sign) and keep only true recurrences:
    >= MIN_OCCURRENCES hits whose median interval lands in a cadence band.
    Pure — sorted by |monthly_equivalent| desc."""
    buckets: dict[tuple[str, int], list[EventLite]] = {}
    for e in events:
        amount = float(e.amount or 0)
        if amount == 0:
            continue
        key = normalize_desc(e.description)
        if not key or key in _INSTRUMENT_KEYS:
            continue
        sign = 1 if amount > 0 else -1
        buckets.setdefault((key, sign), []).append(e)

    groups: list[RecurringGroup] = []
    for (key, sign), members in buckets.items():
        if len(members) < MIN_OCCURRENCES:
            continue
        members = sorted(members, key=lambda ev: ev.occurred_on)
        intervals = [(b.occurred_on - a.occurred_on).days for a, b in zip(members, members[1:])]
        med_interval = median(intervals)
        cadence: str | None = None
        cadence_days = 0
        for lo, hi, label, days in _CADENCE_BANDS:
            if lo <= med_interval <= hi:
                cadence, cadence_days = label, days
                break
        if cadence is None:
            continue  # no recognizable cadence -> not recurring
        amounts = [float(m.amount) for m in members]
        med_amount = median(amounts)
        mad = median(abs(a - med_amount) for a in amounts)
        amount_stable = bool(abs(med_amount) > 0 and mad / abs(med_amount) <= STABILITY_MAX_MAD_RATIO)
        last_seen = members[-1].occurred_on
        groups.append(
            RecurringGroup(
                key=key,
                sample_description=(members[-1].description or "")[:160],
                cadence=cadence,
                occurrences=len(members),
                avg_amount=round(sum(amounts) / len(amounts), 2),
                median_amount=round(med_amount, 2),
                amount_stable=amount_stable,
                first_seen=members[0].occurred_on,
                last_seen=last_seen,
                next_expected_on=last_seen + timedelta(days=cadence_days),
                direction="inflow" if sign > 0 else "outflow",
                event_ids=[m.id for m in members],
            )
        )
    groups.sort(key=lambda g: abs(g.monthly_equivalent), reverse=True)
    return groups


def classify_irregular(
    events: Iterable[EventLite], groups: Sequence[RecurringGroup]
) -> list[EventLite]:
    """Irregular one-offs: outflow events NOT in any recurring group whose
    magnitude >= max(IRREGULAR_FLOOR, p90 of absolute outflow amounts). Pure."""
    recurring_ids = {eid for g in groups for eid in g.event_ids}
    outflows = [e for e in events if float(e.amount or 0) < 0]
    if not outflows:
        return []
    magnitudes = sorted(abs(float(e.amount)) for e in outflows)
    p90 = magnitudes[min(len(magnitudes) - 1, int(round(0.9 * (len(magnitudes) - 1))))]
    threshold = max(IRREGULAR_FLOOR, p90)
    return [
        e
        for e in outflows
        if e.id not in recurring_ids and abs(float(e.amount)) >= threshold
    ]


def merge_recurrence_flags(
    existing: dict | None, group: RecurringGroup | None, irregular: bool
) -> dict:
    """Merge detection results into an event's flags JSONB. Pure.

    Sets/overwrites ONLY recurring / cadence / recurrence_key / irregular.
    Every other key (fixed, addback_candidate, ai_suggested, early_payment,
    transfer, ...) is preserved verbatim. `irregular` is only ever set True —
    an existing True (e.g. from classify_event's add-back flagging) is never
    cleared. category / categorized_by are not this function's to touch.
    """
    flags = dict(existing) if isinstance(existing, dict) else {}
    if group is not None:
        flags["recurring"] = True
        flags["cadence"] = group.cadence
        flags["recurrence_key"] = group.key
    else:
        flags["recurring"] = False
        flags.pop("cadence", None)
        flags.pop("recurrence_key", None)
    if irregular:
        flags["irregular"] = True
    return flags


async def stamp_recurrence(db: AsyncSession, dealer_id: UUID) -> int:
    """Run detection over the dealer's ledger and merge results into each
    event's flags JSONB (merge_recurrence_flags — admin corrections are law:
    category/categorized_by are never touched). Skips dealers with fewer than
    MIN_EVENTS_TO_DETECT events. Flushes, never commits. Returns the number
    of events whose flags changed."""
    rows = (
        (
            await db.execute(
                select(DealerCashEvent)
                .where(DealerCashEvent.dealer_id == dealer_id)
                # Newest window: over-cap ledgers must drop the OLDEST rows,
                # or last_seen/next_expected freeze in the past.
                .order_by(DealerCashEvent.occurred_on.desc())
                .limit(MAX_EVENTS)
            )
        )
        .scalars()
        .all()
    )
    if len(rows) < MIN_EVENTS_TO_DETECT:
        return 0
    rows = list(reversed(rows))  # back to ascending for detection/readability
    lite = [
        EventLite(r.id, r.occurred_on, r.description or "", float(r.amount or 0)) for r in rows
    ]
    groups = detect_groups(lite)
    irregular_ids = {e.id for e in classify_irregular(lite, groups)}
    group_by_event: dict[Any, RecurringGroup] = {}
    for g in groups:
        for eid in g.event_ids:
            group_by_event[eid] = g
    stamped = 0
    for r in rows:
        if r.categorized_by == "admin":
            continue  # human correction wins — same exclusion as the rules engine
        merged = merge_recurrence_flags(r.flags, group_by_event.get(r.id), r.id in irregular_ids)
        if merged != (r.flags if isinstance(r.flags, dict) else {}):
            r.flags = merged  # reassign a fresh dict so JSONB change-tracking fires
            stamped += 1
    await db.flush()
    return stamped


# --- document freshness (coverage date math) ---------------------------------

_MONTH_KEY_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
EXPECTED_MONTHS_WINDOW = 6


def compute_freshness(covered_months: Iterable[str], today: date) -> dict[str, Any]:
    """Deterministic coverage freshness relative to `today` (injected — pure,
    unit-testable). Expected months are the EXPECTED_MONTHS_WINDOW most recent
    COMPLETED calendar months (for 2026-08-15 that is 2026-02..2026-07).

    Returns {current_through, expected_months, missing_months, is_current,
    days_since_latest} — days_since_latest counts from the END of the latest
    covered month to today (floored at 0; null when nothing is covered)."""
    covered = {
        m for m in covered_months if isinstance(m, str) and _MONTH_KEY_RE.match(m)
    }
    y, m = today.year, today.month
    expected: list[str] = []
    for _ in range(EXPECTED_MONTHS_WINDOW):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        expected.append(f"{y:04d}-{m:02d}")
    expected.sort()
    current_through = max(covered) if covered else None
    missing = sorted(set(expected) - covered)
    is_current = bool(covered) and expected[-1] in covered
    days_since_latest: int | None = None
    if current_through is not None:
        cy, cm = (int(p) for p in current_through.split("-"))
        ny, nm = (cy + 1, 1) if cm == 12 else (cy, cm + 1)
        month_end = date(ny, nm, 1) - timedelta(days=1)
        days_since_latest = max(0, (today - month_end).days)
    return {
        "current_through": current_through,
        "expected_months": expected,
        "missing_months": missing,
        "is_current": is_current,
        "days_since_latest": days_since_latest,
    }


def apply_manual_marks(
    events: list, manual: dict, groups: list
) -> tuple[list, set]:
    """Overlay ADMIN recurrence marks on the detected groups. PURE.

    manual maps event id -> "recurring" | "one_time". One-timed events leave
    any detected group (dropping a group below MIN_OCCURRENCES dissolves it);
    recurring-marked events whose merchant key produced no detected group get
    a synthesized group (cadence from intervals when they fit a band, else
    assumed monthly). Returns (groups, force_irregular_ids) — the caller adds
    force_irregular_ids (manually one-timed OUTFLOWS) to the irregular list
    regardless of size thresholds. Detection never overwrites these marks;
    this function makes the live view agree with them.
    """
    one_time_ids = {eid for eid, mark in manual.items() if mark in ("one_time", "none")}
    recurring_ids = {eid for eid, mark in manual.items() if mark == "recurring"}

    kept_groups = []
    for g in groups:
        members = [eid for eid in g.event_ids if eid not in one_time_ids]
        if len(members) >= MIN_OCCURRENCES:
            g.event_ids = members
            kept_groups.append(g)
    covered = {eid for g in kept_groups for eid in g.event_ids}

    # Synthesize groups for admin-marked vendors detection didn't find.
    by_key: dict[tuple[str, int], list] = {}
    for e in events:
        if e.id not in recurring_ids or e.id in covered:
            continue
        amount = float(e.amount or 0)
        if amount == 0:
            continue
        key = normalize_desc(e.description)
        if not key:
            key = (e.description or "MANUAL").strip().upper()[:_KEY_MAX_LEN] or "MANUAL"
        by_key.setdefault((key, 1 if amount > 0 else -1), []).append(e)

    for (key, sign), members in by_key.items():
        members = sorted(members, key=lambda ev: ev.occurred_on)
        amounts = [float(m.amount) for m in members]
        days = [m.occurred_on for m in members]
        cadence, cadence_days = "monthly", 30
        if len(days) >= 2:
            gaps = [(days[i + 1] - days[i]).days for i in range(len(days) - 1)]
            med = sorted(gaps)[len(gaps) // 2]
            for lo, hi, name, ndays in _CADENCE_BANDS:
                if lo <= med <= hi:
                    cadence, cadence_days = name, ndays
                    break
        med_amt = sorted(amounts)[len(amounts) // 2]
        mad = sorted(abs(a - med_amt) for a in amounts)[len(amounts) // 2]
        kept_groups.append(
            RecurringGroup(
                key=key,
                sample_description=(members[-1].description or key)[:120],
                cadence=cadence,
                occurrences=len(members),
                avg_amount=round(sum(amounts) / len(amounts), 2),
                median_amount=round(med_amt, 2),
                amount_stable=(abs(med_amt) > 0 and mad / abs(med_amt) <= STABILITY_MAX_MAD_RATIO),
                first_seen=days[0],
                last_seen=days[-1],
                next_expected_on=days[-1] + timedelta(days=cadence_days),
                direction="inflow" if sign > 0 else "outflow",
                event_ids=[m.id for m in members],
            )
        )

    kept_groups.sort(key=lambda g: -abs(g.monthly_equivalent))
    force_irregular = {
        e.id
        for e in events
        if manual.get(e.id) == "one_time" and float(e.amount or 0) < 0
    }
    return kept_groups, force_irregular
