"""The financial cockpit: one deterministic read of a file's numbers.

The desk could only see a file's figures by generating the lender packet, and
the AI review tab mixes the numbers with the model's verdicts. This module
reads the latest analysis of every current evidence file on the profile (bank
statements, tax returns, profit-and-loss, balance sheet, debt schedule,
advance terms), the debt rows on the file and the loan terms on record, and
answers with month-by-month bank series, tax years, the expense lines a reader
can track, the debt service, a DSCR with its basis spelled out, and a
net-profit conclusion.

Nothing here calls a model. Every figure names its source so a person can
correct it where it came from (the file, the debt schedule form, the recorded
loan terms). Precedence follows the AI review so the two never disagree: a
filed tax return first, a profit-and-loss statement as the labelled fallback,
bank cash flow as a proxy that is always marked as one.
"""

from __future__ import annotations

import math
import re
import statistics
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_os.models import DealerDebt
from app.dealer_os.services.vendors import normalize_vendor
from app.models.application_profile import (
    ApplicationProfile,
    ApplicationProgramRequirementOverride,
    ApplicationProgramSelection,
    ApplicationRequirementState,
)
from app.models.production_package import ProductionTermSheet
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User
from app.services import application_programs as programs
from app.services import debt_types
from app.services.bucket_evidence import (
    analysis_classifications,
    effective_file_classification,
    statement_period_month,
)
from app.services.capital_readiness import classify_dscr
from app.services.payee_classification import (
    CARD_KINDS,
    DEBT_KINDS,
    KIND_LABELS,
    classify_credit,
    classify_payee,
    match_names,
)
from app.services.public_underwriting_packet_pdf import (
    extract_balance_sheet,
    extract_debt_schedule,
    extract_profit_and_loss,
    extract_tax_years,
)

REQUIRED_STATEMENT_MONTHS = 6
#: Months of statements before an average, an annualized figure or a trend is stated.
MIN_MONTHS_FOR_ESTIMATES = 3
#: Classifications whose key_facts.debts rows describe an advance or a loan.
ADVANCE_CLASSES = frozenset(
    {"debt_schedule", "advance_agreement", "mca_agreement", "payoff_letter"}
)
#: Payment cadence -> payments per month (a daily advance debits business days).
#: The same table the client's typed advance terms use (dealer_ai_intake._MCA_REMITS_PER_MONTH),
#: so a verbal breakdown and a typed one land on the same monthly figure.
PAYMENTS_PER_MONTH = {
    "daily": 21.0,
    "weekly": 4.33,
    "biweekly": 2.17,
    "bi-weekly": 2.17,
    "semimonthly": 2.0,
    "monthly": 1.0,
}
OVERDRAFT_TYPES = frozenset(
    {"nsf_fee", "overdraft_fee", "returned_item", "negative_balance", "other"}
)
OVERDRAFT_FEE_TYPES = frozenset({"nsf_fee", "overdraft_fee", "returned_item"})
STATEMENT_READ_FIELDS = ("recurring_debits", "recurring_credits", "overdraft_items")
#: A declared payment within this much of the statements' figure "matches".
RECONCILE_TOLERANCE_PCT = 15.0
MAX_RECURRING_PAYEES = 40
MCA_CATEGORIES = frozenset(
    {"mca", "merchant_cash_advance", "advance", "cash_advance", "revenue_based"}
)
DSCR_THRESHOLDS = {"acceptable": 1.0, "healthy": 1.25, "very_strong": 1.5}

_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_BANK_AMOUNTS = (
    "deposits",
    "withdrawals",
    "beginning_balance",
    "ending_balance",
    "average_balance",
    "low_balance",
)
_BANK_COUNTS = ("deposit_count", "nsf", "returned_checks", "negative_days")
_TAX_LINE_ALIASES: dict[str, tuple[str, ...]] = {
    "depreciation": (
        "depreciation",
        "depreciation_and_amortization",
        "depreciation_expense",
        "depreciation_current",
    ),
    "interest": ("interest", "interest_expense", "interest_deduction", "interest_paid"),
    "officer_compensation": (
        "officer_compensation",
        "compensation_of_officers",
        "officers_compensation",
        "guaranteed_payments_to_partners",
    ),
    "cost_of_goods_sold": ("cost_of_goods_sold", "cogs"),
    "salaries_and_wages": ("salaries_and_wages", "wages", "salaries"),
    "rent": ("rent", "rents"),
    "taxes_and_licenses": ("taxes_and_licenses",),
    "advertising": ("advertising",),
    "total_deductions": ("total_deductions", "total_expenses"),
}
_TAX_INCOME_BASES = (
    ("ordinary_business_income", "ordinary business income"),
    ("net_income", "net income"),
    ("net_income_per_books", "net income per books"),
    ("taxable_income", "taxable income"),
)
_PL_EXPENSE_LINES = (
    ("cost_of_goods_sold", "Cost of goods sold"),
    ("total_operating_expenses", "Operating expenses"),
    ("owner_salaries", "Owner salaries"),
    ("depreciation_and_amortization", "Depreciation and amortization"),
    ("interest", "Interest"),
    ("income_taxes", "Income taxes"),
    ("taxes_and_licenses", "Taxes and licenses"),
)
_TAX_EXPENSE_LINES = (
    ("cost_of_goods_sold", "Cost of goods sold"),
    ("officer_compensation", "Officer compensation"),
    ("salaries_and_wages", "Salaries and wages"),
    ("rent", "Rent"),
    ("interest", "Interest"),
    ("depreciation", "Depreciation"),
    ("taxes_and_licenses", "Taxes and licenses"),
    ("advertising", "Advertising"),
    ("total_deductions", "Total deductions"),
)
_BALANCE_SHEET_KEYS = (
    "file_id",
    "business_name",
    "as_of_date",
    "basis",
    "currency",
    "cash",
    "accounts_receivable",
    "inventory",
    "total_current_assets",
    "total_fixed_assets",
    "total_other_assets",
    "total_assets",
    "accounts_payable",
    "current_portion_long_term_debt",
    "total_current_liabilities",
    "total_long_term_liabilities",
    "total_liabilities",
    "total_equity",
)


# ---------------------------------------------------------------------------
# Small number helpers
# ---------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, Decimal):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = re.sub(r"[^0-9.\-]", "", text)
    if cleaned in {"", "-", ".", "-."}:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return -abs(number) if negative else number


def _int(value: Any) -> int | None:
    number = _num(value)
    return int(round(number)) if number is not None else None


def _round(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def _mean(values: Any) -> float | None:
    numbers = [v for v in values if v is not None]
    return sum(numbers) / len(numbers) if numbers else None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _pct_change(prior: float | None, current: float | None) -> float | None:
    if prior is None or current is None or prior == 0:
        return None
    return round((current - prior) / abs(prior) * 100, 1)


def _first_num(facts: dict[str, Any], aliases: tuple[str, ...]) -> float | None:
    for key in aliases:
        value = _num(facts.get(key))
        if value is not None:
            return value
    return None


def _iso_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime | date):
        return value.isoformat()[:10]
    text = str(value).strip()
    match = re.search(r"(19|20)\d{2}-\d{2}-\d{2}", text)
    return match.group(0) if match else (text[:10] or None)


def month_label(key: str) -> str:
    """'2026-05' -> 'May 2026'."""
    try:
        year, month = key.split("-")[:2]
        return f"{_MONTH_NAMES[int(month) - 1]} {year}"
    except (ValueError, IndexError):
        return key


def _period_label(start: str | None, end: str | None) -> str | None:
    if not start and not end:
        return None
    if start and end and start[:4] == end[:4]:
        return f"{month_label(start[:7]).split(' ')[0]}–{month_label(end[:7])}"
    parts = [month_label(value[:7]) for value in (start, end) if value]
    return "–".join(parts)


def _months_between(first: str, last: str) -> list[str]:
    year, month = int(first[:4]), int(first[5:7])
    stop_year, stop_month = int(last[:4]), int(last[5:7])
    keys: list[str] = []
    while (year, month) <= (stop_year, stop_month):
        keys.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            month = 1
            year += 1
    return keys


def _classes(item: dict[str, Any]) -> set[str]:
    classes = {str(c) for c in (item.get("classifications") or []) if c}
    if item.get("classification"):
        classes.add(str(item["classification"]))
    return classes


