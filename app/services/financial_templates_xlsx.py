"""The four financial templates the desk hands out, generated from the schemas.

The owner shared four spreadsheets — a profit and loss, a balance sheet, a
personal financial statement and a business debt schedule — and asked for them
hosted on the website. They are not committed as binaries: each is written
here, by openpyxl, from the same row definitions the on-screen forms render
(`business_statement_schema`, `pfs_schema`, `DEBT_COLUMNS`), so the spreadsheet
a borrower downloads can never drift from the form their advisor sends.

What the generator fixes in the originals, deliberately:

- dates are blank date cells with a comment, never a title string like
  "January 1 to December 31" of some year — so a template never says the
  wrong year and the analyzer never reads a year off a heading;
- one 2-decimal accounting format everywhere, totals included;
- subtotals and totals are formulas on locked cells; the sheet is protected
  and only the input cells are unlocked, so a total cannot be typed over;
- an EBITDA memo block on the P&L from the `addback` rows, and "Total
  liabilities and equity" plus an "Unreconciled difference" line on the
  balance sheet;
- a print area, no styled empty range, every sheet well under the analyzer's
  budget (`MAX_SPREADSHEET_ROWS`, `MAX_SPREADSHEET_SHEETS`), so a filled copy
  uploaded back is read whole;
- a hidden column carrying the schema key beside every input, and a workbook
  defined name equal to it (`pl.gross_revenue`, `bs.cash_in_bank`,
  `ds.r7.lender`, `pfs.cash_on_hand`), so a deterministic reader is a
  follow-on rather than a rewrite;
- pinned document properties and zip timestamps, so the bytes are the same on
  every build and the edge can cache them by content.

A workbook written by openpyxl carries no cached formula values. The analyzer
shows both the value view and the formula view, and its typed prompt block
tells the model to copy the lines and leave a total null when the document
does not print one; the extractor then computes totals from the lines.

Labels are English. The resource page says so on its Spanish card.
"""

from __future__ import annotations

import functools
import zipfile
from datetime import datetime
from io import BytesIO

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill, Protection
from openpyxl.utils import get_column_letter
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.worksheet import Worksheet

from app.services import business_statement_schema as bss
from app.services import pfs_schema
from app.services.financial_statements import DEBT_COLUMN_LABELS, DEBT_COLUMNS

MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: Public URL slug → workbook kind.
SLUGS: dict[str, str] = {
    "profit-and-loss": "p_and_l",
    "balance-sheet": "balance_sheet",
    "business-debt-schedule": "debt_schedule",
    "personal-financial-statement": "pfs",
}

#: The attachment filename per kind. Each passes
#: `bucket_evidence.filename_evidence_classification`, so a filled copy
#: uploaded back to a room routes to its checklist row before analysis.
ATTACHMENT_FILENAMES: dict[str, str] = {
    "p_and_l": "Qualified Commercial - Profit and Loss Statement.xlsx",
    "balance_sheet": "Qualified Commercial - Balance Sheet.xlsx",
    "debt_schedule": "Qualified Commercial - Business Debt Schedule.xlsx",
    "pfs": "Qualified Commercial - Personal Financial Statement.xlsx",
}

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

#: Rows in the debt-schedule grid. The owner's template had fifteen.
DEBT_ROWS = 15

MONEY_FORMAT = '#,##0.00;(#,##0.00);"-"'
DATE_FORMAT = "yyyy-mm-dd"
RATE_FORMAT = "0.00"

_AUTHOR = "Qualified Commercial"
#: Pinned so two builds are byte-identical. Not a year that appears in a cell.
_PINNED_STAMP = datetime(1980, 1, 1)

_TITLE_FONT = Font(bold=True, size=14)
_HEADING_FONT = Font(bold=True, size=11)
_BOLD = Font(bold=True)
_MUTED = Font(italic=True, color="6B7280")
_HEADING_FILL = PatternFill("solid", fgColor="F3F4F6")
_INPUT_FILL = PatternFill("solid", fgColor="FFFBEB")
_UNLOCKED = Protection(locked=False)
_RIGHT = Alignment(horizontal="right")
_WRAP = Alignment(wrap_text=True, vertical="top")


