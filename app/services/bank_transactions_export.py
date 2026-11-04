"""A bank transactions export (CSV or workbook) read into statement months.

Banks let a client download activity as a table: one row per transaction
with a date, a description, an amount (or a debit and a credit column) and
often a running balance. That is not a statement, and a model asked to sum
hundreds of such rows is unreliable (and sees only the first few hundred).
This reads the table deterministically: it finds the header, buckets the
rows by month, and produces the same facts a bank statement produces
(statement_period, beginning and ending balance, deposits, withdrawals,
counts, low balance, negative days, overdraft items, recurring debits and
credits), so the Financials tab reads an export exactly as it reads a PDF
statement. The model still classifies and summarises; the arithmetic is not
its job here.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from app.services.payee_classification import classify_credit, classify_payee, normalize_vendor
from app.services.spreadsheets import Workbook

MIN_ROWS = 5
MAX_ROWS = 60_000
SOURCE = "transactions_export"

_DATE_HEADERS = (
    "date",
    "posting date",
    "post date",
    "posted",
    "transaction date",
    "trans date",
    "txn date",
    "effective date",
    "value date",
    "fecha",
)
_DESC_HEADERS = (
    "description",
    "memo",
    "payee",
    "name",
    "details",
    "transaction",
    "narrative",
    "merchant",
    "descripcion",
    "descripción",
    "concepto",
)
_AMOUNT_HEADERS = ("amount", "transaction amount", "amt", "monto", "importe")
_DEBIT_HEADERS = (
    "debit",
    "debits",
    "withdrawal",
    "withdrawals",
    "money out",
    "paid out",
    "cargo",
    "retiro",
    "debe",
)
_CREDIT_HEADERS = (
    "credit",
    "credits",
    "deposit",
    "deposits",
    "money in",
    "paid in",
    "abono",
    "deposito",
    "depósito",
    "haber",
)
_BALANCE_HEADERS = ("balance", "running balance", "ending balance", "available balance", "saldo")
_TYPE_HEADERS = ("type", "transaction type", "category", "tipo", "details")
_ACCOUNT_HEADERS = ("account", "account number", "account #", "acct", "cuenta")
_OVERDRAFT = re.compile(
    r"\b(nsf|insufficient funds|overdraft|od fee|returned item|return(?:ed)? (?:check|payment|deposit)|unpaid item|chargeback fee)\b",
    re.I,
)
_MONEY_CLEAN = re.compile(r"[^0-9.\-()]")
_DATE_FORMATS = (
    "%m/%d/%Y",
    "%m/%d/%y",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m-%d-%Y",
    "%Y/%m/%d",
    "%b %d, %Y",
    "%d %b %Y",
    "%B %d, %Y",
    "%m/%d/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)


@dataclass
class Columns:
    date: int
    description: int | None
    amount: int | None
    debit: int | None
    credit: int | None
    balance: int | None
    kind: int | None
    account: int | None
    header_row: int
    #: True when a signed amount column puts withdrawals as negatives.
    signed: bool = True


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower().strip(":"))


def _match(header: str, names: tuple[str, ...]) -> bool:
    h = _norm(header)
    return any(
        h == n or h.startswith(n + " ") or h.endswith(" " + n) or h == n.replace(" ", "")
        for n in names
    )


def parse_money(value: Any) -> float | None:
    """'$1,234.56', '(45.00)', '-45', '45.00 CR' → a float; blank → None."""
    text = str(value or "").strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    credit_suffix = bool(re.search(r"\bCR\b", text, re.I))
    debit_suffix = bool(re.search(r"\bDR\b", text, re.I))
    cleaned = _MONEY_CLEAN.sub("", text).replace("(", "").replace(")", "")
    if cleaned in {"", "-", "."}:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if negative or debit_suffix:
        number = -abs(number)
    if credit_suffix:
        number = abs(number)
    return number


def parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    if isinstance(value, (datetime, date)):
        return value.date() if isinstance(value, datetime) else value
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text[:26], fmt).date()
        except ValueError:
            continue
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _find(cells: list[str], names: tuple[str, ...], exclude: set[int] | None = None) -> int | None:
    """The first column matching the names in THEIR order, so "description"
    wins over "details" and "type" over "details" when an export has both."""
    for name in names:
        for i, c in enumerate(cells):
            if exclude and i in exclude:
                continue
            if _match(c, (name,)):
                return i
    return None


def find_columns(rows: list[list[str]]) -> Columns | None:
    """The header row and the columns a transactions table needs: a date, a
    description, and an amount (or debit/credit). Searches the first rows,
    since exports often start with account lines before the header."""
    for index, row in enumerate(rows[:20]):
        cells = [str(c or "") for c in row]
        if sum(1 for c in cells if c.strip()) < 2:
            continue
        date_col = _find(cells, _DATE_HEADERS)
        if date_col is None:
            continue
        desc = _find(cells, _DESC_HEADERS, exclude={date_col})
        amount = _find(cells, _AMOUNT_HEADERS)
        debit = _find(cells, _DEBIT_HEADERS)
        credit = _find(cells, _CREDIT_HEADERS)
        balance = _find(cells, _BALANCE_HEADERS)
        kind = _find(cells, _TYPE_HEADERS, exclude={desc} if desc is not None else set())
        account = _find(cells, _ACCOUNT_HEADERS)
        if amount is None and (debit is None or credit is None):
            continue
        return Columns(
            date=date_col,
            description=desc,
            amount=amount,
            debit=debit,
            credit=credit,
            balance=balance,
            kind=kind,
            account=account,
            header_row=index,
        )
    return None


@dataclass
class Txn:
    on: date
    description: str
    amount: float  # signed: deposits positive, withdrawals negative
    balance: float | None
    kind: str


def _signed_amount(row: list[str], cols: Columns) -> float | None:
    cell = lambda i: row[i] if i is not None and i < len(row) else ""  # noqa: E731
    if cols.amount is not None:
        value = parse_money(cell(cols.amount))
        if value is None:
            return None
        text = _norm(cell(cols.kind)) if cols.kind is not None else ""
        # An unsigned amount column with a type column: the type says the direction.
        if (
            value > 0
            and text
            and any(
                w in text
                for w in (
                    "debit",
                    "withdrawal",
                    "payment",
                    "fee",
                    "check",
                    "purchase",
                    "ach debit",
                    "cargo",
                    "retiro",
                )
            )
        ):
            return -value
        return value
    debit = parse_money(cell(cols.debit)) if cols.debit is not None else None
    credit = parse_money(cell(cols.credit)) if cols.credit is not None else None
    if debit is None and credit is None:
        return None
    return float(credit or 0.0) - abs(float(debit or 0.0))


def read_transactions(rows: list[list[str]], cols: Columns) -> list[Txn]:
    out: list[Txn] = []
    for row in rows[cols.header_row + 1 : cols.header_row + 1 + MAX_ROWS]:
        if not row or cols.date >= len(row):
            continue
        on = parse_date(row[cols.date])
        if on is None:
            continue
        amount = _signed_amount(row, cols)
        if amount is None:
            continue
        description = str(
            row[cols.description]
            if cols.description is not None and cols.description < len(row)
            else ""
        ).strip()
        balance = (
            parse_money(row[cols.balance])
            if cols.balance is not None and cols.balance < len(row)
            else None
        )
        kind = str(row[cols.kind] if cols.kind is not None and cols.kind < len(row) else "").strip()
        out.append(Txn(on=on, description=description, amount=amount, balance=balance, kind=kind))
    out.sort(key=lambda t: t.on)
    return out


def _r2(value: float) -> float:
    return round(value + 0.0, 2)


def _cadence(dates: list[date]) -> str:
    if len(dates) < 2:
        return "irregular"
    gaps = sorted((b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0)
    if not gaps:
        return "irregular"
    median = gaps[len(gaps) // 2]
    if median <= 1:
        return "daily"
    if 5 <= median <= 9:
        return "weekly"
    if 12 <= median <= 16:
        return "biweekly"
    if 26 <= median <= 35:
        return "monthly"
    return "irregular"


def _recurring(txns: list[Txn], *, debits: bool, keep: int = 25) -> list[dict[str, Any]]:
    groups: dict[str, list[Txn]] = defaultdict(list)
    for t in txns:
        if (t.amount < 0) != debits or t.amount == 0:
            continue
        key = normalize_vendor(t.description) or _norm(t.description)
        if not key:
            continue
        groups[key].append(t)
    out: list[dict[str, Any]] = []
    for key, items in groups.items():
        if len(items) < 2:
            continue
        amounts = [abs(t.amount) for t in items]
        dates = sorted(t.on for t in items)
        cadence = _cadence(dates)
        same_amount = max(amounts) - min(amounts) < 0.01
        payee = items[0].description.strip()
        if debits:
            kind, _why = classify_payee(payee, cadence=cadence, same_amount=same_amount)
        else:
            kind, _why = classify_credit(payee)
        out.append(
            {
                "payee": payee,
                "occurrences": len(items),
                "total_amount": _r2(sum(amounts)),
                "average_amount": _r2(sum(amounts) / len(amounts)),
                "cadence": cadence,
                "same_amount": same_amount,
                "first_date": dates[0].isoformat(),
                "last_date": dates[-1].isoformat(),
                "kind_guess": kind,
            }
        )
    out.sort(key=lambda r: -r["total_amount"])
    return out[:keep]


def _month_block(month: tuple[int, int], txns: list[Txn], opening: float | None) -> dict[str, Any]:
    year, mon = month
    first = txns[0].on
    last = txns[-1].on
    deposits = [t for t in txns if t.amount > 0]
    withdrawals = [t for t in txns if t.amount < 0]
    total_in = _r2(sum(t.amount for t in deposits))
    total_out = _r2(sum(-t.amount for t in withdrawals))
    balances = [t.balance for t in txns if t.balance is not None]
    ending = (
        balances[-1]
        if balances
        else (None if opening is None else _r2(opening + total_in - total_out))
    )
    beginning = (
        opening
        if opening is not None
        else (None if not balances else _r2(balances[0] - txns[0].amount))
    )
    low = min(balances) if balances else None
    negative_dates = sorted(
        {t.on.isoformat() for t in txns if t.balance is not None and t.balance < 0}
    )
    overdraft_items = [
        {
            "date": t.on.isoformat(),
            "description": t.description,
            "amount": _r2(abs(t.amount)),
            "type": "nsf_fee"
            if re.search(r"nsf|insufficient", t.description, re.I)
            else "returned_item"
            if re.search(r"return|unpaid", t.description, re.I)
            else "overdraft_fee",
        }
        for t in txns
        if _OVERDRAFT.search(t.description)
    ]
    overdraft_items += [
        {"date": d, "description": "Balance below zero", "amount": None, "type": "negative_balance"}
        for d in negative_dates
    ]
    return {
        "statement_period": f"{date(year, mon, 1).isoformat()} to {last.isoformat()}",
        "month": f"{year:04d}-{mon:02d}",
        "first_transaction": first.isoformat(),
        "last_transaction": last.isoformat(),
        "beginning_balance": _r2(beginning) if beginning is not None else None,
        "ending_balance": _r2(ending) if ending is not None else None,
        "total_deposits_and_credits": total_in,
        "total_withdrawals_and_debits": total_out,
        "number_of_deposits": len(deposits),
        "number_of_withdrawals": len(withdrawals),
        "number_of_checks": sum(
            1
            for t in withdrawals
            if re.search(r"\bcheck\b|\bchk\b", f"{t.description} {t.kind}", re.I)
        ),
        "average_ledger_balance": _r2(sum(balances) / len(balances)) if balances else None,
        "low_daily_balance": _r2(low) if low is not None else None,
        "nsf_or_overdraft_count": sum(
            1 for i in overdraft_items if i["type"] != "negative_balance"
        ),
        "returned_check_count": sum(1 for i in overdraft_items if i["type"] == "returned_item"),
        "negative_balance_dates": negative_dates,
        "overdraft_items": overdraft_items,
        "recurring_debits": _recurring(txns, debits=True),
        "recurring_credits": _recurring(txns, debits=False),
        "transactions_read": len(txns),
    }


def _account_last4(rows: list[list[str]], cols: Columns, file_name: str) -> str | None:
    if cols.account is not None:
        for row in rows[cols.header_row + 1 : cols.header_row + 40]:
            if cols.account < len(row):
                digits = re.sub(r"\D", "", str(row[cols.account] or ""))
                if len(digits) >= 4:
                    return digits[-4:]
    for row in rows[: cols.header_row]:
        text = " ".join(str(c or "") for c in row)
        m = re.search(r"(?:account|acct|a/c)[^0-9]{0,20}(?:x+|\*+|•+|\.{2,})?(\d{4})\b", text, re.I)
        if m:
            return m.group(1)
    m = re.search(
        r"(?:^|\D)(\d{4})(?:\D|$)", re.sub(r"20\d{6}|\d{4}-\d{2}-\d{2}", " ", file_name or "")
    )
    return m.group(1) if m else None


def read_export(workbook: Workbook, *, file_name: str = "") -> dict[str, Any] | None:
    """The bank-statement facts for a transactions export, or None when no
    sheet holds a transactions table with at least MIN_ROWS dated rows."""
    for sheet in workbook.sheets:
        cols = find_columns(sheet.rows)
        if cols is None:
            continue
        txns = read_transactions(sheet.rows, cols)
        if len(txns) < MIN_ROWS:
            continue
        # A table without a signed amount: decide the sign convention from the data.
        if cols.amount is not None and cols.kind is None and all(t.amount >= 0 for t in txns):
            return None  # every amount positive and nothing says which way: not readable safely
        by_month: dict[tuple[int, int], list[Txn]] = defaultdict(list)
        for t in txns:
            by_month[(t.on.year, t.on.month)].append(t)
        months: list[dict[str, Any]] = []
        opening: float | None = None
        first_balance_seen = next((t for t in txns if t.balance is not None), None)
        if first_balance_seen is not None:
            opening = (
                _r2(first_balance_seen.balance - first_balance_seen.amount)
                if first_balance_seen is txns[0]
                else None
            )
        for key in sorted(by_month):
            block = _month_block(key, by_month[key], opening)
            months.append(block)
            opening = block["ending_balance"]
        if not months:
            continue
        last4 = _account_last4(sheet.rows, cols, file_name)
        facts: dict[str, Any] = {
            "source": SOURCE,
            "sheet": sheet.name,
            "transactions_read": len(txns),
            "truncated": sheet.truncated_rows or len(txns) >= MAX_ROWS,
            "account_last4": last4,
            "statement_period": f"{months[0]['first_transaction']} to {months[-1]['last_transaction']}",
            "beginning_balance": months[0]["beginning_balance"],
            "ending_balance": months[-1]["ending_balance"],
            "total_deposits_and_credits": _r2(sum(m["total_deposits_and_credits"] for m in months)),
            "total_withdrawals_and_debits": _r2(
                sum(m["total_withdrawals_and_debits"] for m in months)
            ),
            "number_of_deposits": sum(m["number_of_deposits"] for m in months),
            "number_of_checks": sum(m["number_of_checks"] for m in months),
            "nsf_or_overdraft_count": sum(m["nsf_or_overdraft_count"] for m in months),
            "returned_check_count": sum(m["returned_check_count"] for m in months),
            "negative_balance_dates": sorted(
                {d for m in months for d in m["negative_balance_dates"]}
            ),
            "months": months,
            "recurring_debits": _recurring(txns, debits=True),
            "recurring_credits": _recurring(txns, debits=False),
            "overdraft_items": [i for m in months for i in m["overdraft_items"]],
        }
        low = [m["low_daily_balance"] for m in months if m["low_daily_balance"] is not None]
        facts["low_daily_balance"] = min(low) if low else None
        return facts
    return None


def describe(facts: dict[str, Any]) -> str:
    months = facts.get("months") or []
    span = (
        f"{months[0]['month']} to {months[-1]['month']}"
        if len(months) > 1
        else (months[0]["month"] if months else "no months")
    )
    return (
        f"Read as a bank transactions export: {facts.get('transactions_read', 0)} transactions over {len(months)} month{'s' if len(months) != 1 else ''} ({span}); "
        f"deposits {facts.get('total_deposits_and_credits', 0):,.0f}, withdrawals {facts.get('total_withdrawals_and_debits', 0):,.0f}"
        + (f", account ••{facts['account_last4']}" if facts.get("account_last4") else "")
        + ". The months, balances and recurring payees were computed from the rows, not read by the model."
    )


def is_nan(value: float | None) -> bool:
    return value is not None and math.isnan(value)