def _extractor_rows(analyses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per (file, classification) in the shape the packet extractors read."""
    rows: list[dict[str, Any]] = []
    for item in analyses:
        for classification in sorted(_classes(item)):
            rows.append(
                {
                    "file_id": item.get("file_id"),
                    "classification": classification,
                    "key_facts": item.get("key_facts") or {},
                }
            )
    return rows


def _accepted_files(analyses: list[dict[str, Any]]) -> set[str]:
    return {str(item.get("file_id")) for item in analyses if item.get("accepted")}


# ---------------------------------------------------------------------------
# Bank statements
# ---------------------------------------------------------------------------


def _statement_record(
    source: dict[str, Any], *, period_hint: str | None = None
) -> dict[str, Any] | None:
    month = statement_period_month(source.get("statement_period")) or period_hint
    if not month:
        return None
    withdrawals = _num(source.get("total_withdrawals_and_debits"))
    negatives = source.get("negative_balance_dates")
    negative_dates: list[str] = []
    if isinstance(negatives, list):
        negative_dates = sorted({d for d in (_iso_date(v) for v in negatives if v) if d})
        negative_days: int | None = len(negative_dates)
    else:
        negative_days = _int(negatives)
    items: list[dict[str, Any]] = []
    for raw in source.get("overdraft_items") or []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("type") or raw.get("kind") or "other").strip().lower()
        items.append(
            {
                "date": _iso_date(raw.get("date")),
                "description": str(raw.get("description") or "").strip()[:140],
                "amount": _num(raw.get("amount")),
                "type": kind if kind in OVERDRAFT_TYPES else "other",
            }
        )
    return {
        "month": month,
        "deposits": _num(source.get("total_deposits_and_credits")),
        "withdrawals": abs(withdrawals) if withdrawals is not None else None,
        "beginning_balance": _num(source.get("beginning_balance")),
        "ending_balance": _num(source.get("ending_balance")),
        "average_balance": _num(source.get("average_ledger_balance")),
        "low_balance": _num(source.get("low_daily_balance")),
        "deposit_count": _int(source.get("number_of_deposits")),
        "nsf": _int(source.get("nsf_or_overdraft_count")),
        "returned_checks": _int(source.get("returned_check_count")),
        "negative_days": negative_days,
        "negative_dates": negative_dates,
        "overdraft_items": items,
    }


def _finalize_month(month: dict[str, Any]) -> dict[str, Any]:
    deposits, withdrawals = month.get("deposits"), month.get("withdrawals")
    month["net"] = (
        _round(deposits - withdrawals) if deposits is not None and withdrawals is not None else None
    )
    month["outflow_ratio"] = _ratio(withdrawals, deposits)
    for field in _BANK_AMOUNTS:
        month[field] = _round(month.get(field))
    month["negative_dates"] = sorted(set(month.get("negative_dates") or []))
    if month["negative_dates"]:
        month["negative_days"] = len(month["negative_dates"])
    fees = sum(
        item["amount"]
        for item in month.get("overdraft_items") or []
        if item.get("amount") and item.get("type") in OVERDRAFT_FEE_TYPES
    )
    month["fees"] = _round(fees) if fees else None
    month.setdefault("label", month_label(month["month"]))
    return month


def _populated(record: dict[str, Any]) -> int:
    return sum(1 for key in _BANK_AMOUNTS + _BANK_COUNTS if record.get(key) is not None)


def _bank_stem(bank: str) -> str:
    """'JPMorgan Chase Bank, N.A.' -> 'jpmorgan'; 'Pinnacle Bank (Horizon 75)' -> 'pinnacle'.
    The reader prints a bank's name differently from statement to statement;
    the first word plus the last four digits is what stays the same."""
    words = [
        w
        for w in re.sub(r"[^a-z0-9 ]", " ", bank.casefold()).split()
        if w not in {"the", "bank", "of"}
    ]
    return words[0] if words else ""


#: Where a reader puts the other accounts a consolidated statement lists.
_SUB_ACCOUNT_LISTS = ("sub_accounts", "accounts", "accounts_summary")
_STATEMENT_AMOUNT_KEYS = (
    "beginning_balance",
    "ending_balance",
    "total_deposits_and_credits",
    "total_withdrawals_and_debits",
    "average_ledger_balance",
    "low_daily_balance",
)


def _last4(value: Any) -> str:
    """The last four digits of whatever the reader printed: "x4884",
    "Bus Plus Checking – …0865"."""
    return re.sub(r"\D", "", str(value or ""))[-4:]


def _entry_last4(entry: dict[str, Any]) -> str:
    return _last4(entry.get("account_last4") or entry.get("account_number"))


def _entry_label(entry: dict[str, Any]) -> str:
    return str(
        entry.get("account_label") or entry.get("account_name") or entry.get("account_type") or ""
    ).strip()


def _activity(entry: dict[str, Any]) -> float:
    return abs(_num(entry.get("total_deposits_and_credits")) or 0) + abs(
        _num(entry.get("total_withdrawals_and_debits")) or 0
    )


def _figures_present(entry: dict[str, Any]) -> int:
    return sum(1 for key in _STATEMENT_AMOUNT_KEYS if _num(entry.get(key)) is not None)


def _sub_accounts_of(row: dict[str, Any]) -> list[dict[str, Any]]:
    """The accounts a consolidated statement lists inside one row, if the
    reader put them there (sub_accounts, or its ad-hoc accounts lists)."""
    for field in _SUB_ACCOUNT_LISTS:
        entries = row.get(field)
        if isinstance(entries, list) and any(isinstance(e, dict) for e in entries):
            return [e for e in entries if isinstance(e, dict)]
    return []


def _primary_entry(entries: list[dict[str, Any]], preferred_last4: str) -> dict[str, Any] | None:
    """Of the accounts one statement lists, the statement's own: the one
    carrying the statement's digits, else the one with the activity."""
    if not entries:
        return None
    if preferred_last4:
        for entry in entries:
            if _entry_last4(entry) == preferred_last4:
                return entry
    return max(entries, key=lambda e: (_activity(e), _figures_present(e)))


def file_account_identity(facts: dict[str, Any]) -> tuple[str, str, str]:
    """The account a statement is FOR: key, label and last four digits.

    One statement file is one account. The digits are the statement's own
    (account_last4, or the primary account the reader named in words), never
    the other accounts a consolidated statement lists or the counterparties on
    its transfer lines. When the file's month rows all carry the same digits
    those are the account's; when the statement lists several accounts, the
    one with the activity is the statement's account, and an account the
    reader listed with no activity is not trusted to name the statement at
    all (it folds into the bank's known account instead).
    """
    bank = str(facts.get("bank") or "").strip()
    rows_raw = facts.get("months")
    rows = [m for m in rows_raw if isinstance(m, dict)] if isinstance(rows_raw, list) else []
    if not bank:
        bank = next((str(r.get("bank")).strip() for r in rows if r.get("bank")), "")
    last4 = (
        _last4(facts.get("account_last4"))
        or _last4(facts.get("primary_account_last4"))
        or _last4(facts.get("primary_account"))
    )
    if not last4:
        row_digits = {d for d in (_last4(r.get("account_last4")) for r in rows) if d}
        if len(row_digits) == 1:
            last4 = row_digits.pop()
        else:
            listed = [entry for source in (rows or [facts]) for entry in _sub_accounts_of(source)]
            if not listed and len(row_digits) > 1:
                listed = rows  # one "month" row per account of the same statement
            primary = _primary_entry(listed, "")
            if primary is not None and _activity(primary) > 0:
                last4 = _entry_last4(primary)
    stem = _bank_stem(bank)
    key = (
        f"{stem} ••{last4}" if last4 and stem else (f"••{last4}" if last4 else (stem or "account"))
    )
    label = " ".join(part for part in (bank, f"••{last4}" if last4 else "") if part) or "Account"
    return key, label, last4


def account_key_of(facts: Any) -> str:
    """The account a statement belongs to, as the cockpit names it."""
    return file_account_identity(facts if isinstance(facts, dict) else {})[0]


def bank_file_counted(analysis_payload: Any, counted: list[str] | None) -> bool:
    """Does a statement's account count on the file? Every account counts
    until the desk chooses; after that only the chosen keys do. A statement
    the reader could not pin to a last-four counts with its bank's only
    counted account, as the cockpit folds it."""
    if not counted:
        return True
    facts = analysis_payload.get("key_facts") if isinstance(analysis_payload, dict) else None
    key = account_key_of(facts)
    chosen = set(counted)
    if key in chosen:
        return True
    if "••" not in key:
        return len([k for k in chosen if k.startswith(f"{key} ••")]) == 1
    return False


def _file_month_records(item: dict[str, Any]) -> tuple[tuple[str, str, str], list[dict[str, Any]]]:
    """One statement file: its account identity and one record per month.

    A consolidated statement lists several accounts for the same month (the
    checking with the activity, a savings, empty share accounts); those never
    become accounts of their own. The statement's account gets the month,
    the other accounts ride along as `other_accounts`, and when the reader
    gave no figures for the statement's own account the combined totals it
    printed stand in.
    """
    facts = item.get("key_facts") or {}
    if not isinstance(facts, dict):
        return ("account", "Account", ""), []
    identity = file_account_identity(facts)
    _key, _label, last4 = identity
    rows_raw = facts.get("months")
    rows = (
        [m for m in rows_raw if isinstance(m, dict)]
        if isinstance(rows_raw, list) and rows_raw
        else [facts]
    )
    hint = item.get("period_hint") if len(rows) == 1 else None
    by_month: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for row in rows:
        month = (
            statement_period_month(row.get("statement_period"))
            or (statement_period_month(facts.get("statement_period")) if row is not facts else None)
            or hint
        )
        if not month:
            continue
        listed = _sub_accounts_of(row)
        for entry in listed or [row]:
            by_month.setdefault(month, []).append((entry, row))
    records: list[dict[str, Any]] = []
    for month, candidates in by_month.items():
        entries = [entry for entry, _row in candidates]
        primary = _primary_entry(entries, last4) if len(entries) > 1 else entries[0]
        row = next(r for entry, r in candidates if entry is primary)
        if primary is row:
            source = dict(row)
        else:
            source = {**row, **{k: v for k, v in primary.items() if v is not None}}
        totals = row.get("combined_totals")
        if (
            primary is not row
            and isinstance(totals, dict)
            and _activity(primary) == 0
            and _activity(totals) > 0
        ):
            source.update(
                {k: v for k, v in totals.items() if v is not None and k in _STATEMENT_AMOUNT_KEYS}
            )
            if (
                totals.get("previous_balance") is not None
                and totals.get("beginning_balance") is None
            ):
                source["beginning_balance"] = totals["previous_balance"]
        source["statement_period"] = row.get("statement_period") or facts.get("statement_period")
        record = _statement_record(source, period_hint=month)
        if record is None:
            continue
        record["other_accounts"] = [
            {
                "last4": _entry_last4(entry),
                "label": _entry_label(entry),
                "ending_balance": _num(entry.get("ending_balance")),
            }
            for entry in entries
            if entry is not primary and _entry_last4(entry)
        ]
        records.append(record)
    return identity, records


def resolve_alias(key: str, aliases: dict[str, str] | None) -> str:
    """Follow the desk's "same account" map to the canonical key (bounded)."""
    seen: set[str] = set()
    current = key
    while (
        aliases
        and current in aliases
        and aliases[current] not in seen
        and aliases[current] != current
    ):
        seen.add(current)
        current = aliases[current]
    return current


def bank_months(
    analyses: list[dict[str, Any]],
    counted: list[str] | None = None,
    aliases: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Month-by-month bank figures, the counted accounts on the file combined.

    One account contributes one record per month (the most populated when a
    statement was uploaded twice); months then sum across the counted
    accounts (every account until the desk chooses), so a business banking at
    two institutions reads as one position. A month is accepted when any
    statement behind it passed the AI evidence review.
    """
    per_account: dict[str, dict[str, dict[str, Any]]] = {}
    labels: dict[str, str] = {}
    members: dict[str, set[str]] = {}
    for item in analyses:
        if "bank_statement" not in _classes(item):
            continue
        (key, label, _digits), records = _file_month_records(item)
        if not records:
            continue
        canonical = resolve_alias(key, aliases)
        if canonical != key:
            labels.setdefault(canonical, label)
            members.setdefault(canonical, set()).add(key)
            key = canonical
        else:
            labels[key] = label
        members.setdefault(key, set()).add(key)
        for record in records:
            record["file_id"] = str(item.get("file_id") or "")
            record["accepted"] = bool(item.get("accepted"))
            slot = per_account.setdefault(key, {})
            prior = slot.get(record["month"])
            if (
                prior is None
                or _populated(record) > _populated(prior)
                or (
                    _populated(record) == _populated(prior)
                    and record["accepted"]
                    and not prior["accepted"]
                )
            ):
                slot[record["month"]] = record
    # A statement the reader could not pin to a last-four belongs to its bank's
    # only known account when there is exactly one; otherwise it stays apart.
    for orphan in [k for k in per_account if "••" not in k]:
        owners = [k for k in per_account if k.startswith(f"{orphan} ••")]
        if len(owners) != 1:
            continue
        target = per_account[owners[0]]
        for month, record in per_account.pop(orphan).items():
            prior = target.get(month)
            if prior is None or _populated(record) > _populated(prior):
                target[month] = record
        labels.pop(orphan, None)
        members.setdefault(owners[0], set()).update(members.pop(orphan, set()) or {orphan})
    counted_keys = {
        canonical
        for k in (counted or [])
        if (canonical := resolve_alias(k, aliases)) in per_account
    } or set(per_account)
    combined: dict[str, dict[str, Any]] = {}
    for key, months in per_account.items():
        if key not in counted_keys:
            continue
        for month, record in months.items():
            agg = combined.get(month)
            if agg is None:
                agg = {
                    "month": month,
                    "label": month_label(month),
                    "accounts": [],
                    "file_ids": [],
                    "accepted": False,
                    "negative_dates": [],
                    "overdraft_items": [],
                }
                for field in _BANK_AMOUNTS + _BANK_COUNTS:
                    agg[field] = None
                combined[month] = agg
            agg["accounts"].append(labels[key])
            if record["file_id"] and record["file_id"] not in agg["file_ids"]:
                agg["file_ids"].append(record["file_id"])
            agg["accepted"] = agg["accepted"] or record["accepted"]
            agg["negative_dates"].extend(record.get("negative_dates") or [])
            agg["overdraft_items"].extend(
                {**item, "account": labels[key]} for item in record.get("overdraft_items") or []
            )
            for field in _BANK_AMOUNTS + _BANK_COUNTS:
                if record.get(field) is not None:
                    agg[field] = (agg[field] or 0) + record[field]
    months_out = [_finalize_month(combined[key]) for key in sorted(combined)]
    for month in months_out:
        month["accounts"] = sorted(set(month["accounts"]))
    accounts = []
    for key, months in sorted(per_account.items(), key=lambda kv: labels[kv[0]]):
        series = []
        subs: dict[str, dict[str, Any]] = {}
        for month_key in sorted(months):
            record = dict(months[month_key])
            file_id = record.pop("file_id", None)
            for other in record.pop("other_accounts", None) or []:
                subs[other["last4"]] = {
                    **subs.get(other["last4"], {}),
                    **{k: v for k, v in other.items() if v not in (None, "")},
                }
            record["label"] = month_label(month_key)
            record["accounts"] = [labels[key]]
            record["file_ids"] = [file_id] if file_id else []
            record["overdraft_items"] = [
                {**item, "account": labels[key]} for item in record.get("overdraft_items") or []
            ]
            series.append(_finalize_month(record))
        accounts.append(
            {
                "key": key,
                "label": labels[key],
                "months": sorted(months.keys()),
                "accepted_months": sorted(m for m, r in months.items() if r["accepted"]),
                "series": series,
                "counted": key in counted_keys,
                "members": sorted(members.get(key) or {key}),
                # Accounts the statements list beside this one (a savings, empty
                # share accounts): seen, named, never accounts of their own.
                "sub_accounts": [subs[k] for k in sorted(subs)],
            }
        )
    return months_out, accounts


def deposit_trend_pct(values: list[float | None]) -> float | None:
    """Later months against earlier ones, as a percentage; None under two months."""
    numbers = [v for v in values if v is not None]
    if len(numbers) < 2:
        return None
    half = len(numbers) // 2 if len(numbers) >= 4 else 1
    first = _mean(numbers[:half])
    last = _mean(numbers[-half:])
    if not first:
        return None
    return round((last - first) / abs(first) * 100, 1)  # type: ignore[operator]


def bank_summary(months: list[dict[str, Any]]) -> dict[str, Any]:
    deposits = [m["deposits"] for m in months if m["deposits"] is not None]
    withdrawals = [m["withdrawals"] for m in months if m["withdrawals"] is not None]
    nets = [m["net"] for m in months if m["net"] is not None]
    enough = len(deposits) >= MIN_MONTHS_FOR_ESTIMATES
    avg_deposits = _mean(deposits)
    avg_withdrawals = _mean(withdrawals)
    avg_net = _mean(nets)
    volatility = None
    if len(deposits) >= MIN_MONTHS_FOR_ESTIMATES and avg_deposits:
        volatility = round(statistics.pstdev(deposits) / avg_deposits * 100, 1)
    by_deposits = [m for m in months if m["deposits"] is not None]
    highest = max(by_deposits, key=lambda m: m["deposits"]) if by_deposits else None
    lowest = min(by_deposits, key=lambda m: m["deposits"]) if by_deposits else None
    average_balances = [m["average_balance"] for m in months if m["average_balance"] is not None]
    ending_balances = [m["ending_balance"] for m in months if m["ending_balance"] is not None]
    lows = [m["low_balance"] for m in months if m["low_balance"] is not None]
    first = months[0] if months else None
    last = months[-1] if months else None
    keys = [m["month"] for m in months]
    gaps = [k for k in _months_between(keys[0], keys[-1]) if k not in keys] if keys else []
    return {
        "months": len(months),
        "accepted_months": sum(1 for m in months if m["accepted"]),
        "required_months": REQUIRED_STATEMENT_MONTHS,
        "first_month": keys[0] if keys else None,
        "last_month": keys[-1] if keys else None,
        "gaps": gaps,
        "avg_deposits": _round(avg_deposits),
        "avg_withdrawals": _round(avg_withdrawals),
        "avg_net": _round(avg_net),
        "annualized_deposits": _round(avg_deposits * 12)
        if enough and avg_deposits is not None
        else None,
        "annualized_withdrawals": _round(avg_withdrawals * 12)
        if enough and avg_withdrawals is not None
        else None,
        "annualized_net": _round(avg_net * 12) if enough and avg_net is not None else None,
        "deposit_trend_pct": deposit_trend_pct([m["deposits"] for m in months]),
        "deposit_volatility_pct": volatility,
        "highest_month": highest["month"] if highest else None,
        "lowest_month": lowest["month"] if lowest else None,
        "beginning_balance": first["beginning_balance"] if first else None,
        "ending_balance": last["ending_balance"] if last else None,
        "average_balance": _round(
            _mean(average_balances) if average_balances else _mean(ending_balances)
        ),
        "average_balance_basis": "average_ledger_balance"
        if average_balances
        else ("ending_balance" if ending_balances else None),
        "lowest_balance": _round(min(lows))
        if lows
        else (_round(min(ending_balances)) if ending_balances else None),
        "avg_deposit_count": _round(
            _mean([m["deposit_count"] for m in months if m["deposit_count"] is not None]), 1
        ),
        "nsf_total": sum(m["nsf"] or 0 for m in months),
        "returned_checks_total": sum(m["returned_checks"] or 0 for m in months),
        "negative_days_total": sum(m["negative_days"] or 0 for m in months),
        "avg_outflow_ratio": _ratio(avg_withdrawals, avg_deposits),
        "estimates_available": enough,
    }


# ---------------------------------------------------------------------------
# Tax returns, profit and loss, balance sheet
# ---------------------------------------------------------------------------


def tax_years(analyses: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    facts_by_file = {str(item.get("file_id")): item.get("key_facts") or {} for item in analyses}
    accepted = _accepted_files(analyses)
    years: list[dict[str, Any]] = []
    for year in extract_tax_years(_extractor_rows(analyses), limit=limit):
        facts = facts_by_file.get(str(year.get("file_id"))) or {}
        record = {
            key: year.get(key)
            for key in (
                "file_id",
                "year",
                "entity",
                "basis",
                "period_start",
                "period_end",
                "gross_receipts",
                "gross_income",
                "total_income",
                "ordinary_business_income",
                "net_income",
                "net_income_per_books",
                "taxable_income",
            )
        }
        record["accepted"] = str(year.get("file_id")) in accepted
        for key, aliases in _TAX_LINE_ALIASES.items():
            record[key] = _first_num(facts, aliases) if isinstance(facts, dict) else None
        income, basis = None, None
        for key, label in _TAX_INCOME_BASES:
            if record.get(key) is not None:
                income, basis = record[key], label
                break
        record["income"] = income
        record["income_basis"] = basis
        interest, depreciation = record.get("interest"), record.get("depreciation")
        record["ebitda"] = (
            _round(income + (interest or 0) + (depreciation or 0)) if income is not None else None
        )
        record["ebitda_complete"] = (
            income is not None and interest is not None and depreciation is not None
        )
        record["net_margin"] = _ratio(income, record.get("gross_receipts"))
        years.append(record)
    for index in range(1, len(years)):
        prior, current = years[index - 1], years[index]
        current["gross_receipts_change_pct"] = _pct_change(
            prior.get("gross_receipts"), current.get("gross_receipts")
        )
        current["income_change_pct"] = _pct_change(prior.get("income"), current.get("income"))
    if years:
        years[0].setdefault("gross_receipts_change_pct", None)
        years[0].setdefault("income_change_pct", None)
    return years


def profit_and_loss(analyses: list[dict[str, Any]]) -> dict[str, Any] | None:
    statement = extract_profit_and_loss(_extractor_rows(analyses))
    if not statement:
        return None
    months = statement.get("months_covered") or 0
    revenue = statement.get("gross_revenue")
    net_income = statement.get("net_income")
    opex = statement.get("total_operating_expenses")
    factor = 12 / months if months and 0 < months < 12 else 1.0
    out = dict(statement)
    out["accepted"] = str(statement.get("file_id")) in _accepted_files(analyses)
    out["period_label"] = _period_label(statement.get("period_start"), statement.get("period_end"))
    out["annualized_net_income"] = _round(net_income * factor) if net_income is not None else None
    out["annualized_operating_expenses"] = _round(opex * factor) if opex is not None else None
    out["gross_margin"] = _ratio(statement.get("gross_profit"), revenue)
    out["net_margin"] = (
        _ratio(net_income, revenue)
        if net_income is None or net_income >= 0
        else (round(net_income / revenue, 4) if revenue else None)
    )
    out["opex_ratio"] = _ratio(opex, revenue)
    out["cogs_ratio"] = _ratio(statement.get("cost_of_goods_sold"), revenue)
    out["statements"] = [
        {**s, "period_label": _period_label(s.get("period_start"), s.get("period_end"))}
        for s in statement.get("statements") or []
    ]
    return out


def balance_sheet(analyses: list[dict[str, Any]]) -> dict[str, Any] | None:
    sheet = extract_balance_sheet(_extractor_rows(analyses))
    if not sheet:
        return None
    out = {key: sheet.get(key) for key in _BALANCE_SHEET_KEYS}
    out["accepted"] = str(sheet.get("file_id")) in _accepted_files(analyses)
    out["current_ratio"] = _ratio(
        sheet.get("total_current_assets"), sheet.get("total_current_liabilities")
    )
    out["debt_to_equity"] = _ratio(sheet.get("total_liabilities"), sheet.get("total_equity"))
    return out


# ---------------------------------------------------------------------------
# Debts and debt service
# ---------------------------------------------------------------------------


def monthly_equivalent(amount: Any, frequency: Any) -> float | None:
    """A payment at its cadence as a monthly figure."""
    payment = _num(amount)
    if payment is None:
        return None
    cadence = str(frequency or "monthly").strip().lower()
    per_month = PAYMENTS_PER_MONTH.get(cadence)
    if per_month is None:
        return None
    return round(payment * per_month, 2)


#: The readiness requirement that stands for the debt schedule on every file.
DEBT_SCHEDULE_REQUIREMENT = "business_debt_schedule"

SCHEDULE_STATUS_LABELS = {
    "voided": "Voided: no debt on the business",
    "rows": "Received: rows on the schedule",
    "document": "Received: read off an uploaded schedule",
    "advance_terms": "Not received: advance terms read off documents",
    "received": "Received: awaiting review",
    "requested": "Requested from the borrower, not received",
    "missing": "Not requested yet",
}


def schedule_block(
    rows: list[dict[str, Any]],
    document: dict[str, Any] | None,
    documented: list[dict[str, Any]],
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Where the debt schedule stands on the file.

    The schedule is a standing part of every file. When nothing has arrived
    the block still says so, and says whether the borrower was asked, so the
    desk never mistakes an empty form for a business with no debt. Only the
    desk's own declaration (a voided schedule) means that, and it names who
    declared it and when.
    """
    state = state or {}
    voided = state.get("voided")
    requirement_status = state.get("requirement_status")
    if voided:
        status = "voided"
    elif rows:
        status = "rows"
    elif document is not None:
        status = "document"
    elif documented:
        status = "advance_terms"
    elif requirement_status in {"received_unverified", "verified"}:
        status = "received"
    elif requirement_status in {"requested", "stale", "failed"} or state.get("requested_at"):
        status = "requested"
    else:
        status = "missing"
    return {
        "status": status,
        "label": SCHEDULE_STATUS_LABELS[status],
        "rows": len(rows),
        "documented": len(documented),
        "has_document": document is not None,
        "voided": voided,
        "requested_at": state.get("requested_at"),
        "received_at": state.get("received_at"),
        "requirement_status": requirement_status,
        "requirement_present": bool(state.get("requirement_present")),
        "can_void": not voided and not rows and document is None,
    }


def schedule_notes(schedule: dict[str, Any], service: dict[str, Any]) -> list[str]:
    """What the cockpit says about the schedule's standing, beside the figures."""
    notes: list[str] = []
    voided = schedule.get("voided")
    if voided:
        who = f" by {voided['by']}" if voided.get("by") else ""
        when = f" on {voided['at'][:10]}" if voided.get("at") else ""
        notes.append(
            f"The debt schedule was voided{who}{when}: {voided.get('reason') or 'no debt on the business'}."
        )
        if service.get("declaration_conflict"):
            observed = service.get("observed_monthly") or 0
            notes.append(
                f"The statements still show ${observed:,.0f} a month going to lenders or funders, so the debt service keeps the observed figure; restore the schedule if the business does carry debt."
            )
    elif schedule["status"] == "missing":
        notes.append(
            "The debt schedule has not been requested on this file; every file carries one until the desk voids it as no debt on the business."
        )
    elif schedule["status"] == "requested" and service.get("source") in {"none", "bank_observed"}:
        notes.append(
            "The debt schedule was requested from the borrower and has not arrived; until it does the debt service leans on what the statements show."
        )
    return notes


def debt_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The debt schedule rows on the file (dos_debts), one dict per active row."""
    out: list[dict[str, Any]] = []
    for row in rows:
        monthly = _num(row.get("monthly_payment"))
        frequency = str(row.get("payment_frequency") or "").strip().lower() or None
        if monthly is None:
            monthly = monthly_equivalent(row.get("payment_amount"), frequency)
        category = str(row.get("category") or "loan").strip().lower()
        is_mca = (
            category in MCA_CATEGORIES
            or _num(row.get("factor_rate")) is not None
            or frequency in {"daily", "weekly"}
        )
        out.append(
            {
                "id": str(row.get("id") or ""),
                "lender": str(row.get("lender") or "").strip() or "Unnamed lender",
                "category": category,
                "is_mca": is_mca,
                "balance": _num(row.get("balance")),
                "monthly_payment": _round(monthly),
                "payment_amount": _num(row.get("payment_amount")),
                "payment_frequency": frequency,
                "rate": _num(row.get("rate")),
                "factor_rate": _num(row.get("factor_rate")),
                "term_months": _int(row.get("term_months")),
                "maturity_on": _iso_date(row.get("maturity_on")),
                "payoff_amount": _num(row.get("payoff_amount")),
                "origin": row.get("origin"),
                "count_in_dscr": bool(row.get("count_in_dscr", True)),
                "vendor_key": (str(row.get("vendor_key") or "").strip() or None),
                "source": "debt_schedule_row",
            }
        )
    return out


def documented_debts(analyses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Advances and loans read off documents (advance terms, payoff letters,
    an uploaded debt schedule), one row per funder; a funder named twice keeps
    its larger balance and payment rather than counting twice."""
    groups: dict[str, dict[str, Any]] = {}
    for item in analyses:
        if not (ADVANCE_CLASSES & _classes(item)):
            continue
        facts = item.get("key_facts") or {}
        rows = facts.get("debts") if isinstance(facts, dict) else None
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            lender = str(row.get("lender") or row.get("funder") or "").strip()
            key = re.sub(r"[^a-z0-9]", "", lender.casefold()) or f"row:{len(groups)}"
            frequency = (
                str(row.get("payment_frequency") or row.get("frequency") or "").strip().lower()
                or None
            )
            monthly = _num(row.get("monthly_payment"))
            if monthly is None:
                monthly = monthly_equivalent(
                    row.get("payment_amount") or row.get("payment"), frequency
                )
            kind = str(row.get("type") or row.get("kind") or "").lower()
            record = {
                "lender": lender or "Unnamed lender",
                "balance": _num(
                    row.get("current_balance")
                    if row.get("current_balance") is not None
                    else row.get("balance")
                ),
                "original_amount": _num(row.get("original_amount") or row.get("funded_amount")),
                "monthly_payment": _round(monthly),
                "payment_amount": _num(row.get("payment_amount") or row.get("payment")),
                "payment_frequency": frequency,
                "factor_rate": _num(row.get("factor_rate")),
                "maturity_on": _iso_date(row.get("maturity_date") or row.get("maturity_on")),
                "is_mca": _num(row.get("factor_rate")) is not None
                or frequency in {"daily", "weekly"}
                or "advance" in kind
                or "mca" in kind
                # The schedule's own type, off a form-filed or AI-read row.
                or debt_types.is_advance_type(row.get("debt_type") or row.get("category")),
                "file_id": str(item.get("file_id") or ""),
                "source": "document",
            }
            prior = groups.get(key)
            if prior is None:
                groups[key] = record
                continue
            for field in ("balance", "monthly_payment", "original_amount"):
                values = [v for v in (prior.get(field), record.get(field)) if v is not None]
                prior[field] = max(values) if values else None
            prior["is_mca"] = prior["is_mca"] or record["is_mca"]
            for field in ("payment_frequency", "factor_rate", "maturity_on"):
                prior[field] = prior.get(field) or record.get(field)
    return list(groups.values())


def debt_service(
    rows: list[dict[str, Any]],
    document: dict[str, Any] | None,
    documented: list[dict[str, Any]],
    recurring: dict[str, Any] | None = None,
    declared_none: bool = False,
) -> dict[str, Any]:
    """The monthly debt service and where it came from.

    Precedence: the debt schedule rows on the file (a person can toggle each
    one), then the total printed on an uploaded debt schedule, then the
    payments read off advance terms, then the debits the statements show to
    lenders and funders (cards excluded). The sources never add up — they
    describe the same obligations — so one wins and the others are shown
    beside it, with one exception borrowed from Capital OS: a statement debit
    to a lender that no schedule row names is real debt service the rows
    forgot, so it is added to them.
    """
    recurring = recurring or {}
    observed_monthly = recurring.get("observed_debt_service_monthly")
    unexplained_observed = 0.0
    if rows:
        for payee in recurring.get("payees") or []:
            if payee.get("is_debt") and not any(
                match_names(row["lender"], payee["payee"]) for row in rows
            ):
                unexplained_observed += payee.get("monthly_average") or 0
    unexplained_observed = round(unexplained_observed, 2)
    counted = [r for r in rows if r["count_in_dscr"] and r["monthly_payment"]]
    schedule_monthly = round(sum(r["monthly_payment"] for r in counted), 2) if counted else None
    document_monthly = _num(document.get("total_monthly_debt_service")) if document else None
    documented_monthly = (
        round(sum(r["monthly_payment"] for r in documented if r["monthly_payment"]), 2)
        if documented
        else None
    )
    if schedule_monthly and unexplained_observed:
        monthly, source = (
            round(schedule_monthly + unexplained_observed, 2),
            "debt_schedule_rows+bank_observed",
        )
    elif schedule_monthly:
        monthly, source = schedule_monthly, "debt_schedule_rows"
    elif document_monthly:
        monthly, source = document_monthly, "debt_schedule_document"
    elif documented_monthly:
        monthly, source = documented_monthly, "advance_terms"
    elif observed_monthly:
        monthly, source = observed_monthly, "bank_observed"
    elif declared_none:
        # The desk voided the schedule: the business carries no debt, and
        # nothing on file says otherwise.
        monthly, source = 0.0, "declared_none"
    else:
        monthly, source = None, "none"
    mca_rows = [r for r in rows if r["is_mca"]] if rows else []
    mca_documented = [r for r in documented if r["is_mca"]]
    mca_source_rows = mca_rows or mca_documented
    return {
        "monthly": _round(monthly),
        "annual": _round(monthly * 12) if monthly is not None else None,
        "source": source,
        "declared_none": declared_none,
        # The desk declared no debt while the statements show lender debits:
        # the observed figure wins and the declaration is questioned.
        "declaration_conflict": bool(declared_none and observed_monthly),
        "schedule_monthly": schedule_monthly,
        "schedule_balance": _round(sum(r["balance"] for r in rows if r["balance"]))
        if any(r["balance"] for r in rows)
        else None,
        "schedule_rows_counted": len(counted),
        "document_monthly": _round(document_monthly),
        "document_balance": _num(document.get("total_outstanding_balance")) if document else None,
        "documented_monthly": documented_monthly,
        "documented_balance": _round(sum(r["balance"] for r in documented if r["balance"]))
        if any(r["balance"] for r in documented)
        else None,
        "mca_count": len(mca_source_rows),
        "mca_monthly": _round(
            sum(r["monthly_payment"] for r in mca_source_rows if r["monthly_payment"])
        )
        if mca_source_rows
        else None,
        "mca_balance": _round(sum(r["balance"] for r in mca_source_rows if r["balance"]))
        if any(r["balance"] for r in mca_source_rows)
        else None,
        "observed_monthly": _round(observed_monthly),
        "observed_mca_monthly": recurring.get("observed_mca_monthly"),
        "observed_loan_monthly": recurring.get("observed_loan_monthly"),
        "observed_card_monthly": recurring.get("observed_card_monthly"),
        "unexplained_observed_monthly": unexplained_observed or None,
    }


def amortized_payment(principal: float, annual_rate_pct: float, months: int) -> float | None:
    if months <= 0 or principal <= 0:
        return None
    rate = annual_rate_pct / 100 / 12
    if rate <= 0:
        return round(principal / months, 2)
    return round(principal * rate / (1 - (1 + rate) ** (-months)), 2)


def proposed_payment(terms: dict[str, Any] | None) -> dict[str, Any] | None:
    """The payment the loan on record would add, from the recorded term sheet
    (its own monthly debt service) or amortized from amount, rate and term."""
    if not terms:
        return None
    monthly = _num(terms.get("monthly_payment"))
    amount, rate, months = (
        _num(terms.get("amount")),
        _num(terms.get("rate_pct")),
        _int(terms.get("term_months")),
    )
    if monthly is None and amount and months:
        monthly = amortized_payment(amount, rate or 0.0, months)
    if monthly is None:
        return None
    return {
        "source": terms.get("source") or "term_sheet",
        "label": terms.get("label"),
        "amount": amount,
        "rate_pct": rate,
        "term_months": months,
        "monthly_payment": _round(monthly),
        "annual_payment": _round(monthly * 12),
        # The structure travels with the terms: the schedule, the package and
        # the offering price on it (proposed_terms_block), and the plan link
        # reads the treatment.
        "structure": terms.get("structure"),
        "interest_only_months": int(terms.get("interest_only_months") or 0),
        "interest_only": bool(terms.get("interest_only")),
        "payment_stated": bool(terms.get("payment_stated")),
        "treatment": terms.get("treatment"),
        "retained_annual_debt_service": terms.get("retained_annual_debt_service"),
    }


# ---------------------------------------------------------------------------
# Recurring payments read off the statements
# ---------------------------------------------------------------------------


def _file_months(facts: dict[str, Any], period_hint: str | None) -> list[str]:
    rows = facts.get("months")
    months: list[str] = []
    if isinstance(rows, list) and rows:
        for row in rows:
            if isinstance(row, dict):
                month = statement_period_month(
                    row.get("statement_period")
                ) or statement_period_month(row.get("month"))
                if month and month not in months:
                    months.append(month)
    if not months:
        month = statement_period_month(facts.get("statement_period")) or period_hint
        if month:
            months.append(month)
    return months


def _statement_fields_read(item: dict[str, Any]) -> set[str]:
    """Which of the extraction fields the reader produced for this statement
    (at file level or inside any month)."""
    facts = item.get("key_facts") or {}
    if not isinstance(facts, dict):
        return set()
    found = {field for field in STATEMENT_READ_FIELDS if field in facts}
    rows = facts.get("months")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                found.update(field for field in STATEMENT_READ_FIELDS if field in row)
    return found


def _recurring_entries(
    item: dict[str, Any], field: str = "recurring_debits"
) -> tuple[list[tuple[list[str], dict[str, Any]]], list[str], bool]:
    """(entries as (months, row), the months the file covers, whether the
    reader produced `field` at all). A per-month list beats a file-level one;
    a file-level list on a multi-month statement is spread over every month
    the file covers."""
    facts = item.get("key_facts") or {}
    if not isinstance(facts, dict):
        return [], [], False
    months_all = _file_months(facts, item.get("period_hint"))
    entries: list[tuple[list[str], dict[str, Any]]] = []
    read_any = False
    rows = facts.get("months")
    if isinstance(rows, list) and rows:
        for row in rows:
            if not isinstance(row, dict) or field not in row:
                continue
            read_any = True
            month = statement_period_month(row.get("statement_period")) or statement_period_month(
                row.get("month")
            )
            for entry in row.get(field) or []:
                if isinstance(entry, dict):
                    entries.append(([month] if month else months_all, entry))
    if not read_any and field in facts:
        read_any = True
        for entry in facts.get(field) or []:
            if isinstance(entry, dict):
                entries.append((months_all, entry))
    return entries, months_all, read_any


def _mode(values: list[str]) -> str | None:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return max(counts, key=lambda k: (counts[k], k)) if counts else None


def recurring_payments(
    analyses: list[dict[str, Any]], *, field: str = "recurring_debits", direction: int = -1
) -> dict[str, Any]:
    """Every payee (or payer, for credits) the statements move money to or
    from repeatedly, rolled up across months and classified, with the amount
    seen each month, the account it ran through, the months the reader
    covered and the statements that still need a re-read."""
    groups: dict[str, dict[str, Any]] = {}
    months_read: set[str] = set()
    files_need_reread: list[str] = []
    for item in analyses:
        if "bank_statement" not in _classes(item):
            continue
        entries, months_all, read_any = _recurring_entries(item, field)
        if not _statement_fields_read(item) >= set(STATEMENT_READ_FIELDS) and item.get("file_id"):
            files_need_reread.append(str(item["file_id"]))
        if not read_any:
            continue
        months_read.update(months_all)
        facts = item.get("key_facts") or {}
        _account_key, account_label, _digits = file_account_identity(
            facts if isinstance(facts, dict) else {}
        )
        for months, entry in entries:
            payee = str(
                entry.get("payee") or entry.get("payer") or entry.get("description") or ""
            ).strip()
            key = normalize_vendor(payee)
            if not key:
                continue
            occurrences = (
                _int(
                    entry.get("occurrences")
                    if entry.get("occurrences") is not None
                    else entry.get("count")
                )
                or 0
            )
            total = _num(
                entry.get("total_amount")
                if entry.get("total_amount") is not None
                else entry.get("total")
            )
            average = _num(
                entry.get("average_amount")
                if entry.get("average_amount") is not None
                else entry.get("amount")
            )
            if total is None and average is not None and occurrences:
                total = average * occurrences
            if total is None:
                continue
            total = abs(total)
            group = groups.setdefault(
                key,
                {
                    "key": key,
                    "payee": payee,
                    "occurrences": 0,
                    "total": 0.0,
                    "months": set(),
                    "monthly": {},
                    "accounts": set(),
                    "cadences": [],
                    "same_amounts": [],
                    "reader_kinds": [],
                    "first_date": None,
                    "last_date": None,
                    "file_ids": [],
                    "accepted": False,
                },
            )
            group["occurrences"] += occurrences
            group["total"] += total
            present = [m for m in months if m]
            group["months"].update(present)
            for m in present:
                group["monthly"][m] = group["monthly"].get(m, 0.0) + total / len(present)
            group["accounts"].add(account_label)
            cadence = str(entry.get("cadence") or "").strip().lower()
            if cadence:
                group["cadences"].append(cadence)
            if isinstance(entry.get("same_amount"), bool):
                group["same_amounts"].append(entry["same_amount"])
            if entry.get("kind_guess") or entry.get("kind"):
                group["reader_kinds"].append(str(entry.get("kind_guess") or entry.get("kind")))
            first, last = _iso_date(entry.get("first_date")), _iso_date(entry.get("last_date"))
            if first and (group["first_date"] is None or first < group["first_date"]):
                group["first_date"] = first
            if last and (group["last_date"] is None or last > group["last_date"]):
                group["last_date"] = last
            if item.get("file_id") and str(item["file_id"]) not in group["file_ids"]:
                group["file_ids"].append(str(item["file_id"]))
            group["accepted"] = group["accepted"] or bool(item.get("accepted"))
    span = len(months_read) or 1
    payees: list[dict[str, Any]] = []
    for group in groups.values():
        cadence = _mode(group["cadences"])
        same_amount: bool | None = None
        if group["same_amounts"]:
            same_amount = all(group["same_amounts"])
        if direction < 0:
            kind, rationale = classify_payee(
                group["payee"],
                cadence=cadence,
                same_amount=same_amount,
                reader_kind=_mode(group["reader_kinds"]),
            )
        else:
            kind, rationale = classify_credit(
                group["payee"], reader_kind=_mode(group["reader_kinds"])
            )
        occurrences = group["occurrences"]
        payees.append(
            {
                "key": group["key"],
                "payee": group["payee"],
                "kind": kind,
                "kind_label": KIND_LABELS.get(kind, kind.replace("_", " ").capitalize()),
                "rationale": rationale,
                "cadence": cadence,
                "same_amount": same_amount,
                "occurrences": occurrences,
                "total": _round(group["total"]),
                "average_amount": _round(group["total"] / occurrences) if occurrences else None,
                "monthly_average": _round(group["total"] / span),
                "monthly": {m: _round(v) for m, v in sorted(group["monthly"].items())},
                "months": sorted(group["months"]),
                "months_seen": len(group["months"]),
                "accounts": sorted(group["accounts"]),
                "first_date": group["first_date"],
                "last_date": group["last_date"],
                "file_ids": group["file_ids"],
                "accepted": group["accepted"],
                "is_debt": direction < 0 and kind in DEBT_KINDS,
                "is_card": direction < 0 and kind in CARD_KINDS,
            }
        )
    payees.sort(key=lambda p: (not p["is_debt"], -(p["monthly_average"] or 0)))
    payees = payees[:MAX_RECURRING_PAYEES]
    debt = [p for p in payees if p["is_debt"]]
    mca = [p for p in debt if p["kind"] == "merchant_cash_advance"]
    loans = [p for p in debt if p["kind"] != "merchant_cash_advance"]
    cards = [p for p in payees if p["is_card"]]
    return {
        "payees": payees,
        "months_read": sorted(months_read),
        "files_need_reread": sorted(set(files_need_reread)),
        "total_monthly": _round(sum(p["monthly_average"] or 0 for p in payees)) if payees else None,
        "observed_debt_service_monthly": _round(sum(p["monthly_average"] or 0 for p in debt))
        if debt
        else None,
        "observed_mca_monthly": _round(sum(p["monthly_average"] or 0 for p in mca))
        if mca
        else None,
        "observed_loan_monthly": _round(sum(p["monthly_average"] or 0 for p in loans))
        if loans
        else None,
        "observed_card_monthly": _round(sum(p["monthly_average"] or 0 for p in cards))
        if cards
        else None,
    }


def overdrafts_block(months: list[dict[str, Any]]) -> dict[str, Any]:
    """Negative-balance days, NSF or overdraft items and their fees, by month."""
    by_month = []
    items: list[dict[str, Any]] = []
    for month in months:
        month_items = [
            {**item, "month": month["month"]} for item in month.get("overdraft_items") or []
        ]
        items.extend(month_items)
        by_month.append(
            {
                "month": month["month"],
                "label": month["label"],
                "negative_dates": list(month.get("negative_dates") or []),
                "negative_days": month.get("negative_days") or 0,
                "nsf": month.get("nsf") or 0,
                "returned_checks": month.get("returned_checks") or 0,
                "low_balance": month.get("low_balance"),
                "ending_balance": month.get("ending_balance"),
                "fees": month.get("fees"),
                "items": month_items,
            }
        )
    negative_months = [
        m["month"]
        for m in by_month
        if m["negative_days"] or (m["low_balance"] is not None and m["low_balance"] < 0)
    ]
    return {
        "by_month": by_month,
        "items": items,
        "negative_days_total": sum(m["negative_days"] for m in by_month),
        "negative_months": negative_months,
        "nsf_total": sum(m["nsf"] for m in by_month),
        "returned_total": sum(m["returned_checks"] for m in by_month),
        "fees_total": _round(sum(m["fees"] or 0 for m in by_month)) or None,
        "lowest_balance": min(
            (m["low_balance"] for m in by_month if m["low_balance"] is not None), default=None
        ),
    }


def _pct_delta(declared: float | None, observed: float | None) -> float | None:
    if declared is None or observed is None or observed == 0:
        return None
    return round((declared - observed) / abs(observed) * 100, 1)


def reconcile_advances(
    rows: list[dict[str, Any]],
    documented: list[dict[str, Any]],
    recurring: dict[str, Any],
) -> dict[str, Any]:
    """Each schedule row against the contracts the AI read and the debits the
    statements show: matches, differs, not seen. Observed debt payees that no
    row or contract explains come back as suggestions."""
    months_read = recurring.get("months_read") or []
    debt_payees = [p for p in recurring.get("payees") or [] if p["is_debt"] or p["is_card"]]
    used_documented: set[int] = set()
    used_observed: set[str] = set()
    out_rows: list[dict[str, Any]] = []
    for row in rows:
        doc_index = next(
            (
                i
                for i, d in enumerate(documented)
                if i not in used_documented and match_names(row["lender"], d["lender"])
            ),
            None,
        )
        doc = documented[doc_index] if doc_index is not None else None
        if doc_index is not None:
            used_documented.add(doc_index)
        obs = None
        vendor_key = row.get("vendor_key")
        if vendor_key and vendor_key not in used_observed:
            obs = next((p for p in debt_payees if p["key"] == vendor_key), None)
        if obs is None:
            obs = next(
                (
                    p
                    for p in debt_payees
                    if p["key"] not in used_observed and match_names(row["lender"], p["payee"])
                ),
                None,
            )
        if obs is not None:
            used_observed.add(obs["key"])
        observed_delta = _pct_delta(
            row.get("monthly_payment"), obs["monthly_average"] if obs else None
        )
        if not months_read:
            status = "no_statements_read"
        elif obs is None:
            status = "not_seen"
        elif observed_delta is None:
            status = "seen"
        elif abs(observed_delta) <= RECONCILE_TOLERANCE_PCT:
            status = "matches"
        else:
            status = "differs"
        out_rows.append(
            {
                "row_id": row.get("id"),
                "lender": row["lender"],
                "is_mca": row.get("is_mca", False),
                "declared_monthly": row.get("monthly_payment"),
                "declared_balance": row.get("balance"),
                "documented": {
                    "lender": doc["lender"],
                    "balance": doc.get("balance"),
                    "monthly_payment": doc.get("monthly_payment"),
                    "payment_frequency": doc.get("payment_frequency"),
                }
                if doc
                else None,
                "documented_monthly_delta_pct": _pct_delta(
                    row.get("monthly_payment"), doc.get("monthly_payment") if doc else None
                ),
                "documented_balance_delta_pct": _pct_delta(
                    row.get("balance"), doc.get("balance") if doc else None
                ),
                "observed": {
                    "payee": obs["payee"],
                    "kind": obs["kind"],
                    "cadence": obs["cadence"],
                    "average_amount": obs["average_amount"],
                    "monthly_average": obs["monthly_average"],
                    "months_seen": obs["months_seen"],
                }
                if obs
                else None,
                "observed_delta_pct": observed_delta,
                "status": status,
            }
        )
    unmatched_observed = [
        p
        for p in debt_payees
        if p["key"] not in used_observed
        and p["is_debt"]
        and not any(match_names(p["payee"], d["lender"]) for d in documented)
    ]
    unmatched_documented = [d for i, d in enumerate(documented) if i not in used_documented]
    return {
        "rows": out_rows,
        "unmatched_observed": unmatched_observed,
        "unmatched_documented": unmatched_documented,
        "months_read": len(months_read),
        "matches": sum(1 for r in out_rows if r["status"] == "matches"),
        "differs": sum(1 for r in out_rows if r["status"] == "differs"),
        "not_seen": sum(1 for r in out_rows if r["status"] == "not_seen"),
    }


# ---------------------------------------------------------------------------
# DSCR and net profit
# ---------------------------------------------------------------------------


def dscr_status(value: float | None) -> str:
    if value is None:
        return "unavailable"
    return classify_dscr(Decimal(str(value)))


def _dscr_candidate(
    key: str,
    label: str,
    *,
    numerator: float | None,
    numerator_label: str,
    period: str | None,
    annual_debt_service: float | None,
    proposed_annual: float | None,
    confidence: str,
    note: str | None = None,
) -> dict[str, Any]:
    value = (
        round(numerator / annual_debt_service, 2)
        if numerator is not None and annual_debt_service
        else None
    )
    pro_forma = None
    if numerator is not None and proposed_annual:
        pro_forma = round(numerator / ((annual_debt_service or 0) + proposed_annual), 2)
    return {
        "key": key,
        "label": label,
        "numerator": _round(numerator),
        "numerator_label": numerator_label,
        "period": period,
        "annual_debt_service": _round(annual_debt_service),
        "value": value,
        "status": dscr_status(value),
        "pro_forma_value": pro_forma,
        "pro_forma_status": dscr_status(pro_forma),
        "confidence": confidence,
        "note": note,
    }


def dscr_block(
    years: list[dict[str, Any]],
    statement: dict[str, Any] | None,
    bank: dict[str, Any],
    service: dict[str, Any],
    proposed: dict[str, Any] | None,
) -> dict[str, Any]:
    annual = service.get("annual")
    proposed_annual = proposed.get("annual_payment") if proposed else None
    candidates: list[dict[str, Any]] = []
    latest_tax = next((y for y in reversed(years) if y.get("ebitda") is not None), None)
    if latest_tax:
        candidates.append(
            _dscr_candidate(
                "tax_return",
                f"Tax return {latest_tax.get('year')}",
                numerator=latest_tax["ebitda"],
                numerator_label=f"EBITDA: {latest_tax.get('income_basis')} + interest + depreciation",
                period=str(latest_tax.get("year") or ""),
                annual_debt_service=annual,
                proposed_annual=proposed_annual,
                confidence="filed",
                note=None
                if latest_tax.get("ebitda_complete")
                else "Interest or depreciation was not read off the return; EBITDA may be understated.",
            )
        )
    if statement and statement.get("annualized_ebitda") is not None:
        candidates.append(
            _dscr_candidate(
                "profit_and_loss",
                "Profit and loss",
                numerator=statement["annualized_ebitda"],
                numerator_label="EBITDA annualized from the statement",
                period=statement.get("period_label"),
                annual_debt_service=annual,
                proposed_annual=proposed_annual,
                confidence="statement" if statement.get("typed") else "statement_unverified",
            )
        )
    if bank.get("avg_net") is not None and bank.get("estimates_available"):
        monthly_service = service.get("monthly") or 0
        candidates.append(
            _dscr_candidate(
                "bank_cash_flow",
                "Bank cash flow",
                numerator=(bank["avg_net"] + monthly_service) * 12,
                numerator_label="Cash retained plus debt paid, twelve times the monthly average",
                period=f"{month_label(bank['first_month'])}–{month_label(bank['last_month'])}"
                if bank.get("first_month")
                else None,
                annual_debt_service=annual,
                proposed_annual=proposed_annual,
                confidence="proxy",
                note="Every outflow counts as spend, including owner draws and transfers; read this as a floor, not an income statement.",
            )
        )
    headline = next((c for c in candidates if c["value"] is not None), None)
    pro_forma_headline = next((c for c in candidates if c["pro_forma_value"] is not None), None)
    reason = None
    if headline is None:
        if not candidates:
            reason = "No income figure yet: a tax return, a profit-and-loss statement or three months of bank statements gives one."
        elif not annual:
            reason = (
                "No debt to cover: the desk voided the debt schedule as no debt on the business, so the coverage is the proposed payment alone."
                if service.get("source") == "declared_none"
                else "No debt service on file: add the debt schedule or upload one to compute the coverage."
            )
    return {
        "no_debt": service.get("source") == "declared_none",
        "candidates": candidates,
        "headline": headline,
        "pro_forma_headline": pro_forma_headline,
        "annual_debt_service": _round(annual),
        "debt_service_source": service.get("source"),
        "proposed": proposed,
        "thresholds": dict(DSCR_THRESHOLDS),
        "reason": reason,
    }


def net_profit_block(
    years: list[dict[str, Any]], statement: dict[str, Any] | None, bank: dict[str, Any]
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    latest_tax = next((y for y in reversed(years) if y.get("income") is not None), None)
    if latest_tax:
        candidates.append(
            {
                "key": "tax_return",
                "label": f"Tax return {latest_tax.get('year')}",
                "value": latest_tax["income"],
                "basis": latest_tax.get("income_basis"),
                "period": str(latest_tax.get("year") or ""),
                "revenue": latest_tax.get("gross_receipts"),
                "margin": _ratio(latest_tax["income"], latest_tax.get("gross_receipts"))
                if latest_tax["income"] >= 0
                else (
                    round(latest_tax["income"] / latest_tax["gross_receipts"], 4)
                    if latest_tax.get("gross_receipts")
                    else None
                ),
                "confidence": "filed",
            }
        )
    if statement and statement.get("annualized_net_income") is not None:
        months = statement.get("months_covered") or 0
        candidates.append(
            {
                "key": "profit_and_loss",
                "label": "Profit and loss" + (" (annualized)" if 0 < months < 12 else ""),
                "value": statement["annualized_net_income"],
                "basis": "net income"
                + (f" over {months} months, annualized" if 0 < months < 12 else ""),
                "period": statement.get("period_label"),
                "revenue": statement.get("annualized_revenue"),
                "margin": statement.get("net_margin"),
                "confidence": "statement" if statement.get("typed") else "statement_unverified",
            }
        )
    if bank.get("annualized_net") is not None:
        candidates.append(
            {
                "key": "bank_cash_flow",
                "label": "Bank cash retained (annualized)",
                "value": bank["annualized_net"],
                "basis": "deposits minus withdrawals, twelve times the monthly average",
                "period": f"{month_label(bank['first_month'])}–{month_label(bank['last_month'])}"
                if bank.get("first_month")
                else None,
                "revenue": bank.get("annualized_deposits"),
                "margin": round(bank["annualized_net"] / bank["annualized_deposits"], 4)
                if bank.get("annualized_deposits")
                else None,
                "confidence": "proxy",
            }
        )
    conclusion = candidates[0] if candidates else None
    note = None
    if conclusion and conclusion["key"] == "bank_cash_flow":
        note = "Only bank statements are on file, so this is cash retained, not profit: owner draws, transfers and advance payments all count as spend."
    elif conclusion and conclusion["key"] == "profit_and_loss":
        note = "No tax return is on file; the profit-and-loss statement stands in until one is uploaded."
    return {"candidates": candidates, "conclusion": conclusion, "note": note}


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------


def expenses_block(
    statement: dict[str, Any] | None,
    years: list[dict[str, Any]],
    bank: dict[str, Any],
    service: dict[str, Any],
) -> dict[str, Any]:
    lines: list[dict[str, Any]] = []
    if statement:
        months = statement.get("months_covered") or 0
        factor = 12 / months if 0 < months < 12 else 1.0
        for key, label in _PL_EXPENSE_LINES:
            value = statement.get(key)
            if value is None:
                continue
            lines.append(
                {
                    "key": f"pl_{key}",
                    "label": label,
                    "amount": _round(value),
                    "annualized": _round(value * factor),
                    "period": statement.get("period_label"),
                    "source": "profit_and_loss",
                    "ratio_to_revenue": _ratio(value, statement.get("gross_revenue")),
                }
            )
    latest_tax = years[-1] if years else None
    if latest_tax:
        for key, label in _TAX_EXPENSE_LINES:
            value = latest_tax.get(key)
            if value is None:
                continue
            lines.append(
                {
                    "key": f"tax_{key}",
                    "label": label,
                    "amount": _round(value),
                    "annualized": _round(value),
                    "period": str(latest_tax.get("year") or ""),
                    "source": "tax_return",
                    "ratio_to_revenue": _ratio(value, latest_tax.get("gross_receipts")),
                }
            )
    if bank.get("avg_withdrawals") is not None:
        lines.append(
            {
                "key": "bank_outflows",
                "label": "Average monthly outflows",
                "amount": bank["avg_withdrawals"],
                "annualized": bank.get("annualized_withdrawals"),
                "period": f"{month_label(bank['first_month'])}–{month_label(bank['last_month'])}"
                if bank.get("first_month")
                else None,
                "source": "bank_statements",
                "ratio_to_revenue": bank.get("avg_outflow_ratio"),
            }
        )
    if service.get("monthly"):
        lines.append(
            {
                "key": "debt_service",
                "label": "Debt service",
                "amount": service["monthly"],
                "annualized": service.get("annual"),
                "period": "monthly",
                "source": service.get("source"),
                "ratio_to_revenue": _ratio(service["monthly"], bank.get("avg_deposits")),
            }
        )
    return {
        "lines": lines,
        "outflow_ratio": bank.get("avg_outflow_ratio"),
        "opex_ratio": statement.get("opex_ratio") if statement else None,
        "cogs_ratio": statement.get("cogs_ratio") if statement else None,
        "debt_service_to_deposits": _ratio(service.get("monthly"), bank.get("avg_deposits")),
    }


# ---------------------------------------------------------------------------
# The cockpit
# ---------------------------------------------------------------------------


def _notes(
    coverage: dict[str, Any],
    bank: dict[str, Any],
    service: dict[str, Any],
    proposed: dict[str, Any] | None,
    years: list[dict[str, Any]],
    statement: dict[str, Any] | None,
) -> list[str]:
    notes: list[str] = []
    if coverage["bank_months"] == 0:
        notes.append(
            "No bank statement has been analyzed yet; deposits, balances and cash flow stay empty until one is."
        )
    else:
        if coverage["bank_months"] < REQUIRED_STATEMENT_MONTHS:
            notes.append(
                f"{coverage['bank_months']} of {REQUIRED_STATEMENT_MONTHS} statement months on file."
            )
        if bank.get("gaps"):
            notes.append(
                "Statement months missing between the first and the last: "
                + ", ".join(month_label(m) for m in bank["gaps"])
                + "."
            )
        if coverage["bank_months_accepted"] < coverage["bank_months"]:
            notes.append(
                f"{coverage['bank_months'] - coverage['bank_months_accepted']} statement month(s) analyzed but not yet accepted by the evidence review."
            )
        if len(coverage["bank_accounts"]) > 1:
            notes.append(
                "Figures combine " + ", ".join(a["label"] for a in coverage["bank_accounts"]) + "."
            )
    if not years:
        notes.append(
            "No tax return on file; EBITDA and net profit lean on "
            + (
                "the profit-and-loss statement."
                if statement
                else "bank cash flow, which is a proxy."
            )
        )
    if service.get("source") == "none":
        notes.append(
            "No debt service on file: the debt schedule form is empty and no schedule or advance terms were read, so coverage cannot be computed."
        )
    elif service.get("source") == "advance_terms":
        notes.append(
            "Debt service comes from the payments read off advance terms; enter the debt schedule to confirm it."
        )
    elif service.get("source") == "debt_schedule_document":
        notes.append(
            "Debt service is the total printed on the uploaded debt schedule; the form on the file has no rows yet."
        )
    elif service.get("source") == "bank_observed":
        notes.append(
            "Debt service is what the statements show going to lenders and funders each month; record the advances or the debt schedule to confirm it."
        )
    elif service.get("source") == "debt_schedule_rows+bank_observed":
        notes.append(
            "The statements show lender debits that no schedule row names; they were added to the debt service."
        )
    if coverage.get("statements_need_reread"):
        notes.append(
            f"{coverage['statements_need_reread']} statement file(s) were read before recurring payments were extracted; re-read them to see who is being paid."
        )
    if proposed is None:
        notes.append(
            "No loan terms recorded, so the pro-forma coverage (with the new payment) is not shown."
        )
    return notes


def build_cockpit(
    analyses: list[dict[str, Any]],
    debts: list[dict[str, Any]],
    *,
    requested_amount: float | None = None,
    proposed_terms: dict[str, Any] | None = None,
    counted_accounts: list[str] | None = None,
    plan: dict[str, Any] | None = None,
    account_aliases: dict[str, str] | None = None,
    schedule_state: dict[str, Any] | None = None,
    recorded_terms: dict[str, Any] | None = None,
    economics: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The cockpit read model from plain dicts.

    `analyses`: one per current evidence file with a completed analysis —
    file_id, file_name, classification, classifications (every type the
    document carries), key_facts, accepted, analyzed_at, period_hint.
    `debts`: the active dos_debts rows as dicts. `proposed_terms`: the loan
    on record (amount, rate_pct, term_months, monthly_payment, source, label).
    """
    months, accounts = bank_months(analyses, counted_accounts, account_aliases)
    bank = bank_summary(months)
    for account in accounts:
        account["summary"] = bank_summary(account["series"])
    narrowed = any(not a["counted"] for a in accounts)
    all_months, _all_accounts = (
        bank_months(analyses, None, account_aliases) if narrowed else (months, accounts)
    )
    all_summary = bank_summary(all_months) if narrowed else bank
    credits = recurring_payments(analyses, field="recurring_credits", direction=1)
    overdrafts = overdrafts_block(months)
    years = tax_years(analyses)
    statement = profit_and_loss(analyses)
    sheet = balance_sheet(analyses)
    rows = debt_rows(debts)
    document = extract_debt_schedule(_extractor_rows(analyses))
    documented = documented_debts(analyses)
    recurring = recurring_payments(analyses)
    schedule = schedule_block(rows, document, documented, schedule_state)
    service = debt_service(
        rows, document, documented, recurring, declared_none=schedule["status"] == "voided"
    )
    reconciliation = reconcile_advances(rows, documented, recurring)
    proposed = proposed_payment(proposed_terms)
    dscr = dscr_block(years, statement, bank, service, proposed)
    from app.services import financial_plan as plans

    plan_record = plans.normalize_plan(plan)
    debt_block = {"rows": rows, "documented": documented, "service": service}
    obligations = plans.obligations(debt_block, recurring)
    evaluation = plans.evaluate_plan(
        plan_record,
        obligations_list=obligations,
        recurring=recurring,
        months=months,
        dscr=dscr,
        service=service,
        proposed=proposed,
        requested_amount=_num(requested_amount),
    )
    levers = plans.coverage_levers(
        plan_record,
        evaluation,
        obligations_list=obligations,
        recurring=recurring,
        dscr=dscr,
        requested_amount=_num(requested_amount),
        terms=proposed_terms,
        tax_years=years,
    )
    modifications = plans.describe_modifications(plan_record, evaluation, obligations, recurring, accounts)
    impact = plans.plan_impact(evaluation, dscr, accounts)
    from app.services import plan_terms_link as terms_link_svc

    terms_link = terms_link_svc.terms_link(plan_record, evaluation, recorded_terms)
    coverage = {
        "files_analyzed": len(analyses),
        "files_accepted": len(_accepted_files(analyses)),
        "bank_months": len(months),
        "bank_months_accepted": bank["accepted_months"],
        "bank_months_required": REQUIRED_STATEMENT_MONTHS,
        "bank_accounts": accounts,
        "bank_gaps": bank["gaps"],
        "first_month": bank["first_month"],
        "last_month": bank["last_month"],
        "tax_years": [str(y.get("year")) for y in years],
        "has_profit_and_loss": statement is not None,
        "has_balance_sheet": sheet is not None,
        "has_debt_schedule_document": document is not None,
        "debt_rows": len(rows),
        "debt_schedule_status": schedule["status"],
        "documented_debts": len(documented),
        "recurring_months_read": len(recurring["months_read"]),
        "statements_need_reread": len(recurring["files_need_reread"]),
        "bank_files_need_reread": recurring["files_need_reread"],
    }
    cockpit: dict[str, Any] = {
        "generated_at": (now or datetime.now(UTC)).isoformat(),
        "requested_amount": _num(requested_amount),
        "coverage": coverage,
        "bank": {
            "months": months,
            "summary": bank,
            "accounts": accounts,
            "recurring": recurring,
            "recurring_credits": credits,
            "overdrafts": overdrafts,
            "counted_accounts": [a["key"] for a in accounts if a["counted"]] if narrowed else [],
            "all_months": all_months if narrowed else None,
            "all_summary": all_summary if narrowed else None,
        },
        "tax": {"years": years},
        "profit_and_loss": statement,
        "balance_sheet": sheet,
        "expenses": expenses_block(statement, years, bank, service),
        "debt": {
            "rows": rows,
            "documented": documented,
            "document": {
                "total_monthly_debt_service": _num(document.get("total_monthly_debt_service")),
                "total_outstanding_balance": _num(document.get("total_outstanding_balance")),
                "debts": [d for d in document.get("debts") or [] if isinstance(d, dict)],
            }
            if document
            else None,
            "service": service,
            "reconciliation": reconciliation,
            "obligations": obligations,
            "schedule": schedule,
        },
        "dscr": dscr,
        "plan": {
            "record": plan_record,
            "evaluation": evaluation,
            "levers": levers,
            "modifications": modifications,
            "impact": impact,
            "terms_link": terms_link,
            "schedule": evaluation.get("schedule"),
            "schedule_rows": evaluation.get("schedule_rows") or [],
            "risk": evaluation.get("risk"),
        },
        "net_profit": net_profit_block(years, statement, bank),
        "notes": _notes(coverage, bank, service, proposed, years, statement)
        + schedule_notes(schedule, service),
    }

    # The terms the package and the listing speak of, with the schedule and
    # the principal at risk; and, for the desk only, what the deal costs the
    # client with QC's fees in (never copied into a summary, a package or a
    # listing: those build their own dicts key by key).
    cockpit["proposed_terms"] = proposed_terms_block(cockpit)
    cockpit["plan"]["economics"] = (
        internal_economics(economics, cockpit["proposed_terms"]) if economics is not None else None
    )
    return cockpit


def plan_summary(cockpit: dict[str, Any]) -> dict[str, Any]:
    """The desk's plan as a package or a listing carries it: whether it is in
    force, what changed in plain words, and the figures before and after."""
    plan = cockpit.get("plan") or {}
    evaluation = plan.get("evaluation") or {}
    record = plan.get("record") or {}
    return {
        "active": bool(evaluation.get("active")),
        "modifications": plan.get("modifications") or [],
        "impact": plan.get("impact") or {},
        "note": record.get("note"),
        "updated_at": record.get("updated_at"),
        "updated_by": record.get("updated_by"),
    }


def plan_brief(cockpit: dict[str, Any]) -> dict[str, Any]:
    """The plan as facts for a model: what the desk changed and the coverage
    before and after, nothing that reads as an instruction."""
    summary = plan_summary(cockpit)
    impact = summary["impact"]
    dscr = impact.get("dscr") or {}
    service = impact.get("debt_service_monthly") or {}
    return {
        "active": summary["active"],
        "modifications": [
            f"{m['label']}: {m['detail']}" if m.get("detail") else str(m.get("label"))
            for m in summary["modifications"]
        ],
        "dscr_before": dscr.get("before"),
        "dscr_after": dscr.get("after"),
        "dscr_basis": dscr.get("basis"),
        "debt_service_before_monthly": service.get("before"),
        "debt_service_after_monthly": service.get("after"),
        "loan": impact.get("loan"),
        "note": summary["note"],
    }


def proposed_terms_block(cockpit: dict[str, Any]) -> dict[str, Any] | None:
    """The terms the package and the listing speak of: the recorded term
    sheet first, else the loan in the desk's plan, with the payment schedule
    on a monthly basis and the principal at risk (the coverage on every
    payment uses the income after the plan and the existing service beside
    the new loan). Never a fee: the package and the offering print this."""
    from app.services.loan_schedule import STRUCTURE_LABELS, build_schedule, describe, risk_profile

    plan = cockpit.get("plan") or {}
    evaluation = plan.get("evaluation") or {}
    dscr = cockpit.get("dscr") or {}
    proposed = dscr.get("proposed")
    base: dict[str, Any] | None = None
    if proposed and proposed.get("amount"):
        base = {
            "source": "term_sheet",
            "label": proposed.get("label"),
            "amount": proposed.get("amount"),
            "rate_pct": proposed.get("rate_pct"),
            "term_months": proposed.get("term_months"),
            "monthly_payment": proposed.get("monthly_payment"),
            "structure": proposed.get("structure")
            or ("interest_only" if proposed.get("interest_only") else None),
            "interest_only_months": int(proposed.get("interest_only_months") or 0),
            "treatment": proposed.get("treatment"),
            "retained_annual_debt_service": proposed.get("retained_annual_debt_service"),
            "payment_stated": bool(proposed.get("payment_stated")),
        }
    else:
        loan = (plan.get("impact") or {}).get("loan")
        if loan and loan.get("amount"):
            base = {
                "source": "plan",
                "label": "The desk's plan",
                "amount": loan.get("amount"),
                "rate_pct": loan.get("rate_pct"),
                "term_months": loan.get("term_months"),
                "monthly_payment": loan.get("payment_monthly"),
                "structure": loan.get("structure")
                or ("interest_only" if loan.get("interest_only") else None),
                "interest_only_months": int(loan.get("interest_only_months") or 0),
                "treatment": None,
                "retained_annual_debt_service": None,
                "payment_stated": False,
            }
    if base is None:
        return None
    schedule = (
        build_schedule(
            base["amount"],
            base["rate_pct"],
            base["term_months"],
            structure=base["structure"],
            interest_only_months=base["interest_only_months"],
        )
        if base["amount"]
        and base["rate_pct"] is not None
        and (base["term_months"] or base["structure"] == "interest_only")
        else None
    )
    base["interest_only"] = (
        bool(schedule and schedule["structure"] == "interest_only")
        or (not schedule and base["structure"] == "interest_only")
    )
    if schedule:
        base["structure"] = schedule["structure"]
        base["structure_label"] = STRUCTURE_LABELS[schedule["structure"]]
        base["interest_only_months"] = schedule["interest_only_months"]
        base["payment_io_monthly"] = schedule["payment_io_monthly"]
        base["payment_after_monthly"] = schedule["payment_after_monthly"]
        base["describe"] = describe(schedule)
        base["schedule"] = {k: v for k, v in schedule.items() if k != "rows"}
        keys = (
            "period",
            "month",
            "phase",
            "payment",
            "interest",
            "principal",
            "balance",
            "balloon",
            "exposure_pct",
            "recovered_pct",
            "cumulative_paid",
            "cash_at_risk_pct",
        )
        base["schedule_rows"] = [{k: r.get(k) for k in keys} for r in schedule["rows"]]
        stated = _num(base.get("monthly_payment"))
        formula = schedule["coverage_payment_monthly"]
        if base["payment_stated"] and stated and formula and abs(stated - formula) / formula > 0.02:
            base["payment_note"] = (
                f"The recorded payment, {stated:,.0f} a month, is the lender's own figure; "
                f"the schedule is the formula at the stated rate ({formula:,.0f} a month)."
            )
        else:
            base["payment_note"] = None
        if base["monthly_payment"] is None:
            base["monthly_payment"] = formula
        # Coverage on every payment: the income after the plan when there is
        # one, else today's; beside the existing service after what the plan
        # retires (or what the sheet retains when it refinances).
        if evaluation.get("active") and evaluation.get("income_after_annual"):
            income_annual = evaluation["income_after_annual"]
        else:
            income_annual = (dscr.get("headline") or {}).get("numerator")
        if base["treatment"] == "refinance" and base["retained_annual_debt_service"] is not None:
            existing = float(base["retained_annual_debt_service"]) / 12.0
        elif (
            evaluation.get("active")
            and evaluation.get("debt_service_before_new_payment_monthly") is not None
        ):
            existing = float(evaluation["debt_service_before_new_payment_monthly"])
        else:
            existing = float(dscr.get("annual_debt_service") or 0.0) / 12.0
        base["risk"] = risk_profile(
            schedule,
            income_monthly=(float(income_annual) / 12.0) if income_annual else None,
            existing_service_monthly=existing,
        )
    else:
        base["structure_label"] = None
        base["schedule"] = None
        base["schedule_rows"] = []
        base["risk"] = None
        base["payment_note"] = None
    return base


def internal_economics(
    inputs: dict[str, Any] | None, proposed: dict[str, Any] | None
) -> dict[str, Any] | None:
    """What the deal costs the client once QC's fees are in, for the desk only."""
    from app.services.deal_cost import internal_economics as _economics

    return _economics(inputs or {}, proposed)


def listing_summary(cockpit: dict[str, Any]) -> dict[str, Any]:
    """The handful of figures a marketplace listing draft carries from the file."""
    bank = cockpit["bank"]["summary"]
    headline = cockpit["dscr"].get("headline") or {}
    conclusion = cockpit["net_profit"].get("conclusion") or {}
    latest_tax = cockpit["tax"]["years"][-1] if cockpit["tax"]["years"] else {}
    return {
        "generated_at": cockpit.get("generated_at"),
        "bank_months": cockpit["coverage"]["bank_months"],
        "bank_months_accepted": cockpit["coverage"]["bank_months_accepted"],
        "avg_monthly_deposits": bank.get("avg_deposits"),
        "annualized_deposits": bank.get("annualized_deposits"),
        "avg_monthly_net": bank.get("avg_net"),
        "average_balance": bank.get("average_balance"),
        "nsf_total": bank.get("nsf_total"),
        "deposit_trend_pct": bank.get("deposit_trend_pct"),
        "dscr": headline.get("value"),
        "dscr_basis": headline.get("key"),
        "dscr_status": headline.get("status"),
        "pro_forma_dscr": (cockpit["dscr"].get("pro_forma_headline") or {}).get("pro_forma_value"),
        "dscr_after_plan": ((cockpit.get("plan") or {}).get("evaluation") or {}).get("headline_after"),
        "debt_service_after_monthly": ((cockpit.get("plan") or {}).get("evaluation") or {}).get(
            "debt_service_after_monthly"
        ),
        "plan": plan_summary(cockpit),
        "proposed_terms": cockpit["proposed_terms"]
        if "proposed_terms" in cockpit
        else proposed_terms_block(cockpit),
        "annual_debt_service": cockpit["dscr"].get("annual_debt_service"),
        "net_profit": conclusion.get("value"),
        "net_profit_basis": conclusion.get("key"),
        "net_profit_period": conclusion.get("period"),
        "tax_year": latest_tax.get("year"),
        "tax_gross_receipts": latest_tax.get("gross_receipts"),
        "tax_income": latest_tax.get("income"),
    }


# ---------------------------------------------------------------------------
# Loading the inputs from the database
# ---------------------------------------------------------------------------


def _analysis_rows(
    files: list[Any], latest: dict[uuid.UUID, Any], accepted: set[uuid.UUID]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for file in files:
        analysis = latest.get(file.id)
        if analysis is None or analysis.status != "completed":
            continue
        payload = analysis.analysis if isinstance(analysis.analysis, dict) else {}
        facts = payload.get("key_facts") if isinstance(payload.get("key_facts"), dict) else {}
        classification = effective_file_classification(file.file_name, analysis)
        classifications = set(analysis_classifications(analysis))
        if classification:
            classifications.add(classification)
        analyzed_at = getattr(analysis, "analyzed_at", None)
        rows.append(
            {
                "file_id": str(file.id),
                "file_name": file.file_name,
                "classification": classification,
                "classifications": sorted(classifications),
                "key_facts": facts,
                "accepted": file.id in accepted,
                "analyzed_at": analyzed_at.isoformat() if analyzed_at else None,
                "period_hint": getattr(file, "statement_period", None),
            }
        )
    return rows


def _debt_dicts(rows: list[DealerDebt]) -> list[dict[str, Any]]:
    fields = (
        "id",
        "lender",
        "category",
        "monthly_payment",
        "balance",
        "rate",
        "term_months",
        "maturity_on",
        "original_amount",
        "origin",
        "count_in_dscr",
        "payment_amount",
        "payment_frequency",
        "factor_rate",
        "payoff_amount",
        "vendor_key",
        # The rest of the schedule row, so the cockpit shows what the PDF shows.
        "payment_status",
        "secured",
        "collateral",
        "originated_on",
        "mca_modified",
        "mca_modification_note",
    )
    out = []
    for row in rows:
        item = {field: getattr(row, field, None) for field in fields}
        item["category_label"] = debt_types.debt_type_label(item["category"]) or (item["category"] or "")
        out.append(item)
    return out


async def current_term_sheet(db: AsyncSession, profile_id: uuid.UUID) -> ProductionTermSheet | None:
    return (
        await db.execute(
            select(ProductionTermSheet)
            .where(
                ProductionTermSheet.profile_id == profile_id,
                ProductionTermSheet.status == "current",
            )
            .order_by(ProductionTermSheet.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def recorded_terms_for_profile(
    db: AsyncSession, profile: ApplicationProfile, sheet: ProductionTermSheet | None
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(the recorded terms as the plan reads them, the proposed-terms dict the
    DSCR block uses): the dealer's production term sheet when there is one,
    else the current client terms. Both instruments live on the Underwriting
    tab; the cockpit mirrors whichever is in force."""
    from app.services import plan_terms_link as link

    if sheet is not None:
        return link.recorded_from_production_sheet(sheet), term_sheet_terms(sheet)
    try:
        from app.services.application_terms import current_term_sheet as current_client_terms

        row = await current_client_terms(db, profile.id)
    except Exception:  # noqa: BLE001 - the client terms are one source among several
        row = None
    recorded = link.recorded_from_client_terms(row)
    if recorded is None:
        return None, None
    proposed = {
        "source": "term_sheet",
        "label": recorded["label"],
        "amount": recorded["amount"],
        "rate_pct": recorded["rate_pct"],
        "term_months": recorded["term_months"],
        "monthly_payment": recorded["payment_monthly"],
        "treatment": recorded["treatment"],
        "retained_annual_debt_service": recorded["retained_annual_debt_service"],
        "structure": recorded.get("structure"),
        "interest_only_months": int(recorded.get("interest_only_months") or 0),
        "payment_stated": False,
    }
    return recorded, proposed


def term_sheet_terms(sheet: ProductionTermSheet | None) -> dict[str, Any] | None:
    if sheet is None:
        return None
    extra = sheet.extra if isinstance(sheet.extra, dict) else {}
    return {
        "source": "term_sheet",
        "label": f"Term sheet v{sheet.version}: {sheet.funding_party_name}",
        "amount": sheet.approved_amount,
        "rate_pct": sheet.rate_pct,
        "term_months": sheet.term_months,
        "monthly_payment": sheet.monthly_debt_service,
        # How the sheet itself treats the existing debt: on top of it
        # (additive) or in place of it (refinance, keeping what is retained).
        "treatment": str(extra.get("debt_service_treatment") or "additive"),
        "retained_annual_debt_service": _num(extra.get("retained_annual_debt_service")),
        # The sheet's repayment structure in the schedule's vocabulary; None
        # leaves the house default for the term (a balloon or a lender-stated
        # payment has no structure the schedule can draw exactly).
        "structure": _sheet_structure(sheet, extra),
        "interest_only_months": int(_num(extra.get("interest_only_months")) or 0)
        if _sheet_repayment(sheet, extra) == "interest_only_then_amortizing"
        else 0,
        "payment_stated": bool(extra.get("lender_payment_override"))
        or _sheet_repayment(sheet, extra) in {"fixed_payment", "custom"},
    }


def _sheet_repayment(sheet: Any, extra: dict[str, Any]) -> str:
    """The sheet's repayment structure; sheets recorded before the structure
    existed carry only whether the payment was the level payment."""
    repayment = str(extra.get("repayment_structure") or "")
    if repayment:
        return repayment
    return "fully_amortizing" if getattr(sheet, "debt_service_is_level_payment", True) else "fixed_payment"


def _sheet_structure(sheet: Any, extra: dict[str, Any]) -> str | None:
    repayment = _sheet_repayment(sheet, extra)
    if repayment in {"fully_amortizing", "interest_only_then_amortizing"}:
        return "amortizing"
    if repayment in {"interest_only", "revolving_interest_only"}:
        return "interest_only"
    if repayment in {"fixed_payment", "custom"}:
        # A lender-stated payment: the structure whose own payment it matches
        # (within five percent) is the one the lender priced; otherwise the
        # house default for the term, and the note names the difference.
        return _structure_matching_payment(
            _num(getattr(sheet, "approved_amount", None)),
            _num(getattr(sheet, "rate_pct", None)),
            int(getattr(sheet, "term_months", 0) or 0),
            _num(getattr(sheet, "monthly_debt_service", None)),
        )
    return None


def _structure_matching_payment(
    amount: float | None, rate_pct: float | None, term_months: int, stated_monthly: float | None
) -> str | None:
    from app.services.loan_schedule import build_schedule

    if not amount or rate_pct is None or term_months <= 0 or not stated_monthly:
        return None
    best: tuple[float, str] | None = None
    for kind in ("amortizing", "simple_interest"):
        summary = build_schedule(amount, rate_pct, term_months, structure=kind)
        if not summary or not summary.get("coverage_payment_monthly"):
            continue
        gap = abs(stated_monthly - summary["coverage_payment_monthly"]) / summary["coverage_payment_monthly"]
        if best is None or gap < best[0]:
            best = (gap, kind)
    return best[1] if best and best[0] <= 0.05 else None


async def load_cockpit(db: AsyncSession, profile: ApplicationProfile) -> dict[str, Any]:
    """Everything the cockpit needs for one profile, then the read model."""
    files, latest, _classifications = await programs._evidence_inventory(db, profile)
    accepted = await programs.accepted_evidence_file_ids(db, profile, files, latest)
    analyses = _analysis_rows(files, latest, accepted)
    debts = list(
        (
            await db.execute(
                select(DealerDebt)
                .where(DealerDebt.profile_id == profile.id, DealerDebt.status == "active")
                .order_by(DealerDebt.lender)
            )
        )
        .scalars()
        .all()
    )
    intake = (
        await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    )
    requested_amount, _source, _purpose, _dealer = await programs.source_funding_data(
        db, profile, intake=intake
    )
    sheet = await current_term_sheet(db, profile.id)
    schedule_state = await debt_schedule_state(db, profile)
    recorded_terms, proposed_terms = await recorded_terms_for_profile(db, profile, sheet)
    cockpit = build_cockpit(
        analyses,
        _debt_dicts(debts),
        requested_amount=float(requested_amount) if requested_amount is not None else None,
        proposed_terms=proposed_terms,
        counted_accounts=list(getattr(profile, "counted_bank_accounts", None) or []),
        plan=getattr(profile, "financial_plan", None) or {},
        account_aliases=dict(getattr(profile, "bank_account_aliases", None) or {}),
        schedule_state=schedule_state,
        recorded_terms=recorded_terms,
        economics={
            "accepted_amount": getattr(profile, "underwriting_accepted_amount", None),
            "origination_points": getattr(profile, "forecast_fee_points", None),
            "consulting_fee": getattr(profile, "forecast_consulting_fee", None),
        },
    )
    cockpit["profile_id"] = str(profile.id)
    return cockpit


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


async def _declaration(
    db: AsyncSession, *, reason: str | None, user_id: uuid.UUID | None, at: datetime | None
) -> dict[str, Any]:
    by = None
    if user_id is not None:
        user = await db.get(User, user_id)
        if user is not None:
            by = user.name or user.email
    return {
        "reason": (reason or "").strip() or "No debt on the business",
        "by": by,
        "by_user_id": str(user_id) if user_id else None,
        "at": _iso(at),
    }


async def debt_schedule_state(db: AsyncSession, profile: ApplicationProfile) -> dict[str, Any]:
    """The debt schedule requirement on the file and whether the desk voided it.

    A void is the requirement engine's "not applicable": set on the state
    itself when the baseline policy asks for the schedule, or as an override
    on every selected program that asks for it. Either way the cockpit reports
    one thing: who declared the business debt-free, when, and why.
    """
    state = (
        await db.execute(
            select(ApplicationRequirementState).where(
                ApplicationRequirementState.profile_id == profile.id,
                ApplicationRequirementState.requirement_key == DEBT_SCHEDULE_REQUIREMENT,
            )
        )
    ).scalar_one_or_none()
    if state is None:
        return {"requirement_present": False}
    out: dict[str, Any] = {
        "requirement_present": True,
        "requirement_status": state.status,
        "requested_at": _iso(state.first_requested_at or state.last_requested_at),
        "received_at": _iso(state.received_at),
        "voided": None,
    }
    if state.status in {"waived", "not_applicable"}:
        out["voided"] = await _declaration(
            db,
            reason=state.state_reason,
            user_id=state.verified_by_user_id,
            at=state.verified_at or state.updated_at,
        )
        return out
    program_keys = [str(k) for k in (state.source_program_keys or [])]
    if not program_keys:
        return out
    selections = list(
        (
            await db.execute(
                select(ApplicationProgramSelection).where(
                    ApplicationProgramSelection.profile_id == profile.id,
                    ApplicationProgramSelection.removed_at.is_(None),
                    ApplicationProgramSelection.program_key.in_(program_keys),
                )
            )
        )
        .scalars()
        .all()
    )
    if not selections:
        return out
    overrides = list(
        (
            await db.execute(
                select(ApplicationProgramRequirementOverride).where(
                    ApplicationProgramRequirementOverride.selection_id.in_(
                        [s.id for s in selections]
                    ),
                    ApplicationProgramRequirementOverride.requirement_key
                    == DEBT_SCHEDULE_REQUIREMENT,
                    ApplicationProgramRequirementOverride.restored_at.is_(None),
                    ApplicationProgramRequirementOverride.disposition.in_(
                        ["waived", "not_applicable"]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    covered = {o.selection_id for o in overrides}
    if overrides and all(s.id in covered for s in selections):
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        latest = max(overrides, key=lambda o: o.updated_at or o.created_at or epoch)
        out["voided"] = await _declaration(
            db,
            reason=latest.reason,
            user_id=latest.created_by_user_id,
            at=latest.updated_at or latest.created_at,
        )
    return out


async def bank_statement_files(db: AsyncSession, profile: ApplicationProfile) -> list[Any]:
    files, latest, _classifications = await programs._evidence_inventory(db, profile)
    return [
        file
        for file in files
        if effective_file_classification(file.file_name, latest.get(file.id)) == "bank_statement"
    ]


async def reread_statements(db: AsyncSession, profile: ApplicationProfile) -> list[Any]:
    """Queue a forced re-analysis of every bank statement on the file, so
    statements read before the recurring-payments extraction existed carry it."""
    from app.services import bucket_ai

    targets = await bank_statement_files(db, profile)
    for file in targets:
        await bucket_ai.enqueue_file_analysis(db, file, force=True)
    return targets


async def bank_accounts_for_profile(
    db: AsyncSession, profile: ApplicationProfile
) -> list[dict[str, Any]]:
    """Every checking account the statements on the file belong to, with its
    months, its accepted months and whether the desk counts it."""
    files, latest, _classifications = await programs._evidence_inventory(db, profile)
    accepted = await programs.accepted_evidence_file_ids(db, profile, files, latest)
    analyses = _analysis_rows(files, latest, accepted)
    _months, accounts = bank_months(
        analyses,
        list(getattr(profile, "counted_bank_accounts", None) or []),
        dict(getattr(profile, "bank_account_aliases", None) or {}),
    )
    return [
        {
            "key": a["key"],
            "label": a["label"],
            "months": a["months"],
            "accepted_months": a["accepted_months"],
            "counted": a["counted"],
            "latest_month": a["months"][-1] if a["months"] else None,
            "members": a.get("members") or [a["key"]],
            "sub_accounts": a.get("sub_accounts") or [],
        }
        for a in accounts
    ]
