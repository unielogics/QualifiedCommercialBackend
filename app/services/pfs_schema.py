"""The shape of a Personal Financial Statement, following SBA Form 413.

One definition, used by four things that previously disagreed or would have:
the totals the underwriting metrics read, the PDF we forward to a partner, the
validation on save, and the field list the browser renders.

**Why liquidity is a flag here and not a label.** The previous form decided
which assets counted as liquid by comparing the row's *display label* against a
frozen set of three strings, duplicated by hand in the frontend with a comment
asking the two to be kept in sync. `pfs_total_liquid_assets` gates programme
eligibility, so renaming a label — a change any designer would think was
cosmetic — silently moved the line between qualifying and not. Liquidity is a
property of the row, so it lives on the row.

Form 413 asks for far more than the old eight-assets-and-six-liabilities sheet:
the summary columns, four income lines, four contingent-liability lines, and
seven supporting schedules. The schedules are lists rather than fixed rows,
because a borrower has as many properties or notes as they have.

No SSN. Form 413 has the field, and we deliberately do not collect it: it is not
needed for the net-worth and liquidity arithmetic, a hard pull goes through the
separate credit-authorisation e-sign flow which also never collects it, and the
client-facing form is reachable through a link with no access code. Staff can
record it on an authenticated screen if a partner insists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

SCHEMA_VERSION = "sba413.v1"


@dataclass(frozen=True)
class SummaryRow:
    key: str
    label: str
    #: Counts toward `liquid_assets`. Cash, savings and marketable securities
    #: only — the same definition the AI classifier uses, so a typed form and a
    #: parsed upload mean the same thing by the word.
    liquid: bool = False
    #: The 413 cross-references several summary lines to a schedule below.
    schedule: str | None = None


ASSET_ROWS: tuple[SummaryRow, ...] = (
    SummaryRow("cash_on_hand", "Cash on hand and in banks", liquid=True),
    SummaryRow("savings_accounts", "Savings accounts", liquid=True),
    SummaryRow("ira_or_retirement", "IRA or other retirement account", schedule="retirement"),
    SummaryRow("accounts_receivable", "Accounts and notes receivable"),
    SummaryRow("life_insurance_cash_value", "Life insurance — cash surrender value only", schedule="life_insurance"),
    SummaryRow("stocks_and_bonds", "Stocks and bonds", liquid=True, schedule="stocks_and_bonds"),
    SummaryRow("real_estate", "Real estate", schedule="real_estate"),
    SummaryRow("automobiles", "Automobiles — present value"),
    SummaryRow("other_personal_property", "Other personal property", schedule="other_property"),
    SummaryRow("other_assets", "Other assets", schedule="other_property"),
)

LIABILITY_ROWS: tuple[SummaryRow, ...] = (
    SummaryRow("accounts_payable", "Accounts payable"),
    SummaryRow("notes_payable", "Notes payable to banks and others", schedule="notes_payable"),
    SummaryRow("installment_auto", "Installment account (auto)"),
    SummaryRow("installment_other", "Installment account (other)"),
    SummaryRow("loans_on_life_insurance", "Loans against life insurance", schedule="life_insurance"),
    SummaryRow("mortgages_on_real_estate", "Mortgages on real estate", schedule="real_estate"),
    SummaryRow("unpaid_taxes", "Unpaid taxes", schedule="unpaid_taxes"),
    SummaryRow("other_liabilities", "Other liabilities", schedule="other_liabilities"),
)

INCOME_ROWS: tuple[SummaryRow, ...] = (
    SummaryRow("salary", "Salary"),
    SummaryRow("net_investment_income", "Net investment income"),
    SummaryRow("real_estate_income", "Real estate income"),
    SummaryRow("other_income", "Other income"),
)

#: Section 1's right-hand column. Not liabilities on the balance sheet — they
#: are disclosures — so they are totalled separately and never folded into
#: `total_liabilities`.
CONTINGENT_ROWS: tuple[SummaryRow, ...] = (
    SummaryRow("as_endorser_or_comaker", "As endorser or co-maker"),
    SummaryRow("legal_claims", "Legal claims and judgments"),
    SummaryRow("provision_federal_tax", "Provision for federal income tax"),
    SummaryRow("other_special_debt", "Other special debt"),
)


@dataclass(frozen=True)
class Schedule:
    key: str
    label: str
    columns: tuple[str, ...]


#: Sections 2 to 8. Each is a list the borrower adds rows to.
SCHEDULES: tuple[Schedule, ...] = (
    Schedule("notes_payable", "Notes payable to banks and others", (
        "Name and address of noteholder", "Original balance", "Current balance",
        "Payment amount", "Frequency", "How secured or endorsed",
    )),
    Schedule("stocks_and_bonds", "Stocks and bonds", (
        "Number of shares", "Name of security", "Cost", "Market value", "Date of quotation",
    )),
    Schedule("real_estate", "Real estate owned", (
        "Property address", "Type", "Date purchased", "Original cost", "Present market value",
        "Mortgage balance", "Mortgage payment", "Status",
    )),
    Schedule("other_property", "Other personal property and other assets", (
        "Description", "Present value", "Amount owing", "Payment", "Terms",
    )),
    Schedule("unpaid_taxes", "Unpaid taxes", (
        "Description", "To whom payable", "Amount", "When due", "Property the lien attaches to",
    )),
    Schedule("other_liabilities", "Other liabilities", ("Description", "Amount")),
    Schedule("life_insurance", "Life insurance held", (
        "Face amount", "Cash surrender value", "Insurance company", "Beneficiary",
    )),
    Schedule("retirement", "Retirement accounts", ("Account type", "Institution", "Present value")),
)

ASSETS_BY_KEY = {row.key: row for row in ASSET_ROWS}
LIABILITIES_BY_KEY = {row.key: row for row in LIABILITY_ROWS}
SCHEDULES_BY_KEY = {schedule.key: schedule for schedule in SCHEDULES}
LIQUID_ASSET_KEYS = frozenset(row.key for row in ASSET_ROWS if row.liquid)


#: What people actually type into a money field. Decimal accepts none of these.
_MONEY_NOISE = re.compile(r"[,$\s]")


def _amount(value: Any) -> Decimal:
    """A money field from an untrusted body, as a number.

    Currency symbols, thousands separators and stray spaces are stripped before
    parsing. Without that, "$1,250" and "1,000" parse as nothing and get counted
    as zero — silently understating a borrower's assets on a form whose totals
    gate programme eligibility. People type commas into money fields; a total
    that quietly disagrees is worse than one that is obviously blank.

    Blank and genuinely unparseable still mean zero: a borrower leaving a line
    empty is the normal case, and a totals routine is the wrong place to reject
    a form. Validation belongs on save, where it can point at the field.
    """
    if value is None:
        return Decimal("0")
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    cleaned = _MONEY_NOISE.sub("", str(value))
    if not cleaned or cleaned in {"-", "."}:
        return Decimal("0")
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def empty_body() -> dict[str, Any]:
    """A blank 413, for a form opened for the first time."""
    return {
        "schema_version": SCHEMA_VERSION,
        "applicant": {"name": "", "business_phone": "", "home_address": "", "business_name": ""},
        "assets": {row.key: None for row in ASSET_ROWS},
        "liabilities": {row.key: None for row in LIABILITY_ROWS},
        "income": {row.key: None for row in INCOME_ROWS},
        "contingent": {row.key: None for row in CONTINGENT_ROWS},
        "schedules": {schedule.key: [] for schedule in SCHEDULES},
        "notes": "",
    }


def totals(body: dict[str, Any]) -> dict[str, Decimal]:
    """The derived numbers, computed one way for every caller.

    These are stored as columns on the statement and are what `key_facts` is
    built from, so the metric the screening logic reads never depends on
    re-walking JSONB.
    """
    assets = body.get("assets") or {}
    liabilities = body.get("liabilities") or {}
    income = body.get("income") or {}
    contingent = body.get("contingent") or {}

    total_assets = sum((_amount(assets.get(row.key)) for row in ASSET_ROWS), Decimal("0"))
    total_liabilities = sum(
        (_amount(liabilities.get(row.key)) for row in LIABILITY_ROWS), Decimal("0")
    )
    liquid_assets = sum(
        (_amount(assets.get(key)) for key in LIQUID_ASSET_KEYS), Decimal("0")
    )
    return {
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "net_worth": total_assets - total_liabilities,
        "liquid_assets": liquid_assets,
        "total_income": sum((_amount(income.get(row.key)) for row in INCOME_ROWS), Decimal("0")),
        "total_contingent": sum(
            (_amount(contingent.get(row.key)) for row in CONTINGENT_ROWS), Decimal("0")
        ),
    }


def key_facts(body: dict[str, Any], *, statement_date: str) -> dict[str, Any]:
    """The contract the rest of the system already reads.

    Deliberately the same five keys, same names, same meaning as the old
    `_pfs_key_facts`: `bucket_ai` rolls `liquid_assets` into
    `pfs_total_liquid_assets`, and the dealer screening logic gates programme
    eligibility on it. A richer form must not change what these words mean.
    """
    computed = totals(body)
    return {
        "statement_date": statement_date,
        "total_assets": float(computed["total_assets"]),
        "total_liabilities": float(computed["total_liabilities"]),
        "net_worth": float(computed["net_worth"]),
        "liquid_assets": float(computed["liquid_assets"]),
    }


def describe() -> dict[str, Any]:
    """The field list, for the browser to render from rather than duplicate.

    The old form hardcoded its labels and liquidity flags in the frontend and
    again in the backend. Serving them removes the second copy.
    """
    def _rows(rows: tuple[SummaryRow, ...]) -> list[dict[str, Any]]:
        return [
            {"key": row.key, "label": row.label, "liquid": row.liquid, "schedule": row.schedule}
            for row in rows
        ]

    return {
        "schema_version": SCHEMA_VERSION,
        "assets": _rows(ASSET_ROWS),
        "liabilities": _rows(LIABILITY_ROWS),
        "income": _rows(INCOME_ROWS),
        "contingent": _rows(CONTINGENT_ROWS),
        "schedules": [
            {"key": s.key, "label": s.label, "columns": list(s.columns)} for s in SCHEDULES
        ],
        "collects_ssn": False,
    }