class _Form:
    """A label / input / hidden-key layout, one line per row."""

    LABEL, INPUT, KEY = "A", "B", "C"

    def __init__(self, wb: Workbook, kind: str) -> None:
        self.ws: Worksheet = wb.active
        self.ws.title = SHEET_TITLES[kind]
        self.wb = wb
        self.prefix = NAME_PREFIX[kind]
        self.row = 0
        self.cells: dict[str, int] = {}
        self.ws.column_dimensions[self.LABEL].width = 46
        self.ws.column_dimensions[self.INPUT].width = 20
        self.ws.column_dimensions[self.KEY].hidden = True

    def _name(self, key: str, row: int) -> None:
        name = f"{self.prefix}.{key}"
        self.wb.defined_names[name] = DefinedName(
            name=name, attr_text=f"'{self.ws.title}'!${self.INPUT}${row}"
        )
        self.cells[key] = row

    def title(self, text: str, subtitle: str) -> None:
        self.row += 1
        cell = self.ws.cell(row=self.row, column=1, value=text)
        cell.font = _TITLE_FONT
        self.row += 1
        cell = self.ws.cell(row=self.row, column=1, value=subtitle)
        cell.font = _MUTED
        self.row += 1

    def heading(self, text: str) -> None:
        self.row += 1
        for column in (1, 2):
            cell = self.ws.cell(row=self.row, column=column)
            cell.fill = _HEADING_FILL
            cell.font = _HEADING_FONT
        self.ws.cell(row=self.row, column=1).value = text

    def text_field(self, key: str, label: str, *, input: str = "text", options=None, hint: str | None = None) -> None:
        self.row += 1
        self.ws.cell(row=self.row, column=1, value=label)
        cell = self.ws.cell(row=self.row, column=2)
        cell.protection = _UNLOCKED
        cell.fill = _INPUT_FILL
        if input == "date":
            cell.number_format = DATE_FORMAT
            cell.comment = Comment(hint or "Enter as a date (year-month-day).", _AUTHOR)
        elif input == "select" and options:
            validation = DataValidation(
                type="list", formula1='"' + ",".join(options) + '"', allow_blank=True
            )
            self.ws.add_data_validation(validation)
            validation.add(cell)
            cell.comment = Comment(hint or "Choose: " + " or ".join(options) + ".", _AUTHOR)
        elif hint:
            cell.comment = Comment(hint, _AUTHOR)
        self.ws.cell(row=self.row, column=3, value=key)
        self._name(key, self.row)

    def money_row(self, key: str, label: str, *, hint: str | None = None) -> int:
        self.row += 1
        self.ws.cell(row=self.row, column=1, value=label)
        cell = self.ws.cell(row=self.row, column=2)
        cell.number_format = MONEY_FORMAT
        cell.protection = _UNLOCKED
        cell.fill = _INPUT_FILL
        cell.alignment = _RIGHT
        if hint:
            cell.comment = Comment(hint, _AUTHOR)
        self.ws.cell(row=self.row, column=3, value=key)
        self._name(key, self.row)
        return self.row

    def formula_row(self, key: str, label: str, formula: str, *, emphasis: bool = True) -> int:
        self.row += 1
        label_cell = self.ws.cell(row=self.row, column=1, value=label)
        cell = self.ws.cell(row=self.row, column=2, value=formula)
        cell.number_format = MONEY_FORMAT
        cell.alignment = _RIGHT
        if emphasis:
            label_cell.font = _BOLD
            cell.font = _BOLD
        self.ws.cell(row=self.row, column=3, value=key)
        self._name(key, self.row)
        return self.row

    def note(self, text: str) -> None:
        self.row += 1
        cell = self.ws.cell(row=self.row, column=1, value=text)
        cell.font = _MUTED
        cell.alignment = _WRAP
        self.ws.merge_cells(start_row=self.row, start_column=1, end_row=self.row, end_column=2)

    def blank(self) -> None:
        self.row += 1

    def ref(self, key: str) -> str:
        return f"{self.INPUT}{self.cells[key]}"

    def section(self, section: bss.Section) -> int:
        """A schema section: heading, one money row per line, a subtotal
        formula that subtracts the contra rows."""
        self.heading(section.label)
        rows = [self.money_row(row.key, row.label, hint=row.hint) for row in section.rows]
        contras = {row.key for row in section.rows if row.contra}
        if not contras:
            formula = f"=SUM({self.INPUT}{rows[0]}:{self.INPUT}{rows[-1]})"
        else:
            terms = [
                ("-" if row.key in contras else "+") + f"{self.INPUT}{number}"
                for row, number in zip(section.rows, rows, strict=True)
            ]
            formula = "=" + "".join(terms).lstrip("+")
        return self.formula_row(section.subtotal_key, section.subtotal_label, formula)

    def finish(self) -> None:
        self.ws.print_area = f"A1:B{self.row}"
        self.ws.protection.sheet = True
        self.ws.sheet_view.showGridLines = True


