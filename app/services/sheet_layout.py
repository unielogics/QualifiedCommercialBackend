"""The four financial forms as sheets: one row list each, shared by the
downloadable workbook and the on-screen grid.

The owner asked for "a google sheet type of interface" over the four forms
(profit and loss, balance sheet, business debt schedule, personal financial
statement). The workbook we already ship is generated from the schemas; this
module is that generation with the rendering taken out, so the grid on screen
and the .xlsx a borrower downloads are the *same* rows in the same order with
the same numbers — a cell's `r` here is the row the workbook writes, and its
`xlsx_name` is the workbook's defined name for it (`pl.gross_revenue`,
`ds.r7.lender`, `pfs.real_estate.r2.mortgage_balance`). One parity test
(`test_sheet_layout.py`) holds the two together.

What a layout carries, per cell:

- `key` — the stable address a save uses. For a fixed row it is the schema
  key. For a list row (a debt, a schedule line) it is `{row key}.{column}`,
  where the row key is the stored row's id when the layout was built from a
  body, and the ordinal `r{n}` otherwise. Saves never use the row number.
- `xlsx_name` — the defined-name suffix the workbook writes. Always ordinal
  for list rows (`r{n}`), because a template has no ids. Carrying both is
  what lets the grid key a debt by identity while the workbook keeps
  `ds.r7.lender`.
- `path` — where the value lives in the stored JSON body, so `flatten` and
  `unflatten` are generic walkers rather than four hand-written mappings.
- `formula` — the arithmetic of a computed cell, written against *keys*
  (`=SUM({supplies}:{other})`); the workbook resolves keys to cell addresses.
  The grid does not evaluate these — it reuses the schema's `totals` — and
  `compute` names the `totals` entry the cell shows.

Row numbers are pinned by the workbook, not the other way round, so a row the
workbook keeps for itself (the hidden key row under the debt schedule's column
head) is simply absent here and its number is skipped.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as _dc_field
from decimal import Decimal
from typing import Any

from app.services import business_statement_schema as bss
from app.services import pfs_schema
from app.services.financial_statements import DEBT_COLUMN_LABELS, DEBT_COLUMNS

LAYOUT_VERSION = "qc_sheets.v1"

#: The debt schedule has no schema module of its own; the version travels here.
DEBT_SCHEMA_VERSION = "qc_ds.v1"

KINDS: tuple[str, ...] = ("p_and_l", "balance_sheet", "debt_schedule", "pfs")

SHEET_TITLES: dict[str, str] = {
    "p_and_l": "Profit and Loss",
    "balance_sheet": "Balance Sheet",
    "debt_schedule": "Business Debt Schedule",
    "pfs": "Personal Financial Statement",
}

NAME_PREFIX: dict[str, str] = {
    "p_and_l": "pl",
    "balance_sheet": "bs",
    "debt_schedule": "ds",
    "pfs": "pfs",
}

SCHEMA_VERSIONS: dict[str, str] = {
    "p_and_l": bss.PL_SCHEMA_VERSION,
    "balance_sheet": bss.BS_SCHEMA_VERSION,
    "debt_schedule": DEBT_SCHEMA_VERSION,
    "pfs": pfs_schema.SCHEMA_VERSION,
}

#: Rows in the debt-schedule grid. The owner's template had fifteen.
DEBT_ROWS = 15

#: Blank lines per supporting schedule on the personal financial statement.
#: Two, and no separator rows, because the analyzer reads at most
#: `bucket_ai.MAX_SPREADSHEET_ROWS` (80) rows of a sheet uploaded back: the
#: summary is 47 rows, eight blocks of (heading + column head + 2) are 32, and
#: the closing note is the 80th. A borrower with more lines adds them on
#: screen; the template is the floor, not the ceiling.
PFS_SCHEDULE_ROWS = 2

#: The columns a PFS schedule block may use, in order. Column 3 is skipped:
#: it is the workbook's hidden key column beside every summary input, and a
#: schedule column written there would be invisible in Excel.
PFS_SCHEDULE_COLUMNS: tuple[int, ...] = (1, 2, 4, 5, 6, 7, 8, 9)

#: Where the PFS "as of" date lives in the body once it is stored there (the
#: statement row's `statement_date` column is the source until then).
PFS_AS_OF_KEY = "as_of"

#: Money and date columns of the PFS schedules, by column slug. Formats only —
#: nothing sums a schedule. A column this table does not name is text.
_PFS_MONEY_COLUMNS = frozenset(
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
_PFS_DATE_COLUMNS = frozenset({"date_of_quotation", "date_purchased", "when_due"})
_PFS_COUNT_COLUMNS = frozenset({"number_of_shares"})

_DEBT_MONEY = frozenset({"original_amount", "balance", "monthly_payment"})
_DEBT_DATES = frozenset({"originated_on", "maturity_on"})
_DEBT_CHOICES: dict[str, tuple[str, ...]] = {
    "secured": ("secured", "unsecured"),
    "payment_status": ("current", "delinquent"),
}

_ORDINAL = re.compile(r"^r(\d+)$")
_SLUG = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# The shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    #: 1-based; the workbook column. Need not be contiguous (see
    #: PFS_SCHEDULE_COLUMNS).
    c: int
    label: str = ""
    width: int = 15
    #: "left" | "right"
    align: str = "left"


@dataclass(frozen=True)
class Cell:
    c: int
    #: label | text | money | date | select | rate | formula
    type: str
    #: The save key. None on labels and formulas.
    key: str | None = None
    #: The workbook defined-name suffix. None on labels.
    xlsx_name: str | None = None
    #: money | ratio | count | text | date
    format: str = "text"
    editable: bool = False
    #: The text of a label cell.
    label: str | None = None
    options: tuple[str, ...] | None = None
    #: The `totals` entry a formula cell shows.
    compute: str | None = None
    #: An input key a formula cell merely echoes (the P&L memo lines).
    source: str | None = None
    #: The workbook formula, written against keys in braces.
    formula: str | None = None
    hint: str | None = None
    flags: tuple[str, ...] = ()
    emphasis: bool = False
    colspan: int = 1
    #: Where the value lives in the stored body. A list row is addressed by
    #: its row key (an id, or `r{n}`) in the element after the list name.
    path: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Row:
    #: The workbook row.
    r: int
    #: title | subtitle | heading | blank | note | field | formula | colhead | data
    kind: str
    label: str | None = None
    cells: tuple[Cell, ...] = ()
    #: The list a data row belongs to ("debts", or a PFS schedule key), on
    #: every row of a block including its heading and column head.
    block: str | None = None
    #: A data row's stable identity: the stored row id, or `r{n}`.
    row_key: str | None = None
    #: A data row's ordinal within its block, 1-based.
    ordinal: int | None = None


@dataclass(frozen=True)
class Sheet:
    kind: str
    title: str
    schema_version: str
    layout_version: str
    name_prefix: str
    columns: tuple[Column, ...]
    rows: tuple[Row, ...]
    #: (rows, cols) frozen at the top left.
    freeze: tuple[int, int] = (0, 0)
    #: Every cell with a key, by key.
    by_key: dict[str, tuple[Row, Cell]] = _dc_field(default_factory=dict, compare=False, repr=False)
    #: Every named cell, by xlsx_name.
    by_xlsx_name: dict[str, tuple[Row, Cell]] = _dc_field(
        default_factory=dict, compare=False, repr=False
    )


def _index(rows: Sequence[Row]) -> tuple[dict, dict]:
    by_key: dict[str, tuple[Row, Cell]] = {}
    by_name: dict[str, tuple[Row, Cell]] = {}
    for row in rows:
        for cell in row.cells:
            if cell.key is not None:
                if cell.key in by_key:
                    raise ValueError(f"duplicate key {cell.key!r} at row {row.r}")
                by_key[cell.key] = (row, cell)
            if cell.xlsx_name is not None:
                if cell.xlsx_name in by_name:
                    raise ValueError(f"duplicate xlsx name {cell.xlsx_name!r} at row {row.r}")
                by_name[cell.xlsx_name] = (row, cell)
    return by_key, by_name


def _sheet(kind: str, columns: Sequence[Column], rows: Sequence[Row], *, freeze) -> Sheet:
    by_key, by_name = _index(rows)
    return Sheet(
        kind=kind,
        title=SHEET_TITLES[kind],
        schema_version=SCHEMA_VERSIONS[kind],
        layout_version=LAYOUT_VERSION,
        name_prefix=NAME_PREFIX[kind],
        columns=tuple(columns),
        rows=tuple(rows),
        freeze=freeze,
        by_key=by_key,
        by_xlsx_name=by_name,
    )


# ---------------------------------------------------------------------------
# Building the row list
# ---------------------------------------------------------------------------


class _Rows:
    """A row counter and the cell helpers the four builders share."""

    def __init__(self) -> None:
        self.rows: list[Row] = []
        self.r = 0

    def _add(self, row: Row) -> Row:
        self.rows.append(row)
        return row

    def title(self, text: str, subtitle: str) -> None:
        self.r += 1
        self._add(Row(self.r, "title", label=text, cells=(_label(1, text),)))
        self.r += 1
        self._add(Row(self.r, "subtitle", label=subtitle, cells=(_label(1, subtitle),)))
        self.blank()

    def blank(self) -> None:
        self.r += 1
        self._add(Row(self.r, "blank"))

    def heading(self, text: str, *, colspan: int = 2, block: str | None = None) -> None:
        self.r += 1
        self._add(
            Row(self.r, "heading", label=text, cells=(_label(1, text, colspan=colspan),), block=block)
        )

    def note(self, text: str, *, colspan: int = 2) -> None:
        self.r += 1
        self._add(Row(self.r, "note", label=text, cells=(_label(1, text, colspan=colspan),)))

    def field(
        self,
        key: str,
        label: str,
        path: tuple[str, ...],
        *,
        input: str = "text",
        options: tuple[str, ...] | None = None,
        hint: str | None = None,
    ) -> None:
        self.r += 1
        cell = Cell(
            c=2,
            type=input,
            key=key,
            xlsx_name=key,
            format="date" if input == "date" else "text",
            editable=True,
            options=options,
            hint=hint,
            path=path,
        )
        self._add(Row(self.r, "field", label=label, cells=(_label(1, label), cell)))

    def money(
        self,
        key: str,
        label: str,
        path: tuple[str, ...],
        *,
        hint: str | None = None,
        flags: tuple[str, ...] = (),
    ) -> None:
        self.r += 1
        cell = Cell(
            c=2,
            type="money",
            key=key,
            xlsx_name=key,
            format="money",
            editable=True,
            hint=hint,
            flags=flags,
            path=path,
        )
        self._add(Row(self.r, "field", label=label, cells=(_label(1, label), cell)))

    def formula(
        self,
        key: str,
        label: str,
        formula: str,
        *,
        compute: str | None = None,
        source: str | None = None,
        emphasis: bool = True,
    ) -> None:
        self.r += 1
        cell = Cell(
            c=2,
            type="formula",
            xlsx_name=key,
            format="money",
            compute=compute,
            source=source,
            formula=formula,
            emphasis=emphasis,
        )
        self._add(Row(self.r, "formula", label=label, cells=(_label(1, label, emphasis=emphasis), cell)))

    def section(self, section: bss.Section) -> None:
        """A schema section: heading, one money row per line, a subtotal
        that subtracts the contra rows."""
        self.heading(section.label)
        for row in section.rows:
            path = ("sections", section.key, row.key)
            if getattr(row, "text", False):
                self.field(row.key, row.label, path, hint=row.hint)
            else:
                self.money(row.key, row.label, path, hint=row.hint, flags=_line_flags(row))
        self.formula(
            section.subtotal_key,
            section.subtotal_label,
            _section_formula(section),
            compute=section.subtotal_key,
        )


def _label(c: int, text: str, *, colspan: int = 1, emphasis: bool = False) -> Cell:
    return Cell(c=c, type="label", label=text, colspan=colspan, emphasis=emphasis)


def _line_flags(row: bss.LineRow) -> tuple[str, ...]:
    return tuple(flag for flag in ("addback", "contra", "owner_comp") if getattr(row, flag, False))


def _summed_rows(section: bss.Section) -> list[bss.LineRow]:
    return [row for row in section.rows if not getattr(row, "text", False)]


def _section_formula(section: bss.Section) -> str:
    """A SUM over the section's money rows when they sit in one unbroken run
    with nothing subtracted; explicit terms otherwise. A text row at the end
    of a section (the P&L's "what the other expenses cover") keeps the SUM."""
    rows = _summed_rows(section)
    if not rows:
        return "=0"
    contras = {row.key for row in rows if row.contra}
    positions = [index for index, row in enumerate(section.rows) if row in rows]
    contiguous = positions == list(range(positions[0], positions[-1] + 1))
    if not contras and contiguous:
        return f"=SUM({{{rows[0].key}}}:{{{rows[-1].key}}})"
    return "=" + "".join(("-" if row.key in contras else "+") + "{" + row.key + "}" for row in rows).lstrip("+")


def _slug(text: str) -> str:
    return _SLUG.sub("_", text.strip().lower()).strip("_")


def _header_fields(rows: _Rows, fields: tuple[bss.HeaderField, ...]) -> None:
    for item in fields:
        rows.field(item.key, item.label, ("header", item.key), input=item.input, options=item.options)


_FORM_COLUMNS = (Column(1, "", 46, "left"), Column(2, "", 20, "right"))


def _p_and_l() -> Sheet:
    schema = bss.SCHEMA_FOR["p_and_l"]
    rows = _Rows()
    rows.title(
        "Profit and Loss Statement",
        "Enter the figures for the period. Totals are calculated for you.",
    )
    _header_fields(rows, schema.header)
    by_key = {section.key: section for section in schema.sections}

    rows.blank()
    rows.section(by_key["revenue"])
    rows.blank()
    rows.section(by_key["operating_expenses"])
    rows.formula(
        "operating_income",
        "Operating income",
        "={gross_profit}-{total_operating_expenses}",
        compute="operating_income",
    )
    rows.blank()
    below = by_key["below_the_line"]
    rows.heading(below.label)
    for row in below.rows:
        rows.money(row.key, row.label, ("sections", below.key, row.key), hint=row.hint, flags=_line_flags(row))
    rows.formula(
        "net_income",
        "Net income",
        "={operating_income}+{other_income}-{income_taxes}",
        compute="net_income",
    )

    # The memo block: every row flagged addback, by the flag. taxes_and_licenses
    # is not flagged and so never appears here.
    rows.blank()
    rows.heading("EBITDA (memo)")
    addback_rows = [row for section in schema.sections for row in section.rows if row.addback]
    memo_keys = ["net_income"]
    for row in addback_rows:
        rows.formula(
            f"memo_{row.key}", f"Add: {row.label.lower()}", "={" + row.key + "}", source=row.key, emphasis=False
        )
        memo_keys.append(f"memo_{row.key}")
    rows.formula("ebitda", "EBITDA (memo)", "=" + "+".join("{" + key + "}" for key in memo_keys), compute="ebitda")
    owner_rows = [row for section in schema.sections for row in section.rows if row.owner_comp]
    rows.formula(
        "owner_compensation",
        "Owner compensation (add-back candidate, not added)",
        "=" + "+".join("{" + row.key + "}" for row in owner_rows),
        compute="owner_compensation",
        emphasis=False,
    )
    rows.note(
        "Notes: describe what \"Other expenses\" covers, and anything a reader should know "
        "about this period."
    )
    rows.field("notes", "Notes", ("notes",))
    return _sheet("p_and_l", _FORM_COLUMNS, rows.rows, freeze=(0, 1))


def _balance_sheet() -> Sheet:
    schema = bss.SCHEMA_FOR["balance_sheet"]
    rows = _Rows()
    rows.title(
        "Balance Sheet",
        "Enter every balance as of one date. Totals are calculated for you.",
    )
    _header_fields(rows, schema.header)
    by_key = {section.key: section for section in schema.sections}

    rows.blank()
    for key in ("current_assets", "fixed_assets", "other_assets"):
        rows.section(by_key[key])
    rows.formula(
        "total_assets",
        "Total assets",
        "={total_current_assets}+{total_fixed_assets}+{total_other_assets}",
        compute="total_assets",
    )
    rows.blank()
    for key in ("current_liabilities", "long_term_liabilities"):
        rows.section(by_key[key])
    rows.formula(
        "total_liabilities",
        "Total liabilities",
        "={total_current_liabilities}+{total_long_term_liabilities}",
        compute="total_liabilities",
    )
    rows.blank()
    equity = by_key["equity"]
    rows.heading(equity.label)
    for row in equity.rows:
        rows.money(row.key, row.label, ("sections", equity.key, row.key), hint=row.hint, flags=_line_flags(row))
    summed = _summed_rows(equity)
    typed = "".join(("-" if row.contra else "+") + "{" + row.key + "}" for row in summed).lstrip("+")
    # Typed when any equity line is typed; implied from assets less liabilities
    # when the whole section is blank — the same rule the on-screen form uses.
    rows.formula(
        "total_equity",
        "Total equity (implied from assets less liabilities when left blank)",
        f"=IF(COUNT({{{summed[0].key}}}:{{{summed[-1].key}}})=0,"
        f"{{total_assets}}-{{total_liabilities}},{typed})",
        compute="total_equity",
    )
    rows.blank()
    rows.formula(
        "total_liabilities_and_equity",
        "Total liabilities and equity",
        "={total_liabilities}+{total_equity}",
        compute="total_liabilities_and_equity",
    )
    rows.formula(
        "imbalance",
        "Unreconciled difference (assets less liabilities and equity)",
        "={total_assets}-{total_liabilities_and_equity}",
        compute="imbalance",
        emphasis=False,
    )
    rows.note(
        "A difference other than zero means the sheet does not balance. Leave the equity "
        "section blank to have equity implied."
    )
    rows.field("notes", "Notes", ("notes",))
    return _sheet("balance_sheet", _FORM_COLUMNS, rows.rows, freeze=(0, 1))


# --- PFS ---------------------------------------------------------------------


def schedule_columns(spec: Any) -> list[tuple[str, str, str]]:
    """(storage key, slug, label) per column of a PFS schedule.

    `Schedule.fields` pairs each column's key with its label: the key is what
    a stored row is addressed by and what the cell is named after. A schedule
    that only lists labels (the shape before keys existed, still what a
    hand-built spec may carry) is keyed by label and named by its slug — which
    is the same word the schema chose, so nothing is named twice.
    """
    fields = getattr(spec, "fields", None)
    if fields:
        return [(key, key, label) for key, label in fields]
    out = []
    for column in spec.columns:
        if isinstance(column, str):
            out.append((column, _slug(column), column))
        else:
            key = column.key
            out.append((key, key, getattr(column, "label", key)))
    return out


def _schedule_cell_type(slug: str) -> tuple[str, str]:
    """(cell type, format) of a schedule column."""
    if slug in _PFS_MONEY_COLUMNS:
        return "money", "money"
    if slug in _PFS_DATE_COLUMNS:
        return "date", "date"
    if slug in _PFS_COUNT_COLUMNS:
        return "text", "count"
    return "text", "text"


def _pfs_columns() -> tuple[Column, ...]:
    return (
        Column(1, "", 46, "left"),
        Column(2, "", 20, "right"),
        *(Column(c, "", 18, "left") for c in PFS_SCHEDULE_COLUMNS[2:]),
    )


def _pfs(schedule_rows: Mapping[str, Sequence[str]]) -> Sheet:
    rows = _Rows()
    rows.title(
        "Personal Financial Statement",
        "One statement per owner. Totals are calculated for you.",
    )
    rows.field("name", "Name", ("applicant", "name"))
    rows.field("business_name", "Business name", ("applicant", "business_name"))
    rows.field("home_address", "Home address", ("applicant", "home_address"))
    rows.field("business_phone", "Business phone", ("applicant", "business_phone"))
    rows.field("statement_date", "As of", (PFS_AS_OF_KEY,), input="date")

    def block(heading: str, section: str, summary, total_key: str, total_label: str) -> None:
        rows.blank()
        rows.heading(heading)
        for row in summary:
            rows.money(row.key, row.label, (section, row.key), flags=("liquid",) if row.liquid else ())
        rows.formula(
            total_key,
            total_label,
            f"=SUM({{{summary[0].key}}}:{{{summary[-1].key}}})",
            compute=total_key,
        )

    block("Assets", "assets", pfs_schema.ASSET_ROWS, "total_assets", "Total assets")
    block("Liabilities", "liabilities", pfs_schema.LIABILITY_ROWS, "total_liabilities", "Total liabilities")
    rows.formula("net_worth", "Net worth", "={total_assets}-{total_liabilities}", compute="net_worth")
    block("Source of income (annual)", "income", pfs_schema.INCOME_ROWS, "total_income", "Total income")
    block(
        "Contingent liabilities",
        "contingent",
        pfs_schema.CONTINGENT_ROWS,
        "total_contingent",
        "Total contingent liabilities",
    )

    # Sections 2 to 8: the supporting schedules, one block each. No separator
    # rows and two lines apiece — see PFS_SCHEDULE_ROWS for the budget.
    for spec in pfs_schema.SCHEDULES:
        columns = schedule_columns(spec)
        placed = list(zip(PFS_SCHEDULE_COLUMNS, columns, strict=False))
        rows.heading(spec.label, colspan=len(columns), block=spec.key)
        rows.r += 1
        rows._add(
            Row(
                rows.r,
                "colhead",
                cells=tuple(_label(c, label) for c, (_, _, label) in placed),
                block=spec.key,
            )
        )
        for ordinal, row_key in enumerate(schedule_rows.get(spec.key, ()), start=1):
            rows.r += 1
            cells = []
            for c, (storage_key, slug, _) in placed:
                cell_type, fmt = _schedule_cell_type(slug)
                cells.append(
                    Cell(
                        c=c,
                        type=cell_type,
                        key=f"{spec.key}.{row_key}.{slug}",
                        xlsx_name=f"{spec.key}.r{ordinal}.{slug}",
                        format=fmt,
                        editable=True,
                        path=("schedules", spec.key, row_key, storage_key),
                    )
                )
            rows._add(
                Row(rows.r, "data", cells=tuple(cells), block=spec.key, row_key=row_key, ordinal=ordinal)
            )

    rows.note(
        "No Social Security Number is collected on this form. Add more lines to any "
        "schedule on screen through the link your advisor sends.",
        colspan=len(PFS_SCHEDULE_COLUMNS),
    )
    return _sheet("pfs", _pfs_columns(), rows.rows, freeze=(0, 1))


# --- Debt schedule ------------------------------------------------------------


def _debt_columns() -> tuple[Column, ...]:
    widths = {"lender": 28, "debt_type": 16, "collateral": 24, "notes": 30}
    return tuple(
        Column(
            index,
            label,
            widths.get(key, 15),
            "right" if key in _DEBT_MONEY or key == "rate" else "left",
        )
        for index, (key, label) in enumerate(zip(DEBT_COLUMNS, DEBT_COLUMN_LABELS, strict=True), start=1)
    )


def _debt_cell_type(key: str) -> tuple[str, str]:
    if key in _DEBT_MONEY:
        return "money", "money"
    if key == "rate":
        return "rate", "ratio"
    if key in _DEBT_DATES:
        return "date", "date"
    if key in _DEBT_CHOICES:
        return "select", "text"
    return "text", "text"


def _debt_schedule(row_keys: Sequence[str]) -> Sheet:
    rows = _Rows()
    rows.r += 1
    rows._add(Row(1, "title", label="Business Debt Schedule", cells=(_label(1, "Business Debt Schedule"),)))
    subtitle = "One line per outstanding business debt. Totals are calculated for you."
    rows.r += 1
    rows._add(Row(2, "subtitle", label=subtitle, cells=(_label(1, subtitle),)))
    rows.field("business_name", "Business name", ("business_name",))
    rows.blank()
    # Row 5 is the column head; the workbook keeps row 6 for the hidden key
    # row beneath it, so the first line is row 7 (`ds.r1.lender` is D7's
    # neighbour, and `test_the_debt_schedule_header...` pins it).
    rows.r += 1
    rows._add(
        Row(
            5,
            "colhead",
            cells=tuple(_label(index, label) for index, label in enumerate(DEBT_COLUMN_LABELS, start=1)),
            block="debts",
        )
    )
    rows.r += 1  # the hidden key row
    for ordinal, row_key in enumerate(row_keys, start=1):
        rows.r += 1
        cells = []
        for index, key in enumerate(DEBT_COLUMNS, start=1):
            cell_type, fmt = _debt_cell_type(key)
            cells.append(
                Cell(
                    c=index,
                    type=cell_type,
                    key=f"{row_key}.{key}",
                    xlsx_name=f"r{ordinal}.{key}",
                    format=fmt,
                    editable=True,
                    options=_DEBT_CHOICES.get(key),
                    path=("debts", row_key, key),
                )
            )
        rows._add(Row(rows.r, "data", cells=tuple(cells), block="debts", row_key=row_key, ordinal=ordinal))

    rows.r += 1
    totals = [_label(1, "Total", emphasis=True)]
    for key in ("balance", "monthly_payment"):
        column = DEBT_COLUMNS.index(key) + 1
        if row_keys:
            formula = f"=SUM({{{row_keys[0]}.{key}}}:{{{row_keys[-1]}.{key}}})"
        else:
            formula = "=0"
        totals.append(
            Cell(
                c=column,
                type="formula",
                xlsx_name=f"total_{key}",
                format="money",
                compute=f"total_{key}",
                formula=formula,
                emphasis=True,
            )
        )
    rows._add(Row(rows.r, "formula", label="Total", cells=tuple(totals), block="debts"))
    return _sheet("debt_schedule", _debt_columns(), rows.rows, freeze=(5, 1))


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def _row_keys(spec: int | Sequence[str]) -> list[str]:
    if isinstance(spec, int):
        return [f"r{number}" for number in range(1, spec + 1)]
    return [str(key) for key in spec]


def layout(
    kind: str,
    *,
    debt_rows: int | Sequence[str] = DEBT_ROWS,
    schedule_rows: int | Mapping[str, int | Sequence[str]] | None = None,
) -> Sheet:
    """The sheet of one kind.

    `debt_rows` and `schedule_rows` are either a count (that many blank lines,
    keyed `r1`…`rN` — the template) or the row keys themselves (a body's row
    ids — the grid). `schedule_rows` may be one count for every schedule or a
    mapping by schedule key; a schedule left out gets PFS_SCHEDULE_ROWS.
    """
    if kind in bss.KINDS:
        return _p_and_l() if kind == "p_and_l" else _balance_sheet()
    if kind == "debt_schedule":
        return _debt_schedule(_row_keys(debt_rows))
    if kind == "pfs":
        if schedule_rows is None or isinstance(schedule_rows, int):
            count = PFS_SCHEDULE_ROWS if schedule_rows is None else schedule_rows
            per = {spec.key: _row_keys(count) for spec in pfs_schema.SCHEDULES}
        else:
            per = {
                spec.key: _row_keys(schedule_rows.get(spec.key, PFS_SCHEDULE_ROWS))
                for spec in pfs_schema.SCHEDULES
            }
        return _pfs(per)
    raise KeyError(kind)


def _list_row_key(row: Any, ordinal: int) -> str:
    """A stored list row's identity: its id when it has one, else its ordinal."""
    if isinstance(row, dict):
        found = str(row.get("id") or row.get("row_id") or "").strip()
        if found:
            return found
    return f"r{ordinal}"


def layout_for_body(kind: str, body: Mapping[str, Any] | None) -> Sheet:
    """The layout whose list rows are the body's rows, keyed by identity."""
    body = body or {}
    if kind == "debt_schedule":
        debts = body.get("debts") or []
        return layout(kind, debt_rows=[_list_row_key(row, n) for n, row in enumerate(debts, start=1)])
    if kind == "pfs":
        schedules = body.get("schedules") or {}
        per = {
            spec.key: [
                _list_row_key(row, n)
                for n, row in enumerate(schedules.get(spec.key) or [], start=1)
            ]
            for spec in pfs_schema.SCHEDULES
        }
        return layout(kind, schedule_rows=per)
    return layout(kind)


def input_cells(sheet: Sheet) -> list[tuple[Row, Cell]]:
    """Every cell a person types into, in row order."""
    return [(row, cell) for row in sheet.rows for cell in row.cells if cell.key is not None]


def compute_keys(kind: str) -> list[str]:
    """The `totals` entries the sheet's formula cells show, in row order."""
    return [
        cell.compute
        for row in layout(kind).rows
        for cell in row.cells
        if cell.type == "formula" and cell.compute is not None
    ]


def debt_totals(body: Mapping[str, Any] | None) -> dict[str, Decimal]:
    """The two sums the debt-schedule sheet shows, computed the way
    `debt_rows_from_body` reads a figure."""
    debts = (body or {}).get("debts") or []
    out = {"total_balance": Decimal("0"), "total_monthly_payment": Decimal("0")}
    for row in debts:
        if not isinstance(row, dict):
            continue
        out["total_balance"] += pfs_schema._amount(row.get("balance"))
        out["total_monthly_payment"] += pfs_schema._amount(row.get("monthly_payment"))
    return out


def totals(kind: str, body: Mapping[str, Any] | None) -> dict[str, Any]:
    """The computed values of one kind, by the schema that owns it."""
    body = dict(body or {})
    if kind in bss.KINDS:
        return bss.totals(kind, body)
    if kind == "pfs":
        return pfs_schema.totals(body)
    if kind == "debt_schedule":
        return debt_totals(body)
    raise KeyError(kind)


# --- Reading and writing bodies through the layout ---------------------------


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _find_list_row(rows: list, row_key: str) -> int | None:
    match = _ORDINAL.match(row_key)
    if match:
        index = int(match.group(1)) - 1
        return index if 0 <= index < len(rows) else None
    for index, row in enumerate(rows):
        if isinstance(row, dict) and str(row.get("id") or row.get("row_id") or "") == row_key:
            return index
    return None


def _get(body: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = body
    for index, step in enumerate(path):
        if isinstance(node, list):
            found = _find_list_row(node, step)
            if found is None:
                return None
            node = node[found]
        elif isinstance(node, dict):
            node = node.get(step)
        else:
            return None
        if node is None and index < len(path) - 1:
            return None
    return node


def _is_row_step(path: tuple[str, ...], index: int) -> bool:
    """Whether path[index] is a row key inside a list — `debts.<row>.<col>`
    and `schedules.<schedule>.<row>.<col>` are the two list-shaped paths."""
    if path[0] == "debts":
        return index == 1
    return path[0] == "schedules" and index == 2


def _set(body: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    """Write `value` at `path`, creating the containers on the way and
    appending a list row the body does not hold yet."""
    node: Any = body
    for index, step in enumerate(path[:-1]):
        if isinstance(node, list):
            found = _find_list_row(node, step)
            if found is None:
                match = _ORDINAL.match(step)
                if match:
                    while len(node) < int(match.group(1)):
                        node.append({})
                    found = int(match.group(1)) - 1
                else:
                    node.append({"id": step})
                    found = len(node) - 1
            if not isinstance(node[found], dict):
                node[found] = {}
            node = node[found]
            continue
        child = node.get(step)
        if _is_row_step(path, index + 1):
            if not isinstance(child, list):
                child = []
                node[step] = child
        elif not isinstance(child, dict):
            child = {}
            node[step] = child
        node = child
    node[path[-1]] = value


def flatten(kind: str, body: Mapping[str, Any] | None) -> dict[str, str]:
    """Every input cell's value as the raw string, keyed by cell key.

    List rows are keyed by identity (`{id}.{column}`) when the body carries
    ids, by ordinal otherwise, so the map round-trips through `unflatten`.
    """
    body = dict(body or {})
    if kind == "pfs":
        # Schedule rows stored before the column keys existed are keyed by
        # label; read them the way the schema does, without writing back.
        body["schedules"] = pfs_schema.normalize_schedule_rows(body)
    sheet = layout_for_body(kind, body)
    return {cell.key: _as_text(_get(body, cell.path)) for _, cell in input_cells(sheet) if cell.path}


def _path_for_key(kind: str, key: str) -> tuple[str, ...]:
    static = layout(kind).by_key.get(key)
    if static is not None and static[1].path is not None:
        return static[1].path
    if kind == "debt_schedule":
        row_key, _, column = key.rpartition(".")
        if row_key and column in DEBT_COLUMNS:
            return ("debts", row_key, column)
    if kind == "pfs":
        parts = key.split(".")
        if len(parts) == 3 and parts[0] in pfs_schema.SCHEDULES_BY_KEY:
            spec = pfs_schema.SCHEDULES_BY_KEY[parts[0]]
            for storage_key, slug, _ in schedule_columns(spec):
                if slug == parts[2]:
                    return ("schedules", spec.key, parts[1], storage_key)
    raise KeyError(key)


def unflatten(
    kind: str, values: Mapping[str, Any], base: Mapping[str, Any] | None
) -> dict[str, Any]:
    """A copy of `base` with `values` (cell key → raw string) written in.

    Blank stays null: an emptied cell is stored as None, the way an untouched
    line on an empty body is. A key the layout does not know raises KeyError
    so a caller can refuse it rather than store it somewhere nobody reads.
    A list row the base does not hold is appended (by id, or padded to the
    ordinal), which is how a new debt or schedule line arrives.
    """
    body = copy.deepcopy(dict(base or {}))
    for key, raw in values.items():
        path = _path_for_key(kind, str(key))
        text = _as_text(raw)
        _set(body, path, text if text != "" else None)
    return body
