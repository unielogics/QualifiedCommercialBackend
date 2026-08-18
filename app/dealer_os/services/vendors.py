"""Vendor-level rollup and classification.

The determining factor for "does this repeat?" is the VENDOR, not a clock.
services/recurrence.detect_groups only accepts a group whose median interval
lands in a cadence band, which rejects real dealer activity outright: 298
merchant-services deposits spread across 6 months fire ~50 times a month, so
their median interval is ~0 days and no band matches. On a live dealer with
414 cash events that engine returned ZERO groups while the same events contain
at least ten obvious repeating vendors.

So this module groups on normalized vendor identity and asks a simpler,
truer question: does this counterparty appear across multiple months? Cadence
is still computed and reported, but as a descriptive attribute rather than a
gate.

Classification is heuristic and deterministic (no model call). Every vendor
gets a proposed category; an admin correction is stored as a
DealerCategoryRule and always wins — same precedence law as targets and
account roles.

Pure functions here; the DB-touching helpers take an AsyncSession and never
commit.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from statistics import median
from typing import Any, Iterable, Sequence

# --- Vendor identity ----------------------------------------------------------

_KEY_MAX_LEN = 60

# Bank-instrument noise that carries no counterparty. These are stripped from
# the head of a description so "ACH DEBIT CHASE CARD" and "ELECTRONIC PMT WEB
# CHASE CARD" land on the same vendor.
_INSTRUMENT_PREFIXES = (
    "ACH DEBIT", "ACH CREDIT", "ACH PMT", "CCD DEBIT", "CCD CREDIT",
    "ELECTRONIC PMT WEB", "ELECTRONIC PMT", "PREAUTHORIZED DEBIT", "PREAUTHORIZED",
    "WIRE TRANSFER OUTGOING", "WIRE TRANSFER INCOMING", "WIRE TRANSFER",
    "ETRANSFER CREDIT", "ETRANSFER DEBIT", "ETRANSFER",
    "ONLINE XFER", "ONLINE PAYMENT", "ONLINE",
    "DEBIT CARD PURCHASE", "POS PURCHASE", "RECURRING DEBIT",
)


def normalize_vendor(description: str) -> str:
    """Raw statement description -> stable vendor key.

    Uppercases, drops digits and punctuation (dates, reference numbers, store
    numbers, amounts), then strips leading bank-instrument words so the key is
    the counterparty rather than the rail it arrived on."""
    s = (description or "").upper()
    s = re.sub(r"\d+", " ", s)
    s = re.sub(r"[^A-Z ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    changed = True
    while changed:
        changed = False
        for prefix in _INSTRUMENT_PREFIXES:
            if s.startswith(prefix + " "):
                s = s[len(prefix) + 1 :].strip()
                changed = True
                break
    # Reference codes (W0940, M0871, REF A12) reduce to varying 1-2 letter
    # orphans after digit-stripping — they split one lender into many keys
    # and break regular-payment detection. Drop trailing short tokens, but
    # never below one remaining token.
    tokens = s.split(" ")
    while len(tokens) > 1 and len(tokens[-1]) <= 2:
        tokens.pop()
    s = " ".join(tokens)
    return s[:_KEY_MAX_LEN].strip()


# --- Classification -----------------------------------------------------------
#
# Ordered: the first category whose keywords hit wins, so specific categories
# (floorplan) are checked before general ones (loan).

CATEGORIES: tuple[str, ...] = (
    "floorplan",
    "loan",
    "credit_card",
    "merchant_services",
    "tax",
    "payroll",
    "insurance",
    "rent",
    "utilities",
    "bank_fees",
    "owner_draw",
    "transfer",
    "vendor",
    "other",
)

# Categories whose outflows are contractual debt service — these seed the debt
# schedule draft and, through it, DSCR.
DEBT_CATEGORIES: frozenset[str] = frozenset({"floorplan", "loan", "credit_card"})

_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("floorplan", (
        "FLOORPLAN", "FLOOR PLAN", "NEXTGEAR", "AFC ", "AUTOMOTIVE FINANCE",
        "WESTLAKE FLOORING", "FLOORING", "CURBSTONE", "DEALER FUNDING",
    )),
    ("loan", (
        "LOAN", "LENDING", "CAPITAL", "FUNDING", "ADVANCE", "SBA",
        "ONDECK", "KABBAGE", "FORWARDLINE", "CREDIBLY", "RAPID FINANCE",
        "NOTE PAYMENT", "TERM PMT", "INSTALLMENT",
    )),
    ("credit_card", (
        "AMEX", "AMERICAN EXPRESS", "CITI CARD", "CHASE CARD", "CAPITAL ONE",
        "DISCOVER", "VISA PAYMENT", "MASTERCARD", "CARD ONLINE PAYMENT",
        "CARDMEMBER", "SYNCHRONY",
    )),
    ("merchant_services", (
        "MERCHANT SVS", "MERCHANT SERVICES", "MERCHANT DEPOSIT", "CARD SETTLEMENT",
        "STRIPE", "SQUARE", "TSYS", "ELAVON", "WORLDPAY", "CLOVER", "TOAST",
    )),
    ("tax", (
        "IRS", "DRS", "DEPT OF REVENUE", "DEPARTMENT OF REVENUE", "TAX",
        "EFTPS", "TREASURY", "FRANCHISE TAX", "SALES TAX", "USATAXPYMT",
    )),
    ("payroll", (
        "PAYROLL", "PAYCHEX", "ADP ", "GUSTO", "INTUIT PAYROLL", "TRINET",
        "WAGE", "DIRECT DEP",
    )),
    ("insurance", (
        "INSURANCE", "GEICO", "PROGRESSIVE", "STATE FARM", "ALLSTATE",
        "LIBERTY MUTUAL", "HARTFORD", "ZURICH", "GARAGE LIABILITY",
    )),
    ("rent", ("RENT", "LEASE", "LANDLORD", "PROPERTIES", "REALTY", "MANAGEMENT LLC")),
    ("utilities", (
        "ELECTRIC", "ENERGY", "GAS COMPANY", "WATER", "COMCAST", "VERIZON",
        "ATT ", "T MOBILE", "EVERSOURCE", "UTILITY", "INTERNET",
    )),
    ("bank_fees", (
        "SERVICE CHARGE", "ANALYSIS FEE", "MAINTENANCE FEE", "OVERDRAFT",
        "NSF", "RETURNED ITEM", "WIRE FEE", "STOP PAYMENT",
    )),
    ("owner_draw", ("OWNER", "DRAW", "DISTRIBUTION", "SHAREHOLDER", "MEMBER DRAW")),
    ("transfer", (
        "TRANSFER FROM", "TRANSFER TO", "XFER", "INTERNAL TRANSFER",
        "BOOK TRANSFER", "ZELLE", "SWEEP",
    )),
)


# Keywords match on WORD BOUNDARIES, never as bare substrings: "NSF" is inside
# "TRA-NSF-ER", so a substring test files every incoming transfer as a bank fee.
# Compiled once per keyword; multi-word keywords match across a single space.
_COMPILED_RULES: tuple[tuple[str, tuple[tuple[re.Pattern[str], str], ...]], ...] = tuple(
    (
        category,
        tuple(
            (re.compile(rf"\b{re.escape(kw.strip())}\b"), kw.strip())
            for kw in keywords
        ),
    )
    for category, keywords in _RULES
)

# Descriptions that name an instrument rather than a counterparty. Rolling
# every paper check into one "CHECK" vendor invents a $83k relationship that
# does not exist.
_NON_VENDOR_KEYS = frozenset({
    "CHECK", "CHECKS", "DEPOSIT", "WITHDRAWAL", "DEBIT", "CREDIT",
    "COUNTER CREDIT", "COUNTER DEBIT", "MOBILE DEPOSIT", "ATM DEPOSIT",
    "CASH DEPOSIT", "MISCELLANEOUS", "ADJUSTMENT",
})


def classify_vendor(
    vendor_key: str, direction: int, self_names: Sequence[str] = ()
) -> tuple[str, str]:
    """(category, rationale) for a vendor key. Deterministic, no model call.

    direction: +1 money in, -1 money out. Inbound card settlement is revenue;
    an outbound one is a processing fee, and they must not share a category.

    self_names: the dealer's own legal/trading names. Money moving to or from
    the business itself is an internal transfer, not a trade vendor — without
    this a $395k wire to their own LLC reads as their largest supplier."""
    text = f" {vendor_key} "
    for name in self_names:
        cleaned = normalize_vendor(name)
        if cleaned and len(cleaned) >= 6 and cleaned in text:
            return "transfer", f"Counterparty is the business itself ({name}) — internal movement."

    for category, patterns in _COMPILED_RULES:
        for pattern, label in patterns:
            if pattern.search(text):
                if category == "merchant_services" and direction < 0:
                    return "bank_fees", f"Outbound to a card processor ({label}) — processing fees."
                return category, f"Matched '{label}' in the vendor name."
    if direction > 0:
        return "other", "Inbound from an unrecognized counterparty."
    return "vendor", "Outbound to an unrecognized counterparty — treated as a trade vendor."


# --- Rollup -------------------------------------------------------------------


@dataclass
class VendorEvent:
    occurred_on: date
    description: str
    amount: float


@dataclass
class VendorRollup:
    key: str
    sample_description: str
    direction: int              # +1 inbound, -1 outbound
    category: str
    category_source: str        # "rule" (admin) | "heuristic"
    rationale: str
    count: int
    months: int                 # distinct calendar months seen
    first_seen: date
    last_seen: date
    total: float
    median_amount: float
    monthly_average: float      # total / months seen
    cadence: str                # descriptive: monthly | weekly | irregular | one_off
    is_recurring: bool          # seen in >= 2 distinct months
    amount_stable: bool
    debt_like: bool
    _amounts: list[float] = field(default_factory=list, repr=False)


_MIN_RECURRING_MONTHS = 2


def _cadence_label(months: int, count: int, intervals: Sequence[int]) -> str:
    if count <= 1:
        return "one_off"
    if months < _MIN_RECURRING_MONTHS:
        return "burst"          # several hits inside a single month
    per_month = count / max(1, months)
    if per_month >= 8:
        return "continuous"     # e.g. daily card settlement batches
    if per_month >= 3:
        return "weekly"
    if not intervals:
        return "irregular"
    med = median(intervals)
    if 25 <= med <= 38:
        return "monthly"
    if 12 <= med <= 18:
        return "semi_monthly"
    if 5 <= med <= 10:
        return "weekly"
    if 80 <= med <= 100:
        return "quarterly"
    return "irregular"


def rollup_vendors(
    events: Iterable[VendorEvent],
    overrides: dict[str, str] | None = None,
    self_names: Sequence[str] = (),
) -> list[VendorRollup]:
    """Group events by (vendor, direction) and summarize each counterparty.

    overrides maps a vendor key to an admin-set category; it always wins over
    the heuristic. Sorted by absolute total, descending — the biggest money
    movements first, which is the order an underwriter reads them in."""
    overrides = overrides or {}
    buckets: dict[tuple[str, int], list[VendorEvent]] = defaultdict(list)
    for e in events:
        amount = float(e.amount or 0)
        if amount == 0:
            continue
        key = normalize_vendor(e.description)
        if not key or key in _NON_VENDOR_KEYS:
            continue
        buckets[(key, 1 if amount > 0 else -1)].append(e)

    out: list[VendorRollup] = []
    for (key, direction), members in buckets.items():
        members.sort(key=lambda ev: ev.occurred_on)
        amounts = [float(m.amount) for m in members]
        month_keys = {(m.occurred_on.year, m.occurred_on.month) for m in members}
        intervals = [
            (b.occurred_on - a.occurred_on).days for a, b in zip(members, members[1:])
        ]
        med_amount = median(amounts)
        mad = median([abs(a - med_amount) for a in amounts]) if amounts else 0.0
        stable = bool(abs(med_amount) > 0 and mad / abs(med_amount) <= 0.25)

        category, rationale = classify_vendor(key, direction, self_names)
        source = "heuristic"
        if key in overrides:
            category, source = overrides[key], "rule"
            rationale = "Set by an admin — the AI never overwrites this."

        months = len(month_keys)
        out.append(
            VendorRollup(
                key=key,
                sample_description=(members[-1].description or "")[:160],
                direction=direction,
                category=category,
                category_source=source,
                rationale=rationale,
                count=len(members),
                months=months,
                first_seen=members[0].occurred_on,
                last_seen=members[-1].occurred_on,
                total=round(sum(amounts), 2),
                median_amount=round(med_amount, 2),
                monthly_average=round(sum(amounts) / max(1, months), 2),
                cadence=_cadence_label(months, len(members), intervals),
                is_recurring=months >= _MIN_RECURRING_MONTHS,
                amount_stable=stable,
                debt_like=direction < 0 and category in DEBT_CATEGORIES,
                _amounts=amounts,
            )
        )

    out.sort(key=lambda v: abs(v.total), reverse=True)
    return out


# --- Debt schedule draft ------------------------------------------------------


def draft_debt_rows(vendors: Sequence[VendorRollup]) -> list[dict[str, Any]]:
    """Propose debt-schedule rows from the debt-like outbound vendors.

    The monthly payment is the MEDIAN observed payment, not the mean: a single
    balloon or catch-up payment should not inflate an ongoing obligation. Only
    vendors seen across at least two months qualify — one payment to a lender
    is not evidence of a schedule.

    Every row is a DRAFT. The admin edits, adds and deletes; nothing here is
    treated as fact until a human has looked at it."""
    rows: list[dict[str, Any]] = []
    for v in vendors:
        if not v.debt_like or not v.is_recurring:
            continue
        monthly = abs(v.median_amount)
        if v.cadence in ("weekly", "continuous"):
            # Several payments a month: the monthly obligation is what actually
            # left the account per month, not one instalment.
            monthly = abs(v.monthly_average)
        if monthly <= 0:
            continue
        rows.append(
            {
                "lender": v.sample_description[:160] or v.key[:160],
                "vendor_key": v.key,
                "category": v.category,
                "monthly_payment": round(monthly, 2),
                "observed_months": v.months,
                "observed_count": v.count,
                "last_seen": v.last_seen,
                "amount_stable": v.amount_stable,
                "cadence": v.cadence,
                "rationale": (
                    f"{v.count} payment(s) across {v.months} month(s); "
                    f"{'stable' if v.amount_stable else 'variable'} amount, {v.cadence} cadence."
                ),
            }
        )
    rows.sort(key=lambda r: r["monthly_payment"], reverse=True)
    return rows