def _header_fields(form: _Form, fields: tuple[bss.HeaderField, ...]) -> None:
    for field in fields:
        form.text_field(field.key, field.label, input=field.input, options=field.options)


def _build_p_and_l(wb: Workbook) -> None:
    schema = bss.SCHEMA_FOR["p_and_l"]
    form = _Form(wb, "p_and_l")
    form.title(
        "Profit and Loss Statement",
        "Enter the figures for the period. Totals are calculated for you.",
    )
    _header_fields(form, schema.header)
    by_key = {section.key: section for section in schema.sections}

    form.blank()
    form.section(by_key["revenue"])
    form.blank()
    form.section(by_key["operating_expenses"])
    form.formula_row(
        "operating_income",
        "Operating income",
        f"={form.ref('gross_profit')}-{form.ref('total_operating_expenses')}",
    )
    form.blank()
    below = by_key["below_the_line"]
    form.heading(below.label)
    for row in below.rows:
        form.money_row(row.key, row.label, hint=row.hint)
    form.formula_row(
        "net_income",
        "Net income",
        f"={form.ref('operating_income')}+{form.ref('other_income')}-{form.ref('income_taxes')}",
    )

    # The memo block: every row flagged addback, by the flag. taxes_and_licenses
    # is not flagged and so never appears here.
    form.blank()
    form.heading("EBITDA (memo)")
    addback_rows = [
        row for section in schema.sections for row in section.rows if row.addback
    ]
    memo_refs = [form.ref("net_income")]
    for row in addback_rows:
        number = form.formula_row(
            f"memo_{row.key}", f"Add: {row.label.lower()}", f"={form.ref(row.key)}", emphasis=False
        )
        memo_refs.append(f"{form.INPUT}{number}")
    form.formula_row("ebitda", "EBITDA (memo)", "=" + "+".join(memo_refs))
    owner_rows = [row for section in schema.sections for row in section.rows if row.owner_comp]
    form.formula_row(
        "owner_compensation",
        "Owner compensation (add-back candidate, not added)",
        "=" + "+".join(form.ref(row.key) for row in owner_rows),
        emphasis=False,
    )
    form.note(
        "Notes: describe what \"Other expenses\" covers, and anything a reader should know "
        "about this period."
    )
    form.text_field("notes", "Notes")
    form.finish()


def _build_balance_sheet(wb: Workbook) -> None:
    schema = bss.SCHEMA_FOR["balance_sheet"]
    form = _Form(wb, "balance_sheet")
    form.title(
        "Balance Sheet",
        "Enter every balance as of one date. Totals are calculated for you.",
    )
    _header_fields(form, schema.header)
    by_key = {section.key: section for section in schema.sections}

    form.blank()
    for key in ("current_assets", "fixed_assets", "other_assets"):
        form.section(by_key[key])
    form.formula_row(
        "total_assets",
        "Total assets",
        f"={form.ref('total_current_assets')}+{form.ref('total_fixed_assets')}"
        f"+{form.ref('total_other_assets')}",
    )
    form.blank()
    for key in ("current_liabilities", "long_term_liabilities"):
        form.section(by_key[key])
    form.formula_row(
        "total_liabilities",
        "Total liabilities",
        f"={form.ref('total_current_liabilities')}+{form.ref('total_long_term_liabilities')}",
    )
    form.blank()
    equity = by_key["equity"]
    form.heading(equity.label)
    equity_rows = [form.money_row(row.key, row.label, hint=row.hint) for row in equity.rows]
    typed = "".join(
        ("-" if row.contra else "+") + f"{form.INPUT}{number}"
        for row, number in zip(equity.rows, equity_rows, strict=True)
    ).lstrip("+")
    first, last = equity_rows[0], equity_rows[-1]
    # Typed when any equity line is typed; implied from assets less liabilities
    # when the whole section is blank — the same rule the on-screen form uses.
    form.formula_row(
        "total_equity",
        "Total equity (implied from assets less liabilities when left blank)",
        f"=IF(COUNT({form.INPUT}{first}:{form.INPUT}{last})=0,"
        f"{form.ref('total_assets')}-{form.ref('total_liabilities')},{typed})",
    )
    form.blank()
    form.formula_row(
        "total_liabilities_and_equity",
        "Total liabilities and equity",
        f"={form.ref('total_liabilities')}+{form.ref('total_equity')}",
    )
    form.formula_row(
        "imbalance",
        "Unreconciled difference (assets less liabilities and equity)",
        f"={form.ref('total_assets')}-{form.ref('total_liabilities_and_equity')}",
        emphasis=False,
    )
    form.note(
        "A difference other than zero means the sheet does not balance. Leave the equity "
        "section blank to have equity implied."
    )
    form.text_field("notes", "Notes")
    form.finish()


