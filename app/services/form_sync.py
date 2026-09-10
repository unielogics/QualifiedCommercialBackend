"""Making the four financial forms agree with each other.

A file carries four forms — a profit and loss, a balance sheet, a business
debt schedule and a personal financial statement — and they overlap. The same
business name is typed on all four. Net income is on the P&L and again on the
balance sheet's equity block. Every loan on the debt schedule is also a
balance-sheet liability. Today those copies drift silently, and the desk finds
out from a lender.

**The rule this module is built on: an identity fact is shared; a figure is
cross-checked and offered, never silently written.**

A name is one fact stored four times, so filling a blank copy of it from a
typed one loses nobody anything — and `apply_identity` fills *blanks only*,
never a value somebody typed, even a value that disagrees. A figure is
different. Two figures that ought to match can differ for reasons the form
cannot see: a balance sheet dated mid-period, a loan closed the week after the
schedule was filled, an accountant's reclassification. Overwriting one with
the other destroys an answer a person gave and hides the very disagreement the
desk needs to see. So every figure comes back as a `Check` — a sentence naming
both sides and their numbers, and, where there is a single blank field that
would obviously take it, a `suggested_value` for the UI to *offer*. Nothing in
this module writes a figure into a body. That is deliberate and it is the
point: a form save that overwrote rows it did not own is the bug this codebase
spent the day removing.

Severity is honest. `warn` means two forms state the same thing differently.
`info` means here is a figure worth seeing beside that one — including the
comparison people most want and that is most often misread, monthly debt
service against the P&L's interest line, which are *not* meant to be equal.

Nothing here fires on a form nobody has filled in. A file opened for the first
time must not be covered in warnings.

**Keys.** A `Check` names the field it is about with a dotted path into that
form's body — `"header.business_name"`, `"sections.equity.current_period_net_income"`,
`"applicant.business_name"` — so the UI can scroll to it without a second map.
Where the subject is a derived figure rather than an input, the key is the name
of the entry in that form's `totals` (`"interest_bearing_debt"`,
`"net_income"`, `"total_balance"`).

Money is parsed with `pfs_schema._amount`, the one money parser, so "$1,250"
and "1,250" compare equal here exactly as they do on the forms.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from app.services import business_statement_schema as bss
from app.services.pfs_schema import _amount

P_AND_L = "p_and_l"
BALANCE_SHEET = "balance_sheet"
DEBT_SCHEDULE = "debt_schedule"
PFS = "pfs"

#: The four, in the order a packet prints them.
SHEET_KINDS: tuple[str, ...] = (P_AND_L, BALANCE_SHEET, DEBT_SCHEDULE, PFS)

#: How each form is named in a sentence a desk officer reads. Lower case: these
#: land mid-message.
SHEET_LABELS: dict[str, str] = {
    P_AND_L: "profit and loss",
    BALANCE_SHEET: "balance sheet",
    DEBT_SCHEDULE: "debt schedule",
    PFS: "personal financial statement",
}

Severity = Literal["info", "warn"]


# ---------------------------------------------------------------------------
# The shared identity block
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdentitySlot:
    """Where one identity fact lives on one form."""

    sheet: str
    #: Into the body, from the root.
    path: tuple[str, ...]

    @property
    def key(self) -> str:
        return ".".join(self.path)


@dataclass(frozen=True)
class IdentityField:
    key: str
    #: How the field is named mid-sentence.
    label: str
    slots: tuple[IdentitySlot, ...]
    #: A select, not free text: compared and reported as the word itself.
    choice: bool = False


#: The identity block, declared once. Slot order is the tie-break order when
#: two forms carry the same fact and nothing says which was typed last.
#:
#: Only `business_name` is on all four: the debt schedule asks for the business
#: and nothing else about who prepared it, and Form 413 is a *personal*
#: statement whose only business field is the name. A field is shared with the
#: forms that have somewhere to put it, and no form grows a box to make this
#: table tidier.
IDENTITY_FIELDS: tuple[IdentityField, ...] = (
    IdentityField(
        "business_name",
        "business name",
        (
            IdentitySlot(P_AND_L, ("header", "business_name")),
            IdentitySlot(BALANCE_SHEET, ("header", "business_name")),
            IdentitySlot(DEBT_SCHEDULE, ("business_name",)),
            IdentitySlot(PFS, ("applicant", "business_name")),
        ),
    ),
    IdentityField(
        "prepared_by",
        "preparer",
        (
            IdentitySlot(P_AND_L, ("header", "prepared_by")),
            IdentitySlot(BALANCE_SHEET, ("header", "prepared_by")),
        ),
    ),
    IdentityField(
        "basis",
        "accounting basis",
        (
            IdentitySlot(P_AND_L, ("header", "basis")),
            IdentitySlot(BALANCE_SHEET, ("header", "basis")),
        ),
        choice=True,
    ),
)

IDENTITY_FIELDS_BY_KEY: dict[str, IdentityField] = {
    field.key: field for field in IDENTITY_FIELDS
}


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

CODE_IDENTITY_DISAGREEMENT = "identity_disagreement"
CODE_NET_INCOME_AVAILABLE = "net_income_available"
CODE_NET_INCOME_MISMATCH = "net_income_mismatch"
CODE_DEBT_BALANCE_MISMATCH = "debt_balance_mismatch"
CODE_DEBT_BALANCE_ONE_SIDED = "debt_balance_one_sided"
CODE_DEBT_SERVICE_VS_INTEREST = "debt_service_vs_interest"


@dataclass(frozen=True)
class Check:
    """One thing two forms say differently, or one figure worth seeing beside
    another.

    `suggested_value` is an *offer*: the raw string that would go in the field
    named by `sheet`/`key` if a person decided the other form was right. It is
    set only where there is a single empty field that would plainly take it,
    and it is never set on a `warn` about a figure — a typed number is an
    answer, and the desk chooses. Nothing in this module applies one.
    """

    code: str
    severity: Severity
    #: The form the check is about — the one that would change.
    sheet: str
    key: str
    #: One sentence, naming both sides and their figures.
    message: str
    suggested_value: str | None = None
    #: The form the other figure came from.
    from_sheet: str | None = None
    from_key: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "sheet": self.sheet,
            "key": self.key,
            "message": self.message,
            "suggested_value": self.suggested_value,
            "from_sheet": self.from_sheet,
            "from_key": self.from_key,
        }


# ---------------------------------------------------------------------------
# Tolerances
# ---------------------------------------------------------------------------

#: The debt schedule against the balance sheet's debt lines. A hundred dollars
#: or a percent of the larger side, whichever is more: a schedule filled in on
#: Tuesday and a balance sheet dated Sunday differ by a payment or two, and
#: people round a loan balance to the nearest hundred.
DEBT_TOLERANCE_FLOOR = Decimal("100")
DEBT_TOLERANCE_SHARE = Decimal("0.01")

#: Net income against net income. These are the same figure, so the room is
#: only for rounding: a dollar, or half a percent of the larger side, whichever
#: is more — a P&L kept to whole dollars against a balance sheet carried to the
#: cent is not a disagreement.
NET_INCOME_TOLERANCE_FLOOR = Decimal("1")
NET_INCOME_TOLERANCE_SHARE = Decimal("0.005")

_ZERO = Decimal("0")
_CENT = Decimal("0.01")


# ---------------------------------------------------------------------------
# Reading bodies
# ---------------------------------------------------------------------------


def _body(bodies: Mapping[str, Any] | None, kind: str) -> dict[str, Any] | None:
    """One form's body, or None when it is absent, null or not a document.

    A form that was never opened is absent, not blank, and the difference
    matters: absent means say nothing.
    """
    value = (bodies or {}).get(kind)
    return value if isinstance(value, dict) else None


def _at(body: Mapping[str, Any] | None, path: tuple[str, ...]) -> Any:
    cursor: Any = body
    for step in path:
        if not isinstance(cursor, Mapping):
            return None
        cursor = cursor.get(step)
    return cursor


def _clean(value: Any) -> str:
    """A typed text value with its whitespace tidied, or "" for blank."""
    if value is None or isinstance(value, (list, dict)):
        return ""
    return " ".join(str(value).split())


def _same_text(left: str, right: str) -> bool:
    """Whether two typed strings say the same thing. Case and inner spacing
    are how people type, not what they mean: "Acme  LLC" and "acme llc" are one
    business, and reporting them as a disagreement teaches the desk to ignore
    the panel."""
    return left.casefold() == right.casefold()


def debt_schedule_answered(body: Mapping[str, Any] | None) -> bool:
    """Whether this body carries an answer about the file's debts at all.

    The same distinction `financial_statements.debt_rows_from_body` makes, for
    the same reason: a body with no `debts` list is a client that sent nothing
    — an autosave that fired before the form finished loading — and it must not
    be read as "this borrower owes nobody". An explicit empty list is an
    answer, and is compared like any other.
    """
    return isinstance((body or {}).get("debts"), list)


def debt_rows(body: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The obligations on a debt-schedule body.

    Every row, whoever owns it: the schedule's total is the file's total debt,
    not the part this form's saver may write.
    """
    debts = (body or {}).get("debts")
    if not isinstance(debts, list):
        return []
    return [row for row in debts if isinstance(row, dict)]


