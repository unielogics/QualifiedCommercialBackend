"""A filled-in financial form, read back the way a person reads it.

The forms themselves are built to be typed into: the personal financial
statement is the whole SBA 413, dozens of lines of which a borrower leaves
empty, and the debt schedule is a card per obligation. That is the right shape
for filling one in and the wrong one for finding out what somebody said. This
module turns what is stored into the short version: the headline figures, only
the lines that were answered, and a count of the ones that were not.

It reads the same schemas the forms and the PDFs read (`pfs_schema`,
`business_statement_schema`, `debt_types`), so a label renamed there is renamed
here, and it does its own money parsing through `pfs_schema._amount`, so
"$1,250" and "(500)" mean here what they mean in the totals.

Nothing in here touches the database. The route loads the rows and hands them
in, which keeps every rule testable with plain objects.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.services import business_statement_schema as bss
from app.services import debt_types, pfs_schema

FORM_LABELS: dict[str, str] = {
    "pfs": "Personal financial statement",
    "debt_schedule": "Business debt schedule",
    "p_and_l": "Profit and loss statement",
    "balance_sheet": "Balance sheet",
    "real_estate_schedule": "Real estate schedule",
}

#: Whose answers a debt row carries, by the source that wrote it.
ORIGIN_WORDS: dict[str, str] = {
    "client_form": "the borrower",
    "admin": "the desk",
    "field_desk": "the field desk",
    "ai_draft": "a document we read",
    "document": "a document we read",
}

#: Schedule columns on the 413 that hold money, by column key.
_MONEY_COLUMNS = frozenset(
    {
        "original_balance",
        "current_balance",
        "payment_amount",
        "cost",
        "market_value",
        "original_cost",
        "present_market_value",
        "mortgage_balance",
        "mortgage_payment",
        "present_value",
        "amount_owing",
        "payment",
        "amount",
        "face_amount",
        "cash_surrender_value",
    }
)


# --- values -------------------------------------------------------------------


def is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _money_value(value: Any) -> Decimal | None:
    if is_blank(value):
        return None
    if isinstance(value, Decimal):
        return value
    return pfs_schema._amount(value)


def format_money(value: Any) -> str | None:
    amount = _money_value(value)
    if amount is None:
        return None
    sign = "-" if amount < 0 else ""
    whole = abs(amount)
    text = f"{whole:,.0f}" if whole == whole.to_integral_value() else f"{whole:,.2f}"
    return f"{sign}${text}"


def format_date(value: Any) -> str | None:
    if is_blank(value):
        return None
    parsed: date | None = None
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    else:
        try:
            parsed = date.fromisoformat(str(value).strip()[:10])
        except ValueError:
            return str(value).strip()
    return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"


def format_percent(value: Any) -> str | None:
    if is_blank(value):
        return None
    try:
        number = Decimal(str(value))
    except Exception:  # noqa: BLE001 - a rate typed as words stays words
        return str(value).strip()
    text = f"{number:f}".rstrip("0").rstrip(".") if "." in f"{number:f}" else f"{number:f}"
    return f"{text}%"


def _text(value: Any) -> str | None:
    return None if is_blank(value) else str(value).strip()


def _line(
    label: str, value: str | None, *, amount: Any = None, note: str | None = None
) -> dict[str, Any] | None:
    if value is None:
        return None
    figure = _money_value(amount) if amount is not None else None
    return {
        "label": label,
        "value": value,
        "amount": float(figure) if figure is not None else None,
        "note": note,
    }


def _money_line(label: str, value: Any, *, note: str | None = None) -> dict[str, Any] | None:
    amount = _money_value(value)
    if amount is None:
        return None
    return _line(label, format_money(amount), amount=amount, note=note)


def _compact(lines: list[dict[str, Any] | None]) -> list[dict[str, Any]]:
    return [line for line in lines if line is not None]


def _base(kind: str, **fields: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "label": FORM_LABELS[kind],
        "source": "none",
        "figures_from": None,
        "draft": False,
        "who": None,
        "as_of": None,
        "filled_by": None,
        "updated_at": None,
        "headline": [],
        "sections": [],
        "tables": [],
        "notes": None,
        "files": [],
        "empty_reason": None,
        **fields,
    }


def empty_answers(kind: str, reason: str) -> dict[str, Any]:
    return _base(kind, empty_reason=reason)


# --- personal financial statement ----------------------------------------------


def _summary_section(
    key: str,
    title: str,
    spec_rows: tuple[Any, ...],
    values: dict[str, Any],
    *,
    total_label: str,
    total: Decimal,
) -> dict[str, Any] | None:
    rows: list[dict[str, Any]] = []
    blank = 0
    for spec in spec_rows:
        raw = values.get(spec.key)
        if is_blank(raw):
            blank += 1
            continue
        note = "Counts as liquid" if getattr(spec, "liquid", False) else None
        row = _money_line(spec.label, raw, note=note)
        if row is not None:
            rows.append(row)
    if not rows:
        return None
    return {
        "key": key,
        "title": title,
        "rows": rows,
        "total": _money_line(total_label, total),
        "left_blank": blank,
        "collapsed": False,
    }


def _schedule_cell(column_key: str, value: Any) -> str:
    if is_blank(value):
        return ""
    if column_key in _MONEY_COLUMNS:
        return format_money(value) or ""
    if "date" in column_key or column_key in {"when_due"}:
        return format_date(value) or ""
    return str(value).strip()


def pfs_answers_from_statement(statement: Any) -> dict[str, Any]:
    body = statement.body if isinstance(statement.body, dict) else {}
    totals = pfs_schema.totals(body)
    applicant = body.get("applicant") if isinstance(body.get("applicant"), dict) else {}
    submitted = getattr(statement, "status", None) == "submitted"
    stamped = getattr(statement, "statement_date", None)
    as_of = format_date(
        pfs_schema.statement_date_for(body, stamped.isoformat() if stamped else None)
    )

    sections = _compact(
        [
            _summary_section(
                "assets",
                "Assets",
                pfs_schema.ASSET_ROWS,
                body.get("assets") or {},
                total_label="Total assets",
                total=totals["total_assets"],
            ),
            _summary_section(
                "liabilities",
                "Liabilities",
                pfs_schema.LIABILITY_ROWS,
                body.get("liabilities") or {},
                total_label="Total liabilities",
                total=totals["total_liabilities"],
            ),
            _summary_section(
                "income",
                "Income",
                pfs_schema.INCOME_ROWS,
                body.get("income") or {},
                total_label="Total income",
                total=totals["total_income"],
            ),
            _summary_section(
                "contingent",
                "Contingent liabilities",
                pfs_schema.CONTINGENT_ROWS,
                body.get("contingent") or {},
                total_label="Total contingent",
                total=totals["total_contingent"],
            ),
        ]
    )

    tables: list[dict[str, Any]] = []
    schedules = pfs_schema.normalize_schedule_rows(body)
    for spec in pfs_schema.SCHEDULES:
        rows = [
            [_schedule_cell(key, row.get(key)) for key in spec.column_keys]
            for row in schedules.get(spec.key, [])
            if any(not is_blank(row.get(key)) for key in spec.column_keys)
        ]
        if rows:
            tables.append(
                {
                    "key": spec.key,
                    "title": spec.label,
                    "columns": list(spec.columns),
                    "rows": rows,
                    "total_row": None,
                }
            )

    headline = _compact(
        [
            _money_line("Net worth", totals["net_worth"]),
            _money_line("Total assets", totals["total_assets"]),
            _money_line("Total liabilities", totals["total_liabilities"]),
            _money_line(
                "Liquid assets",
                totals["liquid_assets"],
                note="Cash, savings and marketable securities",
            ),
        ]
    )
    answered = bool(sections or tables)
    return _base(
        "pfs",
        source="filled" if submitted else "none",
        figures_from="form" if answered else None,
        draft=not submitted,
        who=_text(applicant.get("name")),
        as_of=as_of,
        filled_by=(
            "the desk" if getattr(statement, "submitted_by_user_id", None) else "the borrower"
        )
        if submitted
        else None,
        updated_at=getattr(statement, "updated_at", None),
        headline=headline if answered else [],
        sections=sections,
        tables=tables,
        notes=_text(body.get("notes")),
        empty_reason=None if answered else "The form was opened but nothing has been entered yet.",
    )


def pfs_answers_from_uploads(statements: list[dict[str, Any]]) -> dict[str, Any]:
    """What the AI read off each uploaded statement, one block per person.

    Never summed: a statement belongs to one person, and two documents are two
    people's balance sheets.
    """
    sections: list[dict[str, Any]] = []
    for index, item in enumerate(statements, start=1):
        as_of = format_date(item.get("statement_date"))
        rows = _compact(
            [
                _money_line("Net worth", item.get("net_worth")),
                _money_line("Total assets", item.get("total_assets")),
                _money_line("Total liabilities", item.get("total_liabilities")),
                _money_line("Liquid assets", item.get("liquid_assets")),
            ]
        )
        if not rows:
            continue
        title = f"Statement {index}" if len(statements) > 1 else "Read from the document"
        if as_of:
            title = f"{title} · as of {as_of}"
        sections.append(
            {
                "key": f"statement-{index}",
                "title": title,
                "rows": rows,
                "total": None,
                "left_blank": 0,
                "collapsed": False,
            }
        )
    if not sections:
        return _base(
            "pfs",
            source="uploaded",
            empty_reason="A document is on file, but no figures could be read off it. Open the file to check it.",
        )
    single = len(sections) == 1
    return _base(
        "pfs",
        source="uploaded",
        figures_from="document",
        filled_by="a document we read",
        as_of=format_date(statements[0].get("statement_date")) if single else None,
        headline=list(sections[0]["rows"]) if single else [],
        sections=sections,
    )


# --- business debt schedule ----------------------------------------------------

_SECURED_WORDS = {
    "secured": "Secured",
    "unsecured": "Unsecured",
    "yes": "Secured",
    "no": "Unsecured",
}
_STATUS_WORDS = {
    "current": "Current",
    "delinquent": "Delinquent",
    "late": "Late",
    "default": "In default",
}


def _word(mapping: dict[str, str], value: Any) -> str | None:
    text = _text(value)
    return mapping.get(text.lower(), text) if text else None


def _debt_type_words(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    slug = debt_types.normalize_debt_type(text)
    if slug and slug != "loan":
        return debt_types.debt_type_label(slug)
    return None if text.lower() == "loan" else text


def _debt_detail_section(index: int, row: Any) -> dict[str, Any]:
    category = getattr(row, "category", None)
    slug = debt_types.normalize_debt_type(category)
    advance = slug in debt_types.ADVANCE_DEBT_TYPES
    modified = getattr(row, "mca_modified", None)
    rows = _compact(
        [
            _line("Type", _debt_type_words(category)),
            _money_line("Original amount", getattr(row, "original_amount", None)),
            _money_line("Balance", getattr(row, "balance", None)),
            _line("Interest rate", format_percent(getattr(row, "rate", None))),
            _money_line("Monthly payment", getattr(row, "monthly_payment", None)),
            _line("Started", format_date(getattr(row, "originated_on", None))),
            _line("Matures", format_date(getattr(row, "maturity_on", None))),
            _line("Secured", _word(_SECURED_WORDS, getattr(row, "secured", None))),
            _line("Collateral", _text(getattr(row, "collateral", None))),
            _line("Payment status", _word(_STATUS_WORDS, getattr(row, "payment_status", None))),
            _line(
                "Funder lowered the payment because sales fell",
                None if modified is None or not advance else ("Yes" if modified else "No"),
            ),
            _line("What changed", _text(getattr(row, "mca_modification_note", None))),
            _line(
                "Counted in DSCR",
                None if getattr(row, "count_in_dscr", True) else "No, left out of the DSCR",
            ),
            _line("Notes", _text(getattr(row, "notes", None))),
            _line("Entered by", ORIGIN_WORDS.get(str(getattr(row, "origin", "") or ""))),
        ]
    )
    title = _text(getattr(row, "lender", None)) or f"Obligation {index}"
    return {
        "key": f"debt-{index}",
        "title": title,
        "rows": rows,
        "total": None,
        "left_blank": 0,
        "collapsed": True,
    }


def debt_answers_from_rows(rows: list[Any]) -> dict[str, Any]:
    if not rows:
        return empty_answers("debt_schedule", "No obligations are on the schedule yet.")
    total_balance = sum(
        (_money_value(getattr(row, "balance", None)) or Decimal("0") for row in rows), Decimal("0")
    )
    total_monthly = sum(
        (_money_value(getattr(row, "monthly_payment", None)) or Decimal("0") for row in rows),
        Decimal("0"),
    )
    glance = [
        [
            _text(getattr(row, "lender", None)) or "Unnamed",
            _debt_type_words(getattr(row, "category", None)) or "",
            format_money(getattr(row, "balance", None)) or "",
            format_money(getattr(row, "monthly_payment", None)) or "",
            format_percent(getattr(row, "rate", None)) or "",
            _word(_STATUS_WORDS, getattr(row, "payment_status", None)) or "",
        ]
        for row in rows
    ]
    origins = []
    for row in rows:
        words = ORIGIN_WORDS.get(str(getattr(row, "origin", "") or ""))
        if words and words not in origins:
            origins.append(words)
    updated = [
        getattr(row, "updated_at", None)
        for row in rows
        if getattr(row, "updated_at", None) is not None
    ]
    return _base(
        "debt_schedule",
        source="filled",
        figures_from="form",
        filled_by=" and ".join(origins) if origins else None,
        updated_at=max(updated) if updated else None,
        headline=_compact(
            [
                _line("Obligations", str(len(rows)), amount=len(rows)),
                _money_line("Monthly payments", total_monthly),
                _money_line("Total owed", total_balance),
            ]
        ),
        tables=[
            {
                "key": "glance",
                "title": "At a glance",
                "columns": ["Lender", "Type", "Balance", "Monthly", "Rate", "Status"],
                "rows": glance,
                "total_row": [
                    "Total",
                    "",
                    format_money(total_balance) or "",
                    format_money(total_monthly) or "",
                    "",
                    "",
                ],
            }
        ],
        sections=[_debt_detail_section(index, row) for index, row in enumerate(rows, start=1)],
    )


def debt_answers_from_upload(schedule: dict[str, Any] | None) -> dict[str, Any]:
    """The obligations the AI read off an uploaded schedule."""
    listed = [item for item in (schedule or {}).get("debts") or [] if isinstance(item, dict)]
    monthly = (schedule or {}).get("total_monthly_debt_service")
    balance = (schedule or {}).get("total_outstanding_balance")
    if not listed and not monthly and not balance:
        return _base(
            "debt_schedule",
            source="uploaded",
            empty_reason="A document is on file, but no obligations could be read off it. Open the file to check it.",
        )
    glance = [
        [
            _text(item.get("lender")) or "Unnamed",
            _debt_type_words(item.get("debt_type") or item.get("type")) or "",
            format_money(item.get("current_balance")) or "",
            format_money(item.get("monthly_payment")) or "",
            format_percent(item.get("interest_rate")) or "",
            _word(_STATUS_WORDS, item.get("payment_status")) or "",
        ]
        for item in listed
    ]
    return _base(
        "debt_schedule",
        source="uploaded",
        figures_from="document",
        filled_by="a document we read",
        headline=_compact(
            [
                _line("Obligations", str(len(listed)), amount=len(listed)) if listed else None,
                _money_line("Monthly payments", monthly),
                _money_line("Total owed", balance),
            ]
        ),
        tables=(
            [
                {
                    "key": "glance",
                    "title": "Read from the document",
                    "columns": ["Lender", "Type", "Balance", "Monthly", "Rate", "Status"],
                    "rows": glance,
                    "total_row": None,
                }
            ]
            if glance
            else []
        ),
    )


# --- profit and loss, balance sheet ---------------------------------------------


def _computed_value(item: Any, value: Any) -> str | None:
    if value is None:
        return None
    if item.format == "ratio":
        return f"{Decimal(str(value)):.2f}"
    if item.format == "count":
        return str(int(value))
    return format_money(value)


def business_answers_from_statement(kind: str, statement: Any) -> dict[str, Any]:
    schema = bss.SCHEMA_FOR[kind]
    body = statement.body if isinstance(statement.body, dict) else {}
    header = body.get("header") if isinstance(body.get("header"), dict) else {}
    submitted = getattr(statement, "status", None) == "submitted"
    totals = bss.totals(kind, body)
    sections: list[dict[str, Any]] = []
    for section in schema.sections:
        values = bss._section_values(body, section)
        rows: list[dict[str, Any]] = []
        blank = 0
        for row in section.rows:
            raw = values.get(row.key)
            if is_blank(raw):
                blank += 1
                continue
            line = _line(row.label, _text(raw)) if row.text else _money_line(row.label, raw)
            if line is not None:
                rows.append(line)
        if not rows:
            continue
        subtotal = bss._section_total(body, section)
        sections.append(
            {
                "key": section.key,
                "title": section.label,
                "rows": rows,
                "total": _money_line(section.subtotal_label, subtotal),
                "left_blank": blank,
                "collapsed": False,
            }
        )
    headline = _compact(
        [
            _line(
                item.label,
                _computed_value(item, totals.get(item.key)),
                amount=totals.get(item.key) if item.format == "money" else None,
            )
            for item in schema.computed
            if item.emphasis and totals.get(item.key) is not None
        ]
    )
    answered = bool(sections)
    return _base(
        kind,
        source="filled" if submitted else "none",
        figures_from="form" if answered else None,
        draft=not submitted,
        who=_text(header.get("business_name")),
        as_of=bss.period_label(kind, body),
        filled_by=(
            "the desk" if getattr(statement, "submitted_by_user_id", None) else "the borrower"
        )
        if submitted
        else None,
        updated_at=getattr(statement, "updated_at", None),
        headline=headline if answered else [],
        sections=sections,
        notes=_text(body.get("notes")),
        empty_reason=None if answered else "The form was opened but nothing has been entered yet.",
    )


_UPLOADED_PL = (
    ("gross_revenue", "Gross revenue"),
    ("cost_of_goods_sold", "Cost of goods sold"),
    ("gross_profit", "Gross profit"),
    ("total_operating_expenses", "Total operating expenses"),
    ("operating_income", "Operating income"),
    ("net_income", "Net income"),
    ("ebitda", "EBITDA"),
)
_UPLOADED_BS = (
    ("total_assets", "Total assets"),
    ("total_liabilities", "Total liabilities"),
    ("total_equity", "Total equity"),
    ("cash", "Cash"),
    ("accounts_receivable", "Accounts receivable"),
    ("inventory", "Inventory"),
    ("accounts_payable", "Accounts payable"),
)


def business_answers_from_upload(kind: str, facts: dict[str, Any] | None) -> dict[str, Any]:
    """The headline figures the AI read off an uploaded statement."""
    facts = facts or {}
    spec = _UPLOADED_PL if kind == "p_and_l" else _UPLOADED_BS
    rows = _compact([_money_line(label, facts.get(key)) for key, label in spec])
    if not rows:
        return _base(
            kind,
            source="uploaded",
            empty_reason="A document is on file, but no figures could be read off it. Open the file to check it.",
        )
    period = None
    if kind == "p_and_l":
        start, end = format_date(facts.get("period_start")), format_date(facts.get("period_end"))
        period = f"{start} to {end}" if start and end else None
    else:
        period = format_date(facts.get("as_of_date") or facts.get("statement_date"))
    return _base(
        kind,
        source="uploaded",
        figures_from="document",
        filled_by="a document we read",
        who=_text(facts.get("business_name")),
        as_of=period,
        headline=rows[:4],
        sections=[
            {
                "key": "read",
                "title": "Read from the document",
                "rows": rows,
                "total": None,
                "left_blank": 0,
                "collapsed": False,
            }
        ],
    )