def _build_pfs(wb: Workbook) -> None:
    form = _Form(wb, "pfs")
    form.title(
        "Personal Financial Statement",
        "One statement per owner. Totals are calculated for you.",
    )
    form.text_field("name", "Name")
    form.text_field("business_name", "Business name")
    form.text_field("home_address", "Home address")
    form.text_field("business_phone", "Business phone")
    form.text_field("statement_date", "As of", input="date")

    def block(heading: str, rows, total_key: str, total_label: str) -> None:
        form.blank()
        form.heading(heading)
        numbers = [form.money_row(row.key, row.label) for row in rows]
        form.formula_row(
            total_key, total_label, f"=SUM({form.INPUT}{numbers[0]}:{form.INPUT}{numbers[-1]})"
        )

    block("Assets", pfs_schema.ASSET_ROWS, "total_assets", "Total assets")
    block("Liabilities", pfs_schema.LIABILITY_ROWS, "total_liabilities", "Total liabilities")
    form.formula_row(
        "net_worth", "Net worth", f"={form.ref('total_assets')}-{form.ref('total_liabilities')}"
    )
    block("Source of income (annual)", pfs_schema.INCOME_ROWS, "total_income", "Total income")
    block(
        "Contingent liabilities",
        pfs_schema.CONTINGENT_ROWS,
        "total_contingent",
        "Total contingent liabilities",
    )
    form.blank()
    form.note(
        "The supporting schedules (notes payable, stocks and bonds, real estate, other "
        "property, unpaid taxes, other liabilities, life insurance, retirement accounts) are "
        "completed on screen through the link your advisor sends. No Social Security Number "
        "is collected on this form."
    )
    form.finish()


def _build_debt_schedule(wb: Workbook) -> None:
    ws: Worksheet = wb.active
    ws.title = SHEET_TITLES["debt_schedule"]
    prefix = NAME_PREFIX["debt_schedule"]
    columns = len(DEBT_COLUMNS)
    key_column = columns + 1  # hidden, carries the row key

    def name(key: str, column: int, row: int) -> None:
        full = f"{prefix}.{key}"
        wb.defined_names[full] = DefinedName(
            name=full, attr_text=f"'{ws.title}'!${get_column_letter(column)}${row}"
        )

    ws.cell(row=1, column=1, value="Business Debt Schedule").font = _TITLE_FONT
    ws.cell(
        row=2,
        column=1,
        value="One line per outstanding business debt. Totals are calculated for you.",
    ).font = _MUTED
    ws.cell(row=3, column=1, value="Business name")
    business = ws.cell(row=3, column=2)
    business.protection = _UNLOCKED
    business.fill = _INPUT_FILL
    name("business_name", 2, 3)

    header_row = 5
    for index, (key, label) in enumerate(zip(DEBT_COLUMNS, DEBT_COLUMN_LABELS, strict=True), start=1):
        cell = ws.cell(row=header_row, column=index, value=label)
        cell.font = _HEADING_FONT
        cell.fill = _HEADING_FILL
        cell.alignment = _WRAP
        # The schema key under its label, on a hidden row.
        ws.cell(row=header_row + 1, column=index, value=key)
    ws.row_dimensions[header_row + 1].hidden = True
    ws.column_dimensions[get_column_letter(key_column)].hidden = True

    secured = DataValidation(type="list", formula1='"secured,unsecured"', allow_blank=True)
    paid = DataValidation(type="list", formula1='"current,delinquent"', allow_blank=True)
    ws.add_data_validation(secured)
    ws.add_data_validation(paid)

    first = header_row + 2
    last = first + DEBT_ROWS - 1
    for number in range(1, DEBT_ROWS + 1):
        row = first + number - 1
        ws.cell(row=row, column=key_column, value=f"r{number}")
        for index, key in enumerate(DEBT_COLUMNS, start=1):
            cell = ws.cell(row=row, column=index)
            cell.protection = _UNLOCKED
            cell.fill = _INPUT_FILL
            if key in {"original_amount", "balance", "monthly_payment"}:
                cell.number_format = MONEY_FORMAT
            elif key == "rate":
                cell.number_format = RATE_FORMAT
            elif key in {"originated_on", "maturity_on"}:
                cell.number_format = DATE_FORMAT
            elif key == "secured":
                secured.add(cell)
            elif key == "payment_status":
                paid.add(cell)
            name(f"r{number}.{key}", index, row)

    totals_row = last + 1
    ws.cell(row=totals_row, column=1, value="Total").font = _BOLD
    for key in ("balance", "monthly_payment"):
        column = DEBT_COLUMNS.index(key) + 1
        letter = get_column_letter(column)
        cell = ws.cell(row=totals_row, column=column, value=f"=SUM({letter}{first}:{letter}{last})")
        cell.number_format = MONEY_FORMAT
        cell.font = _BOLD
        name(f"total_{key}", column, totals_row)

    widths = {"lender": 28, "debt_type": 16, "collateral": 24, "notes": 30}
    for index, key in enumerate(DEBT_COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(index)].width = widths.get(key, 15)
    ws.print_area = f"A1:{get_column_letter(columns)}{totals_row}"
    ws.page_setup.orientation = "landscape"
    ws.protection.sheet = True