def debt_totals(body: Mapping[str, Any] | None) -> dict[str, Any]:
    """The schedule's two sums and how many lines carry a balance.

    Read the way `financial_statements.debt_rows_from_body` reads a figure, so
    the total shown beside the grid, the total filed on the file and the total
    cross-checked here are one number.
    """
    rows = debt_rows(body)
    balance = _ZERO
    monthly = _ZERO
    with_balance = 0
    for row in rows:
        if _clean(row.get("balance")):
            with_balance += 1
        balance += _amount(row.get("balance"))
        monthly += _amount(row.get("monthly_payment"))
    return {
        "total_balance": balance,
        "total_monthly_payment": monthly,
        "rows": len(rows),
        "rows_with_balance": with_balance,
    }


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _money(value: Decimal) -> str:
    """A figure the way it is read aloud: "$412,300", "$412,300.55", "-$500"."""
    quantized = Decimal(value).quantize(_CENT, rounding=ROUND_HALF_UP)
    magnitude = abs(quantized)
    if magnitude == magnitude.to_integral_value():
        body = f"{magnitude:,.0f}"
    else:
        body = f"{magnitude:,.2f}"
    return f"{'-' if quantized < 0 else ''}${body}"


def _plain(value: Decimal) -> str:
    """A figure as it would be typed into the field — no symbol, no separators,
    no trailing cents when there are none. What `suggested_value` carries."""
    quantized = Decimal(value).quantize(_CENT, rounding=ROUND_HALF_UP)
    if quantized == quantized.to_integral_value():
        return str(quantized.to_integral_value())
    return format(quantized, "f")


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _within(left: Decimal, right: Decimal, *, floor: Decimal, share: Decimal) -> bool:
    """Whether two figures agree, allowing `floor` dollars or `share` of the
    larger side, whichever is more."""
    allowed = max(floor, max(abs(left), abs(right)) * share)
    return abs(left - right) <= allowed


