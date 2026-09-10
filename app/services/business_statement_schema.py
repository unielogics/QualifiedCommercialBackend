"""The shape of a profit-and-loss statement and a balance sheet.

One definition each, the way `pfs_schema` declares Form 413, and everything
derives from it: the on-screen form (staff editor, borrower link, packet
page), the filed PDF, the `key_facts` the extractors read, the typed prompt
block that tells the model which fields to pull off an *uploaded* statement,
and the downloadable workbook the marketing site links to. A label edited here
is edited everywhere; a line added here is added everywhere.

**Why behaviour is a flag and not a label.** EBITDA is net income plus
interest, income taxes, depreciation and amortization — and nothing else. The
owner's template had one undifferentiated "Taxes" line, which is the single
biggest EBITDA error a template can invite: sales and payroll taxes are an
operating cost, income tax is an add-back, and a reader has to guess which the
line holds. Here `addback` is a property of the row, `taxes_and_licenses`
never carries it, `income_taxes` always does, and the arithmetic reads the
flag rather than the wording. `owner_comp` marks owner salaries as the
seller's-discretionary-earnings candidate — shown to the desk, never added.
`contra` marks a row subtracted within its own section.

**Equity is typed with a safety net.** A borrower who cannot break equity out
leaves the section blank and it is implied from assets and liabilities; one
who types it sees any difference, and nobody is blocked from submitting on a
sheet that does not balance. `balances` is a fact the desk reads, not a gate.

Money is kept as the string the person typed, parsed with `pfs_schema._amount`
so "$1,250" and "1,250" mean the same thing on both forms.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from app.services.pfs_schema import _amount

KINDS: tuple[str, ...] = ("p_and_l", "balance_sheet")

PL_SCHEMA_VERSION = "qc_pl.v1"
BS_SCHEMA_VERSION = "qc_bs.v1"


@dataclass(frozen=True)
class LineRow:
    key: str
    label: str
    #: Added back to net income when computing EBITDA. A property of the row,
    #: not of its label.
    addback: bool = False
    #: Subtracted within its own section (cost of goods sold, accumulated
    #: depreciation, owner draws, income taxes).
    contra: bool = False
    #: Owner compensation — the SDE add-back candidate. Shown, never added.
    owner_comp: bool = False
    #: Words, not money — "what does Other expenses cover". Never summed, never
    #: flagged, rendered as a text input. A property of the row, not of its
    #: key's suffix.
    text: bool = False
    hint: str | None = None


#: What a section *is* on the statement, so a reader rolls it up by kind
#: rather than by sniffing its key for "liabilit" or "_equity".
SectionRole = Literal["asset", "liability", "equity", "income", "expense", "other"]


@dataclass(frozen=True)
class HeaderField:
    key: str
    label: str
    #: "text" | "date" | "select"
    input: str = "text"
    options: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    rows: tuple[LineRow, ...]
    subtotal_key: str
    subtotal_label: str
    role: SectionRole = "other"


#: How a computed line is shown: dollars, a ratio to two places, or a plain
#: count. A ratio through a currency formatter is "$1.50" — which is why the
#: ratios were never rendered before this existed.
ComputedFormat = Literal["money", "ratio", "count"]


@dataclass(frozen=True)
class Computed:
    key: str
    label: str
    emphasis: bool = False
    format: ComputedFormat = "money"


# ---------------------------------------------------------------------------
# Profit and loss — qc_pl.v1
# ---------------------------------------------------------------------------

_BASIS = HeaderField("basis", "Accounting basis", "select", ("cash", "accrual"))

PL_HEADER: tuple[HeaderField, ...] = (
    HeaderField("business_name", "Business name"),
    HeaderField("period_start", "Period start", "date"),
    HeaderField("period_end", "Period end", "date"),
    _BASIS,
    HeaderField("prepared_by", "Prepared by"),
)

#: The owner's 19 expense lines, in the owner's order, with two corrections:
#: "Depreciation" becomes "Depreciation and amortization" (an add-back), and
#: "Taxes" becomes "Taxes and licenses" — an operating cost — with income tax
#: moved below the line where it is an add-back.
PL_SECTIONS: tuple[Section, ...] = (
    Section(
        "revenue",
        "Revenue",
        (
            LineRow("gross_revenue", "Gross revenue"),
            LineRow("cost_of_goods_sold", "Cost of goods sold", contra=True),
        ),
        "gross_profit",
        "Gross profit",
        role="income",
    ),
    Section(
        "operating_expenses",
        "Operating expenses",
        (
            LineRow("supplies", "Supplies"),
            LineRow(
                "depreciation_and_amortization", "Depreciation and amortization", addback=True
            ),
            LineRow("bank_charges", "Bank charges"),
            LineRow("payroll", "Payroll"),
            LineRow(
                "owner_salaries",
                "Owner salaries",
                owner_comp=True,
                hint="Salaries and guaranteed payments to owners, not staff payroll",
            ),
            LineRow("repairs", "Repairs and maintenance"),
            LineRow("marketing", "Marketing and advertising"),
            LineRow("commissions", "Commissions and fees"),
            LineRow("interest", "Interest", addback=True),
            LineRow("utilities", "Utilities"),
            LineRow(
                "taxes_and_licenses",
                "Taxes and licenses",
                hint="Sales, payroll and property taxes; not income tax",
            ),
            LineRow("office_equipment", "Office equipment and supplies"),
            LineRow("phone", "Phone and internet"),
            LineRow("rent", "Rent"),
            LineRow("insurance", "Insurance"),
            LineRow("shipping", "Shipping and delivery"),
            LineRow("security", "Security"),
            LineRow("professional_fees", "Professional fees / accountant"),
            LineRow("other", "Other expenses", hint="Describe what this covers in the notes"),
            # Free text beside "Other expenses". A text row: never summed.
            LineRow("other_description", "What the other expenses cover", text=True),
        ),
        "total_operating_expenses",
        "Total operating expenses",
        role="expense",
    ),
    Section(
        "below_the_line",
        "Other income and income taxes",
        (
            LineRow("other_income", "Other income", hint="Interest earned, grants, one-off gains"),
            LineRow("income_taxes", "Income taxes", addback=True, contra=True),
        ),
        "net_income",
        "Net income",
        role="other",
    ),
)

PL_COMPUTED: tuple[Computed, ...] = (
    Computed("gross_profit", "Gross profit"),
    Computed("total_operating_expenses", "Total operating expenses"),
    Computed("operating_income", "Operating income", emphasis=True),
    Computed("net_income", "Net income", emphasis=True),
    Computed("addbacks", "Add-backs (interest, income taxes, depreciation and amortization)"),
    Computed("ebitda", "EBITDA (memo)", emphasis=True),
    Computed("owner_compensation", "Owner compensation (add-back candidate, not added)"),
    Computed("months_covered", "Months covered", format="count"),
)


# ---------------------------------------------------------------------------
# Balance sheet — qc_bs.v1
# ---------------------------------------------------------------------------

BS_HEADER: tuple[HeaderField, ...] = (
    HeaderField("business_name", "Business name"),
    HeaderField("as_of_date", "As of", "date"),
    _BASIS,
    HeaderField("prepared_by", "Prepared by"),
)

BS_SECTIONS: tuple[Section, ...] = (
    Section(
        "current_assets",
        "Current assets",
        (
            LineRow("cash_in_bank", "Cash in bank"),
            LineRow("petty_cash", "Petty cash"),
            LineRow("accounts_receivable", "Accounts receivable"),
            LineRow("inventory", "Inventory, at cost"),
            LineRow("other_current_assets", "Other current assets"),
        ),
        "total_current_assets",
        "Total current assets",
        role="asset",
    ),
    Section(
        "fixed_assets",
        "Fixed assets",
        (
            LineRow("equipment", "Equipment"),
            LineRow("vehicles", "Vehicles"),
            LineRow("real_estate", "Real estate"),
            LineRow(
                "accumulated_depreciation",
                "Less: accumulated depreciation",
                contra=True,
                hint="Entered as a positive figure; it is subtracted",
            ),
        ),
        "total_fixed_assets",
        "Total fixed assets",
        role="asset",
    ),
    Section(
        "other_assets",
        "Other assets",
        (
            LineRow("trademarks_patents", "Trademarks and patents"),
            LineRow("security_deposits", "Security deposits"),
            LineRow("other_assets", "Other assets"),
        ),
        "total_other_assets",
        "Total other assets",
        role="asset",
    ),
    Section(
        "current_liabilities",
        "Current liabilities",
        (
            LineRow("credit_cards", "Credit cards"),
            LineRow("lines_of_credit", "Lines of credit"),
            LineRow("short_term_loans", "Short-term loans, including merchant cash advances"),
            LineRow("accounts_payable", "Accounts payable"),
            LineRow("current_portion_long_term_debt", "Current portion of long-term debt"),
            LineRow("other_current_liabilities", "Other current liabilities"),
        ),
        "total_current_liabilities",
        "Total current liabilities",
        role="liability",
    ),
    Section(
        "long_term_liabilities",
        "Long-term liabilities",
        (
            LineRow("long_term_loans", "Long-term loans"),
            LineRow("equipment_loans", "Equipment loans"),
            LineRow("vehicle_loans", "Vehicle loans"),
            LineRow("real_estate_loans", "Real estate loans"),
            LineRow("other_long_term_liabilities", "Other long-term liabilities"),
        ),
        "total_long_term_liabilities",
        "Total long-term liabilities",
        role="liability",
    ),
    Section(
        "equity",
        "Equity",
        (
            LineRow("owner_capital", "Owner capital"),
            LineRow("retained_earnings", "Retained earnings"),
            LineRow("current_period_net_income", "Current period net income"),
            LineRow(
                "owner_draws",
                "Less: owner draws and distributions",
                contra=True,
                hint="Entered as a positive figure; it is subtracted",
            ),
        ),
        "total_equity",
        "Total equity",
        role="equity",
    ),
)

BS_COMPUTED: tuple[Computed, ...] = (
    Computed("total_assets", "Total assets", emphasis=True),
    Computed("total_liabilities", "Total liabilities", emphasis=True),
    Computed("implied_equity", "Implied equity (assets less liabilities)"),
    Computed("total_equity", "Total equity", emphasis=True),
    Computed("total_liabilities_and_equity", "Total liabilities and equity", emphasis=True),
    Computed("imbalance", "Unreconciled difference"),
    Computed("working_capital", "Working capital"),
    Computed("current_ratio", "Current ratio", format="ratio"),
    Computed("debt_to_equity", "Debt to equity", format="ratio"),
)


# ---------------------------------------------------------------------------
# Shared arithmetic
# ---------------------------------------------------------------------------

_ZERO = Decimal("0")
_CENT = Decimal("0.01")


def _section_values(body: dict[str, Any], section: Section) -> dict[str, Any]:
    sections = body.get("sections") or {}
    values = sections.get(section.key) or {}
    return values if isinstance(values, dict) else {}


def _section_total(body: dict[str, Any], section: Section) -> Decimal:
    values = _section_values(body, section)
    total = _ZERO
    for row in section.rows:
        if row.text:
            # Words, never money — even when someone types a number there.
            continue
        amount = _amount(values.get(row.key))
        total = total - amount if row.contra else total + amount
    return total


def _section_is_blank(body: dict[str, Any], section: Section) -> bool:
    """Nothing typed on any money line — not even a zero."""
    values = _section_values(body, section)
    return all(
        str(values.get(row.key) or "").strip() == "" for row in section.rows if not row.text
    )


def _flagged_total(body: dict[str, Any], sections: tuple[Section, ...], flag: str) -> Decimal:
    total = _ZERO
    for section in sections:
        values = _section_values(body, section)
        for row in section.rows:
            if row.text:
                continue
            if getattr(row, flag):
                total += _amount(values.get(row.key))
    return total


def _header(body: dict[str, Any]) -> dict[str, Any]:
    header = body.get("header") or {}
    return header if isinstance(header, dict) else {}


def _iso_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator == 0:
        return None
    return (numerator / denominator).quantize(_CENT, rounding=ROUND_HALF_UP)


def _empty_body(version: str, header: tuple[HeaderField, ...], sections: tuple[Section, ...]) -> dict[str, Any]:
    return {
        "schema_version": version,
        "header": {field.key: None for field in header},
        "sections": {
            section.key: {row.key: None for row in section.rows} for section in sections
        },
        "notes": "",
    }


def months_between(start: Any, end: Any) -> int | None:
    """Inclusive calendar months between two ISO dates; None when either is
    blank, unparseable, or the period runs backwards."""
    first = _iso_date(start)
    last = _iso_date(end)
    if first is None or last is None or last < first:
        return None
    return (last.year - first.year) * 12 + (last.month - first.month) + 1


# ---------------------------------------------------------------------------
# Profit and loss
# ---------------------------------------------------------------------------


def pl_empty_body() -> dict[str, Any]:
    # `other_description` is seeded like any other line: it is a text row of
    # the operating-expenses section, and `_section_total` skips text rows.
    return _empty_body(PL_SCHEMA_VERSION, PL_HEADER, PL_SECTIONS)


def pl_totals(body: dict[str, Any]) -> dict[str, Any]:
    """Every derived P&L figure, computed one way for every caller.

    EBITDA reads the `addback` flag: net income plus interest, income taxes and
    depreciation and amortization. `taxes_and_licenses` is an operating cost
    and never comes back; `owner_salaries` is reported beside it as the SDE
    candidate and never added.
    """
    by_key = {section.key: section for section in PL_SECTIONS}
    gross_profit = _section_total(body, by_key["revenue"])
    total_opex = _section_total(body, by_key["operating_expenses"])
    operating_income = gross_profit - total_opex
    below = _section_values(body, by_key["below_the_line"])
    other_income = _amount(below.get("other_income"))
    income_taxes = _amount(below.get("income_taxes"))
    net_income = operating_income + other_income - income_taxes
    addbacks = _flagged_total(body, PL_SECTIONS, "addback")
    header = _header(body)
    return {
        "gross_profit": gross_profit,
        "total_operating_expenses": total_opex,
        "operating_income": operating_income,
        "other_income": other_income,
        "income_taxes": income_taxes,
        "net_income": net_income,
        "addbacks": addbacks,
        "ebitda": net_income + addbacks,
        "owner_compensation": _flagged_total(body, PL_SECTIONS, "owner_comp"),
        "months_covered": months_between(header.get("period_start"), header.get("period_end")),
    }


def pl_key_facts(body: dict[str, Any]) -> dict[str, Any]:
    """What the extractors and the prompt block read, by name. The shape is
    pinned by `PL_KEY_FACT_KEYS`; a filed form and an uploaded statement read
    with the typed block produce the same keys."""
    computed = pl_totals(body)
    header = _header(body)
    revenue = _section_values(body, PL_SECTIONS[0])
    opex = _section_values(body, PL_SECTIONS[1])

    def money(value: Any) -> float:
        return float(_amount(value))

    start = _iso_date(header.get("period_start"))
    end = _iso_date(header.get("period_end"))
    return {
        "business_name": _text(header.get("business_name")),
        "period_start": start.isoformat() if start else None,
        "period_end": end.isoformat() if end else None,
        "months_covered": computed["months_covered"],
        "basis": _text(header.get("basis")),
        "gross_revenue": money(revenue.get("gross_revenue")),
        "cost_of_goods_sold": money(revenue.get("cost_of_goods_sold")),
        "gross_profit": float(computed["gross_profit"]),
        "other_income": float(computed["other_income"]),
        "total_operating_expenses": float(computed["total_operating_expenses"]),
        "operating_income": float(computed["operating_income"]),
        "depreciation_and_amortization": money(opex.get("depreciation_and_amortization")),
        "interest": money(opex.get("interest")),
        "income_taxes": float(computed["income_taxes"]),
        "taxes_and_licenses": money(opex.get("taxes_and_licenses")),
        "owner_salaries": money(opex.get("owner_salaries")),
        "net_income": float(computed["net_income"]),
        "ebitda": float(computed["ebitda"]),
        "source_form": PL_SCHEMA_VERSION,
    }


# ---------------------------------------------------------------------------
# Balance sheet
# ---------------------------------------------------------------------------

#: |assets − liabilities − equity| within this is "balances": rounding and a
#: forgotten petty-cash line, not a sheet that is wrong.
BALANCE_TOLERANCE_FLOOR = Decimal("100")
BALANCE_TOLERANCE_SHARE = Decimal("0.01")


def bs_empty_body() -> dict[str, Any]:
    return _empty_body(BS_SCHEMA_VERSION, BS_HEADER, BS_SECTIONS)


def bs_totals(body: dict[str, Any]) -> dict[str, Any]:
    """Every subtotal, the identity, and the ratios the desk reads.

    Equity is the typed section when any of its lines was typed, and the
    implied figure (assets less liabilities) when the whole section is blank.
    `imbalance` is assets − liabilities − typed equity, so it is zero by
    construction when equity is implied. Ratios return None rather than divide
    by zero: a sheet with no current liabilities has no current ratio.
    """
    by_key = {section.key: section for section in BS_SECTIONS}
    tca = _section_total(body, by_key["current_assets"])
    tfa = _section_total(body, by_key["fixed_assets"])
    toa = _section_total(body, by_key["other_assets"])
    tcl = _section_total(body, by_key["current_liabilities"])
    tll = _section_total(body, by_key["long_term_liabilities"])
    total_assets = tca + tfa + toa
    total_liabilities = tcl + tll
    implied_equity = total_assets - total_liabilities

    equity_blank = _section_is_blank(body, by_key["equity"])
    typed_equity = _section_total(body, by_key["equity"])
    total_equity = implied_equity if equity_blank else typed_equity
    imbalance = _ZERO if equity_blank else total_assets - total_liabilities - typed_equity
    tolerance = max(BALANCE_TOLERANCE_FLOOR, abs(total_assets) * BALANCE_TOLERANCE_SHARE)

    current = _section_values(body, by_key["current_assets"])
    return {
        "total_current_assets": tca,
        "total_fixed_assets": tfa,
        "total_other_assets": toa,
        "total_assets": total_assets,
        "total_current_liabilities": tcl,
        "total_long_term_liabilities": tll,
        "total_liabilities": total_liabilities,
        "implied_equity": implied_equity,
        "equity_implied": equity_blank,
        "total_equity": total_equity,
        "total_liabilities_and_equity": total_liabilities + total_equity,
        "imbalance": imbalance,
        "balances": abs(imbalance) <= tolerance,
        "working_capital": tca - tcl,
        "current_ratio": _ratio(tca, tcl),
        "debt_to_equity": _ratio(total_liabilities, total_equity) if total_equity > 0 else None,
        "cash": _amount(current.get("cash_in_bank")) + _amount(current.get("petty_cash")),
    }


def bs_key_facts(body: dict[str, Any]) -> dict[str, Any]:
    computed = bs_totals(body)
    header = _header(body)
    by_key = {section.key: section for section in BS_SECTIONS}
    current = _section_values(body, by_key["current_assets"])
    current_liabilities = _section_values(body, by_key["current_liabilities"])
    as_of = _iso_date(header.get("as_of_date"))
    return {
        "business_name": _text(header.get("business_name")),
        "as_of_date": as_of.isoformat() if as_of else None,
        "basis": _text(header.get("basis")),
        "cash": float(computed["cash"]),
        "accounts_receivable": float(_amount(current.get("accounts_receivable"))),
        "inventory": float(_amount(current.get("inventory"))),
        "total_current_assets": float(computed["total_current_assets"]),
        "total_fixed_assets": float(computed["total_fixed_assets"]),
        "total_other_assets": float(computed["total_other_assets"]),
        "total_assets": float(computed["total_assets"]),
        "accounts_payable": float(_amount(current_liabilities.get("accounts_payable"))),
        "current_portion_long_term_debt": float(
            _amount(current_liabilities.get("current_portion_long_term_debt"))
        ),
        "total_current_liabilities": float(computed["total_current_liabilities"]),
        "total_long_term_liabilities": float(computed["total_long_term_liabilities"]),
        "total_liabilities": float(computed["total_liabilities"]),
        "total_equity": float(computed["total_equity"]),
        "balances": bool(computed["balances"]),
        "imbalance": float(computed["imbalance"]),
        "source_form": BS_SCHEMA_VERSION,
    }


# ---------------------------------------------------------------------------
# The bundle, by kind
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatementSchema:
    kind: str
    schema_version: str
    label: str
    #: The analyzer classification a filed copy is stored under, and the one
    #: an uploaded document of this kind is recognised by.
    classification: str
    header: tuple[HeaderField, ...]
    sections: tuple[Section, ...]
    computed: tuple[Computed, ...]
    empty_body: Callable[[], dict[str, Any]]
    totals: Callable[[dict[str, Any]], dict[str, Any]]
    key_facts: Callable[[dict[str, Any]], dict[str, Any]]

    def describe(self) -> dict[str, Any]:
        return describe(self.kind)


SCHEMA_FOR: dict[str, StatementSchema] = {
    "p_and_l": StatementSchema(
        kind="p_and_l",
        schema_version=PL_SCHEMA_VERSION,
        label="Profit and loss statement",
        classification="current_p_and_l",
        header=PL_HEADER,
        sections=PL_SECTIONS,
        computed=PL_COMPUTED,
        empty_body=pl_empty_body,
        totals=pl_totals,
        key_facts=pl_key_facts,
    ),
    "balance_sheet": StatementSchema(
        kind="balance_sheet",
        schema_version=BS_SCHEMA_VERSION,
        label="Balance sheet",
        classification="balance_sheet",
        header=BS_HEADER,
        sections=BS_SECTIONS,
        computed=BS_COMPUTED,
        empty_body=bs_empty_body,
        totals=bs_totals,
        key_facts=bs_key_facts,
    ),
}

#: Everything `key_facts` emits, in order, per kind.
KEY_FACT_KEYS: dict[str, tuple[str, ...]] = {
    "p_and_l": tuple(pl_key_facts(pl_empty_body()).keys()),
    "balance_sheet": tuple(bs_key_facts(bs_empty_body()).keys()),
}

#: The subset the model is asked for off an uploaded document: the lines the
#: document prints. Derived figures (months, EBITDA, whether it balances) and
#: the form marker are computed here, never requested — a model told to
#: compute EBITDA will invent an add-back.
_NOT_PROMPTED = {"months_covered", "ebitda", "balances", "imbalance", "source_form"}
PROMPT_KEYS: dict[str, tuple[str, ...]] = {
    kind: tuple(key for key in keys if key not in _NOT_PROMPTED)
    for kind, keys in KEY_FACT_KEYS.items()
}


def describe(kind: str) -> dict[str, Any]:
    """The field list, served so the browser renders rather than duplicates."""
    schema = SCHEMA_FOR[kind]
    return {
        "schema_version": schema.schema_version,
        "kind": kind,
        "header": [
            {
                "key": field.key,
                "label": field.label,
                "input": field.input,
                **({"options": list(field.options)} if field.options else {}),
            }
            for field in schema.header
        ],
        "sections": [
            {
                "key": section.key,
                "label": section.label,
                "role": section.role,
                "rows": [
                    {
                        "key": row.key,
                        "label": row.label,
                        "addback": row.addback,
                        "contra": row.contra,
                        "owner_comp": row.owner_comp,
                        "text": row.text,
                        "hint": row.hint,
                    }
                    for row in section.rows
                ],
                "subtotal": {"key": section.subtotal_key, "label": section.subtotal_label},
            }
            for section in schema.sections
        ],
        "computed": [
            {
                "key": item.key,
                "label": item.label,
                "emphasis": item.emphasis,
                "format": item.format,
            }
            for item in schema.computed
        ],
        "collects_ssn": False,
    }


def empty_body(kind: str) -> dict[str, Any]:
    return SCHEMA_FOR[kind].empty_body()


def totals(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    return SCHEMA_FOR[kind].totals(body)


def key_facts(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    return SCHEMA_FOR[kind].key_facts(body)


def period_label(kind: str, body: dict[str, Any]) -> str | None:
    """"Jan–Jun 2026", "FY 2025", or "as of 2026-06-30" — for a file label, a
    summary line, a PDF subtitle. None when the dates are blank."""
    header = _header(body)
    if kind == "balance_sheet":
        as_of = _iso_date(header.get("as_of_date"))
        return f"as of {as_of.isoformat()}" if as_of else None
    first = _iso_date(header.get("period_start"))
    last = _iso_date(header.get("period_end"))
    if first is None or last is None:
        return None
    if first.year == last.year:
        if first.month == 1 and last.month == 12:
            return f"FY {last.year}"
        if first.month == last.month:
            return f"{first.strftime('%b')} {last.year}"
        return f"{first.strftime('%b')}–{last.strftime('%b')} {last.year}"
    return f"{first.strftime('%b %Y')}–{last.strftime('%b %Y')}"