_BUILDERS = {
    "p_and_l": _build_p_and_l,
    "balance_sheet": _build_balance_sheet,
    "debt_schedule": _build_debt_schedule,
    "pfs": _build_pfs,
}


def _pin_zip_timestamps(raw: bytes) -> bytes:
    """openpyxl stamps zip entries with the wall clock. Rewrite them with one
    fixed timestamp so the same workbook is the same bytes on every build."""
    source = zipfile.ZipFile(BytesIO(raw))
    out = BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            pinned = zipfile.ZipInfo(info.filename, date_time=(1980, 1, 1, 0, 0, 0))
            pinned.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(pinned, source.read(info.filename))
    return out.getvalue()


@functools.cache
def build_workbook(kind: str) -> bytes:
    """The workbook for one kind, as bytes. Built once per process."""
    if kind not in _BUILDERS:
        raise KeyError(kind)
    wb = Workbook()
    wb.properties.creator = _AUTHOR
    wb.properties.lastModifiedBy = _AUTHOR
    wb.properties.title = SHEET_TITLES[kind]
    wb.properties.created = _PINNED_STAMP
    wb.properties.modified = _PINNED_STAMP
    _BUILDERS[kind](wb)
    buffer = BytesIO()
    wb.save(buffer)
    return _pin_zip_timestamps(buffer.getvalue())


def workbook_for_slug(slug: str) -> tuple[str, bytes] | None:
    """(attachment filename, bytes) for a public slug, or None for an unknown one."""
    kind = SLUGS.get(slug)
    if kind is None:
        return None
    return ATTACHMENT_FILENAMES[kind], build_workbook(kind)


def schema_keys(kind: str) -> list[str]:
    """Every key the workbook of this kind must carry a defined name for."""
    if kind in bss.KINDS:
        schema = bss.SCHEMA_FOR[kind]
        keys = [field.key for field in schema.header]
        for section in schema.sections:
            keys.extend(row.key for row in section.rows)
            keys.append(section.subtotal_key)
        return keys
    if kind == "pfs":
        keys = ["name", "business_name", "home_address", "business_phone", "statement_date"]
        for rows in (
            pfs_schema.ASSET_ROWS,
            pfs_schema.LIABILITY_ROWS,
            pfs_schema.INCOME_ROWS,
            pfs_schema.CONTINGENT_ROWS,
        ):
            keys.extend(row.key for row in rows)
        keys.extend(["total_assets", "total_liabilities", "net_worth", "total_income", "total_contingent"])
        return keys
    if kind == "debt_schedule":
        return ["business_name"] + [
            f"r{number}.{column}" for number in range(1, DEBT_ROWS + 1) for column in DEBT_COLUMNS
        ]
    raise KeyError(kind)