# ---------------------------------------------------------------------------
# Shared identity
# ---------------------------------------------------------------------------


def _edited_at(edited_at: Mapping[str, Any] | None, sheet: str) -> datetime | None:
    """When a form was last edited, however the caller holds it. Anything
    unparseable is treated as "not known", which sends the slot to the back of
    the queue rather than to the front of it."""
    value = (edited_at or {}).get(sheet)
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = _clean(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _sort_key(when: datetime | None) -> tuple[int, float]:
    """Newest first, "not known" last. Naive and aware timestamps are compared
    as floats rather than as datetimes, so a caller mixing a stored `updated_at`
    with a hand-built string sorts oddly at worst and never raises: this runs on
    a save path, and a timezone is not a reason to fail one."""
    if when is None:
        return (0, 0.0)
    return (1, when.timestamp())


def _stated(
    bodies: Mapping[str, Any] | None, field: IdentityField
) -> list[tuple[IdentitySlot, str]]:
    """Every non-blank copy of one fact, in slot order."""
    out: list[tuple[IdentitySlot, str]] = []
    for slot in field.slots:
        body = _body(bodies, slot.sheet)
        if body is None:
            continue
        value = _clean(_at(body, slot.path))
        if value:
            out.append((slot, value))
    return out


def _agreed(
    bodies: Mapping[str, Any] | None,
    field: IdentityField,
    edited_at: Mapping[str, Any] | None,
) -> tuple[IdentitySlot, str] | None:
    """The copy of one fact that stands: the most recently edited non-blank
    one, and slot order when nothing says which form was touched last."""
    stated = _stated(bodies, field)
    if not stated:
        return None
    ordered = sorted(
        enumerate(stated),
        key=lambda pair: (_sort_key(_edited_at(edited_at, pair[1][0].sheet)), -pair[0]),
        reverse=True,
    )
    return ordered[0][1]


def shared_identity(
    bodies: Mapping[str, Any] | None, *, edited_at: Mapping[str, Any] | None = None
) -> dict[str, str]:
    """The agreed value of each identity fact, across whichever forms exist.

    One fact stored four times has one answer, and this is it: the most
    recently edited non-blank copy, falling back to slot order when nothing
    says which form was touched last. Pass `edited_at` as `{sheet: when}` —
    datetimes or ISO strings, whichever the caller holds.

    A fact no form states is left out of the result rather than returned blank,
    so a caller can tell "nobody has said" from "somebody said nothing".

    This picks a value to *offer*; it never decides a disagreement. Where the
    copies differ, `checks` reports it and both stay exactly as typed.
    """
    identity: dict[str, str] = {}
    for field in IDENTITY_FIELDS:
        agreed = _agreed(bodies, field, edited_at)
        if agreed is not None:
            identity[field.key] = agreed[1]
    return identity


def apply_identity(
    bodies: Mapping[str, Any] | None, identity: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Fill every blank copy of a shared fact, and nothing else.

    Blanks only, and that is the whole contract. A value somebody typed is
    never replaced — not by a newer copy, not by a "better" one, not even when
    the two disagree, because a form that quietly rewrites what a person typed
    is the harm this module exists to avoid. A disagreement comes back from
    `checks` as a sentence for a human, and both copies stay as they are.

    `identity` defaults to `shared_identity(bodies)`. Bodies are not mutated:
    a changed form comes back as a new document, and a form that needed nothing
    comes back as the very object that was passed in, so a caller can tell
    whether anything happened by identity.
    """
    if identity is None:
        identity = shared_identity(bodies)
    out: dict[str, Any] = dict(bodies or {})
    for field in IDENTITY_FIELDS:
        value = _clean(identity.get(field.key))
        if not value:
            continue
        for slot in field.slots:
            body = _body(out, slot.sheet)
            if body is None:
                continue
            if _clean(_at(body, slot.path)):
                continue  # typed. Not ours to touch.
            if not _fillable(body, slot.path):
                continue  # something else is living there. Leave it alone.
            out[slot.sheet] = _filled(body, slot.path, value)
    return out


def _fillable(body: Mapping[str, Any], path: tuple[str, ...]) -> bool:
    """Whether the blank at `path` can be filled without discarding anything.

    Every container on the way down has to be a mapping or absent. A body
    holding something else where a block belongs is malformed, and replacing it
    would destroy whatever it does hold — the one thing this module never does.
    """
    cursor: Any = body
    for step in path[:-1]:
        cursor = cursor.get(step)
        if cursor is None:
            return True
        if not isinstance(cursor, Mapping):
            return False
    return isinstance(cursor, Mapping)


def _filled(body: dict[str, Any], path: tuple[str, ...], value: str) -> dict[str, Any]:
    """`body` with `path` set, copying only the containers on the way down."""
    if len(path) == 1:
        return {**body, path[0]: value}
    head, rest = path[0], path[1:]
    child = body.get(head)
    child = dict(child) if isinstance(child, Mapping) else {}
    return {**body, head: _filled(child, rest, value)}


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def _identity_checks(
    bodies: Mapping[str, Any] | None,
    edited_at: Mapping[str, Any] | None,
) -> list[Check]:
    """Where two forms state the same fact differently.

    Reported against the form that differs from the copy that stands, naming
    both forms and both values, with the other value offered — never applied.
    Nobody is told they are wrong: the older copy may well be the right one,
    and only a person knows which.
    """
    out: list[Check] = []
    for field in IDENTITY_FIELDS:
        agreed = _agreed(bodies, field, edited_at)
        if agreed is None:
            continue
        source, value_agreed = agreed
        for slot, value in _stated(bodies, field):
            if slot.sheet == source.sheet or _same_text(value, value_agreed):
                continue
            if field.choice:
                message = (
                    f"The {SHEET_LABELS[slot.sheet]} is marked {value.lower()} basis; the "
                    f"{SHEET_LABELS[source.sheet]} is marked {value_agreed.lower()}. Two "
                    f"statements filed together are normally on one basis — both are kept "
                    f"as typed."
                )
            else:
                message = (
                    f'The {SHEET_LABELS[slot.sheet]} gives the {field.label} as "{value}"; '
                    f'the {SHEET_LABELS[source.sheet]} gives "{value_agreed}". Both are '
                    f"kept as typed — worth confirming which one matches the filing."
                )
            out.append(
                Check(
                    code=CODE_IDENTITY_DISAGREEMENT,
                    severity="warn",
                    sheet=slot.sheet,
                    key=slot.key,
                    message=message,
                    suggested_value=value_agreed,
                    from_sheet=source.sheet,
                    from_key=source.key,
                )
            )
    return out


def _net_income_check(bodies: Mapping[str, Any] | None) -> list[Check]:
    """The P&L's net income against the balance sheet's equity line.

    Only when the balance sheet's "as of" date falls inside the P&L's period:
    a balance sheet dated the year end beside a first-quarter P&L holds a
    different figure on purpose, and saying so would be wrong.
    """
    pl = _body(bodies, P_AND_L)
    bs = _body(bodies, BALANCE_SHEET)
    if pl is None or bs is None:
        return []
    if not bss.has_typed_figures(P_AND_L, pl):
        return []  # nobody has filled the P&L in; it agrees with nothing yet.

    header = pl.get("header") or {}
    start = bss.parse_date(header.get("period_start"))
    end = bss.parse_date(header.get("period_end"))
    as_of = bss.parse_date((bs.get("header") or {}).get("as_of_date"))
    if start is None or end is None or as_of is None:
        return []
    if not (start <= as_of <= end):
        return []

    stated = _clean(((bs.get("sections") or {}).get("equity") or {}).get("current_period_net_income"))
    net_income = bss.pl_totals(pl)["net_income"]
    period = bss.period_label(P_AND_L, pl) or f"{start.isoformat()} to {end.isoformat()}"
    key = "sections.equity.current_period_net_income"

    if not stated:
        return [
            Check(
                code=CODE_NET_INCOME_AVAILABLE,
                severity="info",
                sheet=BALANCE_SHEET,
                key=key,
                message=(
                    f"The profit and loss for {period} reports net income of "
                    f"{_money(net_income)}, and the balance sheet is dated "
                    f"{as_of.isoformat()}, inside that period. Its current period net "
                    f"income line is empty."
                ),
                suggested_value=_plain(net_income),
                from_sheet=P_AND_L,
                from_key="net_income",
            )
        ]

    on_sheet = _amount(stated)
    if _within(
        on_sheet,
        net_income,
        floor=NET_INCOME_TOLERANCE_FLOOR,
        share=NET_INCOME_TOLERANCE_SHARE,
    ):
        return []
    return [
        Check(
            code=CODE_NET_INCOME_MISMATCH,
            severity="warn",
            sheet=BALANCE_SHEET,
            key=key,
            message=(
                f"The balance sheet dated {as_of.isoformat()} shows current period net "
                f"income of {_money(on_sheet)}; the profit and loss covering that date "
                f"({period}) reports {_money(net_income)} — a difference of "
                f"{_money(abs(on_sheet - net_income))}. They cover the same period, so "
                f"they are meant to be the same figure."
            ),
            # A typed figure is an answer. The desk decides which one stands.
            suggested_value=None,
            from_sheet=P_AND_L,
            from_key="net_income",
        )
    ]


def _debt_balance_check(bodies: Mapping[str, Any] | None) -> list[Check]:
    """The debt schedule's balances against the balance sheet's debt lines.

    The balance-sheet side is whatever carries the `interest_bearing` flag, so
    a liability line added to the schema joins this check by itself. Payables,
    accrued taxes and "other" liabilities are left out: they are debts, but not
    the kind that has a lender and a line on the schedule.
    """
    ds = _body(bodies, DEBT_SCHEDULE)
    bs = _body(bodies, BALANCE_SHEET)
    if ds is None or bs is None or not debt_schedule_answered(ds):
        return []

    schedule = debt_totals(ds)
    on_sheet = bss.bs_totals(bs)["interest_bearing_debt"]
    sheet_started = bss.has_typed_figures(BALANCE_SHEET, bs)
    listed = _plural(schedule["rows_with_balance"], "obligation", "obligations")

    if schedule["rows_with_balance"] == 0:
        # Nothing to compare against. Only worth a word once somebody has
        # actually filled the balance sheet in and it reports borrowings.
        if not sheet_started or on_sheet <= 0:
            return []
        return [
            Check(
                code=CODE_DEBT_BALANCE_ONE_SIDED,
                severity="info",
                sheet=DEBT_SCHEDULE,
                key="debts",
                message=(
                    f"The balance sheet reports {_money(on_sheet)} of interest-bearing "
                    f"debt; the debt schedule has no balances on it yet. A lender will "
                    f"ask for the schedule behind that figure."
                ),
                suggested_value=None,
                from_sheet=BALANCE_SHEET,
                from_key="interest_bearing_debt",
            )
        ]

    if on_sheet == 0:
        if not sheet_started:
            return []
        return [
            Check(
                code=CODE_DEBT_BALANCE_ONE_SIDED,
                severity="info",
                sheet=BALANCE_SHEET,
                key="interest_bearing_debt",
                message=(
                    f"The debt schedule lists {listed} totalling "
                    f"{_money(schedule['total_balance'])}; the balance sheet's "
                    f"interest-bearing debt lines are all empty."
                ),
                # Eight lines share this total; which loan sits on which is a
                # judgement, so nothing is offered for a single field.
                suggested_value=None,
                from_sheet=DEBT_SCHEDULE,
                from_key="total_balance",
            )
        ]

    if _within(
        schedule["total_balance"],
        on_sheet,
        floor=DEBT_TOLERANCE_FLOOR,
        share=DEBT_TOLERANCE_SHARE,
    ):
        return []
    return [
        Check(
            code=CODE_DEBT_BALANCE_MISMATCH,
            severity="warn",
            sheet=BALANCE_SHEET,
            key="interest_bearing_debt",
            message=(
                f"The debt schedule lists {listed} totalling "
                f"{_money(schedule['total_balance'])}; the balance sheet's "
                f"interest-bearing debt lines total {_money(on_sheet)} — a difference of "
                f"{_money(abs(schedule['total_balance'] - on_sheet))}. The two are "
                f"normally the same borrowings, seen twice."
            ),
            suggested_value=None,
            from_sheet=DEBT_SCHEDULE,
            from_key="total_balance",
        )
    ]


def _debt_service_check(bodies: Mapping[str, Any] | None) -> list[Check]:
    """Monthly debt service beside the P&L's interest line.

    Information, and never anything more. A payment is principal plus
    interest, so the two figures are not meant to match and a warning here
    would be wrong every time. It is shown because a desk reading a P&L wants
    the annualised payment beside the interest expense, and because a large
    gap between them is the shape of a merchant advance — worth a look, not an
    accusation.
    """
    ds = _body(bodies, DEBT_SCHEDULE)
    pl = _body(bodies, P_AND_L)
    if ds is None or pl is None or not debt_schedule_answered(ds):
        return []

    monthly = debt_totals(ds)["total_monthly_payment"]
    interest = _amount(((pl.get("sections") or {}).get("operating_expenses") or {}).get("interest"))
    if monthly <= 0 or interest <= 0:
        return []

    months = bss.months_between(
        (pl.get("header") or {}).get("period_start"),
        (pl.get("header") or {}).get("period_end"),
    )
    if months:
        span = f"the {_plural(months, 'month', 'months')} the profit and loss covers"
        annualised = monthly * months
    else:
        span = "a year"
        annualised = monthly * 12
    return [
        Check(
            code=CODE_DEBT_SERVICE_VS_INTEREST,
            severity="info",
            sheet=P_AND_L,
            key="sections.operating_expenses.interest",
            message=(
                f"The debt schedule's payments come to {_money(monthly)} a month, "
                f"{_money(annualised)} over {span}; the profit and loss "
                f"shows {_money(interest)} of interest. A payment is principal as well "
                f"as interest, so these are not meant to match — this is context, not a "
                f"discrepancy."
            ),
            # Never offered: filling interest from a payment total would be a
            # bookkeeping error, which is precisely why this is info.
            suggested_value=None,
            from_sheet=DEBT_SCHEDULE,
            from_key="total_monthly_payment",
        )
    ]


def checks(
    bodies: Mapping[str, Any] | None, *, edited_at: Mapping[str, Any] | None = None
) -> list[Check]:
    """Everything the four forms say differently, in a stable order.

    Pass whichever bodies the file has, keyed by sheet kind; a form that is
    absent, null, or has never been filled in simply takes no part. A file
    where nobody has typed anything comes back empty — a fresh file must not
    open covered in warnings.

    Order is declared, not sorted by severity: identity first, then net income,
    then the debt schedule against the balance sheet, then debt service beside
    interest. A caller that wants the warnings at the top sorts them; a caller
    rendering them in place gets the same order every time.
    """
    return [
        *_identity_checks(bodies, edited_at),
        *_net_income_check(bodies),
        *_debt_balance_check(bodies),
        *_debt_service_check(bodies),
    ]


def checks_payload(
    bodies: Mapping[str, Any] | None, *, edited_at: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """`checks`, as JSON for a response body."""
    return [check.as_dict() for check in checks(bodies, edited_at=edited_at)]
