"""Read spreadsheets (.xlsx, legacy .xls, .csv) into rows.

One reader for everything that needs to look inside a spreadsheet: the
per-file AI analysis attaches the rows as a JSON text block, and the file
review screens show them as a table. Both paths want the same facts:

- what kind of file the bytes really are (uploads carry browser-declared
  types, and .xlsx often lands as application/octet-stream);
- the visible sheets, in order, with each row's cells as display text;
- what was left out, said plainly, because a sheet cut at N rows is only
  half read and the reader must know.

Two cases the previous reader got wrong and this one handles on purpose:

- Workbooks saved without a ``<dimension>`` record (web exports, Numbers,
  some Google Sheets downloads) report no size at all in openpyxl's
  read-only mode. The rows are still there; we walk them instead of
  trusting ``max_row``. Three client workbooks on production were read as
  "completely empty" for exactly this reason.
- Legacy .xls (BIFF) needs xlrd, which also tells an encrypted workbook and
  a Word/PowerPoint/Outlook file (the same OLE2 container) apart from a
  spreadsheet.

Nothing here touches storage or the database; callers hand in bytes.
"""

from __future__ import annotations

import csv
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO, StringIO
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLS_CONTENT_TYPE = "application/vnd.ms-excel"
CSV_CONTENT_TYPE = "text/csv"
CSV_CONTENT_TYPES = frozenset({"text/csv", "application/csv", "text/tab-separated-values"})
OLE2_SIGNATURE = b"\xd0\xcf\x11\xe0"
SPREADSHEET_SUFFIXES = (".xlsx", ".xlsm", ".xls", ".csv", ".tsv")
CSV_DELIMITERS = ",\t;|"

KIND_XLSX = "xlsx"
KIND_XLS = "xls"
KIND_CSV = "csv"

# A failed read names its cause so the UI and the analysis row can say what
# the client has to do about it.
ERROR_LOCKED = "locked"
ERROR_NOT_A_WORKBOOK = "not_a_workbook"
ERROR_EMPTY = "empty"
ERROR_PARSE_FAILED = "parse_failed"

# Rows beyond the limit are counted, not read, so "500 of 1,284 rows" can be
# reported. Counting stops at this many rows so a huge export stays cheap.
_COUNT_CEILING = 50_000


@dataclass(frozen=True)
class Limits:
    sheets: int = 8
    rows: int = 500
    cols: int = 40
    cell_chars: int = 200
    # 0 disables the second (formula) pass over an .xlsx.
    formulas: int = 20


DEFAULT_LIMITS = Limits()
# The preview shows exactly what the AI was given, so it reads with the same
# bounds; it only skips the formula pass.
PREVIEW_LIMITS = Limits(formulas=0)

# Bumped when the reader's behaviour changes enough that a file it could not
# read before deserves another try. Skipped rows are stamped with it so the
# cache re-opens them once, not on every review.
READER_VERSION = 1


@dataclass
class Sheet:
    name: str
    rows: list[list[str]]
    total_rows: int
    total_cols: int
    truncated_rows: bool = False
    truncated_cols: bool = False
    # total_rows stopped counting at _COUNT_CEILING; the sheet has more.
    count_capped: bool = False
    warnings: list[str] = field(default_factory=list)
    formulas: list[dict[str, str]] = field(default_factory=list)


@dataclass
class Workbook:
    kind: str
    sheet_names: list[str]
    sheets: list[Sheet]
    warnings: list[str] = field(default_factory=list)
    encoding: str | None = None
    delimiter: str | None = None


class SpreadsheetError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --- what the bytes are -------------------------------------------------------


def is_spreadsheet_name(file_name: str | None, content_type: str | None = None) -> bool:
    """Cheap gate from metadata alone, before any bytes are fetched."""

    name = (file_name or "").lower()
    declared = (content_type or "").split(";", 1)[0].strip().lower()
    return (
        name.endswith(SPREADSHEET_SUFFIXES)
        or declared in {XLSX_CONTENT_TYPE, XLS_CONTENT_TYPE, *CSV_CONTENT_TYPES}
        or "spreadsheet" in declared
    )


def _decode_sample(raw: bytes) -> str | None:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return raw.decode("cp1252")
        except UnicodeDecodeError:
            return None


def _looks_textual(sample: str) -> bool:
    if not sample:
        return False
    printable = sum(1 for char in sample if char.isprintable() or char.isspace())
    return printable / len(sample) > 0.95


def sniff_delimiter(sample: str) -> str | None:
    try:
        return csv.Sniffer().sniff(sample, delimiters=CSV_DELIMITERS).delimiter
    except csv.Error:
        return None


def sniff_kind(raw: bytes, *, declared_content_type: str = "", file_name: str = "") -> str | None:
    """Which reader the bytes want, or None when they are not a spreadsheet."""

    name = (file_name or "").lower()
    declared = (declared_content_type or "").split(";", 1)[0].strip().lower()
    if raw.startswith(OLE2_SIGNATURE):
        return KIND_XLS
    if raw.startswith(b"PK"):
        try:
            with zipfile.ZipFile(BytesIO(raw)) as archive:
                if "xl/workbook.xml" in archive.namelist():
                    return KIND_XLSX
                return None
        except zipfile.BadZipFile:
            pass  # a CSV can start with the letters PK
    sample = _decode_sample(raw[:8192])
    if sample is None or not _looks_textual(sample):
        return None
    if (
        sniff_delimiter(sample)
        or name.endswith((".csv", ".tsv"))
        or declared in CSV_CONTENT_TYPES
        # Windows browsers declare a .csv as Excel; textual bytes settle it.
        # An HTML or XML "Export to Excel" saved as .xls is not a table.
        or (declared == XLS_CONTENT_TYPE and not sample.lstrip().startswith("<"))
    ):
        return KIND_CSV
    return None


# --- cells ---------------------------------------------------------------------


def compact_cell(value: Any, *, cell_chars: int = DEFAULT_LIMITS.cell_chars) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        value = int(value)
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if len(text) > cell_chars:
        return text[: cell_chars - 1] + "…"
    return text


def trim_empty_tail(values: list[str]) -> list[str]:
    trimmed = list(values)
    while trimmed and not trimmed[-1]:
        trimmed.pop()
    return trimmed


def _row_warnings(sheet: Sheet, limits: Limits) -> list[str]:
    warnings: list[str] = []
    if sheet.truncated_rows:
        counted = f"more than {sheet.total_rows:,}" if sheet.count_capped else f"{sheet.total_rows:,}"
        warnings.append(f"Only the first {limits.rows:,} of {counted} rows were included.")
    if sheet.truncated_cols:
        warnings.append(f"Only the first {limits.cols} of {sheet.total_cols} columns were included.")
    return warnings


# --- .xlsx ---------------------------------------------------------------------


def _formula_text(value: Any) -> Any:
    """openpyxl hands back ArrayFormula / DataTableFormula objects for array
    and data-table cells; keep the formula text, never the object's repr."""

    if value is None or isinstance(value, str):
        return value
    text = getattr(value, "text", None)
    return text if isinstance(text, str) else None


def _read_openpyxl_sheet(name: str, values_ws: Any, formulas_ws: Any, limits: Limits) -> Sheet:
    rows: list[list[str]] = []
    formulas: list[dict[str, str]] = []
    total_rows = 0
    total_cols = 0
    truncated_rows = False
    truncated_cols = False
    count_capped = False
    # The <dimension> record is not trusted: absent, read-only mode reports no
    # size at all; present but stale (a placeholder "A1" from some exporters),
    # read-only iter_rows() stops at that bound with no error. Clearing it puts
    # every sheet on the path that walks all rows. iter_rows captures the
    # bounds when called, so the reset comes first.
    for worksheet in (values_ws, formulas_ws):
        reset = getattr(worksheet, "reset_dimensions", None)
        if callable(reset):
            reset()
    formula_rows = formulas_ws.iter_rows() if formulas_ws is not None else None
    for row_index, value_row in enumerate(values_ws.iter_rows(), start=1):
        formula_row: tuple[Any, ...] = next(formula_rows, ()) if formula_rows is not None else ()
        values = [cell.value for cell in value_row]
        formula_values = [_formula_text(cell.value) for cell in formula_row]
        width = len(trim_empty_tail([compact_cell(v, cell_chars=limits.cell_chars) for v in values]))
        formula_width = len(trim_empty_tail([compact_cell(v, cell_chars=limits.cell_chars) for v in formula_values]))
        width = max(width, formula_width)
        if width == 0:
            continue
        total_rows += 1
        total_cols = max(total_cols, width)
        if total_rows > limits.rows:
            truncated_rows = True
            if total_rows > _COUNT_CEILING:
                count_capped = True
                total_rows = _COUNT_CEILING
                break
            continue
        if width > limits.cols:
            truncated_cols = True
        compacted: list[str] = []
        for col_index in range(min(width, limits.cols)):
            cached = values[col_index] if col_index < len(values) else None
            formula = formula_values[col_index] if col_index < len(formula_values) else None
            shown = cached if cached is not None else formula
            compacted.append(compact_cell(shown, cell_chars=limits.cell_chars))
            if (
                isinstance(formula, str)
                and formula.startswith("=")
                and len(formulas) < limits.formulas
            ):
                formulas.append(
                    {
                        "cell": f"{get_column_letter(col_index + 1)}{row_index}",
                        "formula": compact_cell(formula, cell_chars=limits.cell_chars),
                        "cached_value": compact_cell(cached, cell_chars=limits.cell_chars),
                    }
                )
        rows.append(trim_empty_tail(compacted))
    sheet = Sheet(
        name=name,
        rows=rows,
        total_rows=total_rows,
        total_cols=total_cols,
        truncated_rows=truncated_rows,
        truncated_cols=truncated_cols,
        count_capped=count_capped,
        formulas=formulas,
    )
    sheet.warnings = _row_warnings(sheet, limits)
    if not rows:
        sheet.warnings.append("Sheet appears empty.")
    if limits.formulas and len(formulas) >= limits.formulas:
        sheet.warnings.append(f"Only the first {limits.formulas} formulas were included.")
    return sheet


def _read_xlsx(raw: bytes, limits: Limits) -> Workbook:
    values_wb = None
    formulas_wb = None
    try:
        values_wb = load_workbook(BytesIO(raw), read_only=True, data_only=True)
        formulas_wb = load_workbook(BytesIO(raw), read_only=True, data_only=False) if limits.formulas else None
        names = list(values_wb.sheetnames)
        # Workbook.worksheets excludes chart sheets, which have no rows to
        # walk; sheet_names above still lists them.
        visible = [
            worksheet.title for worksheet in values_wb.worksheets
            if getattr(worksheet, "sheet_state", "visible") == "visible"
        ]
        if not visible:
            raise SpreadsheetError(ERROR_EMPTY, "This workbook has no visible sheets.")
        warnings: list[str] = []
        if len(visible) > limits.sheets:
            warnings.append(f"Only the first {limits.sheets} of {len(visible)} visible sheets were included.")
        sheets = [
            _read_openpyxl_sheet(name, values_wb[name], formulas_wb[name] if formulas_wb is not None else None, limits)
            for name in visible[: limits.sheets]
        ]
        return Workbook(kind=KIND_XLSX, sheet_names=names, sheets=sheets, warnings=warnings)
    except SpreadsheetError:
        raise
    except Exception as exc:  # noqa: BLE001 - any malformed workbook is one failure kind
        raise SpreadsheetError(ERROR_PARSE_FAILED, f"The workbook could not be opened: {type(exc).__name__}.") from exc
    finally:
        for workbook in (values_wb, formulas_wb):
            if workbook is not None:
                try:
                    workbook.close()
                except Exception:  # noqa: BLE001
                    pass


# --- legacy .xls ---------------------------------------------------------------


def _ole_is_encrypted_package(raw: bytes) -> bool:
    """An encrypted .xlsx is an OLE2 container holding an EncryptedPackage stream."""

    try:
        from xlrd.compdoc import CompDoc

        return CompDoc(raw).get_named_stream("EncryptedPackage") is not None
    except Exception:  # noqa: BLE001
        return False


def _xls_cell(cell: Any, datemode: int) -> Any:
    import xlrd

    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return None
    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            value = xlrd.xldate_as_datetime(cell.value, datemode)
        except Exception:  # noqa: BLE001
            return cell.value
        return value.date() if isinstance(value, datetime) and value.time() == datetime.min.time() else value
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    if cell.ctype == xlrd.XL_CELL_ERROR:
        return "#ERROR"
    return cell.value


def _read_xls(raw: bytes, limits: Limits) -> Workbook:
    import xlrd

    if _ole_is_encrypted_package(raw):
        raise SpreadsheetError(ERROR_LOCKED, "This Excel file is password protected.")
    try:
        book = xlrd.open_workbook(file_contents=raw, formatting_info=False)
    except xlrd.XLRDError as exc:
        message = str(exc).lower()
        if "encrypt" in message or "password" in message:
            raise SpreadsheetError(ERROR_LOCKED, "This Excel file is password protected.") from exc
        if "can't find workbook" in message or "expected bof" in message or "not a known type" in message:
            raise SpreadsheetError(
                ERROR_NOT_A_WORKBOOK,
                "This is an old-format Office file but not an Excel workbook (Word, PowerPoint or Outlook).",
            ) from exc
        raise SpreadsheetError(ERROR_PARSE_FAILED, f"The workbook could not be opened: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise SpreadsheetError(ERROR_PARSE_FAILED, f"The workbook could not be opened: {type(exc).__name__}.") from exc
    names = [sheet.name for sheet in book.sheets()]
    visible = [sheet for sheet in book.sheets() if sheet.visibility == 0]
    if not visible:
        raise SpreadsheetError(ERROR_EMPTY, "This workbook has no visible sheets.")
    warnings: list[str] = []
    if len(visible) > limits.sheets:
        warnings.append(f"Only the first {limits.sheets} of {len(visible)} visible sheets were included.")
    sheets: list[Sheet] = []
    for worksheet in visible[: limits.sheets]:
        rows: list[list[str]] = []
        total_rows = 0
        total_cols = 0
        truncated_rows = False
        for row_index in range(worksheet.nrows):
            values = [_xls_cell(worksheet.cell(row_index, col), book.datemode) for col in range(worksheet.ncols)]
            compacted = trim_empty_tail([compact_cell(v, cell_chars=limits.cell_chars) for v in values])
            if not compacted:
                continue
            total_rows += 1
            total_cols = max(total_cols, len(compacted))
            if total_rows > limits.rows:
                truncated_rows = True
                continue
            rows.append(compacted[: limits.cols])
        sheet = Sheet(
            name=worksheet.name,
            rows=rows,
            total_rows=total_rows,
            total_cols=total_cols,
            truncated_rows=truncated_rows,
            truncated_cols=total_cols > limits.cols,
        )
        sheet.warnings = _row_warnings(sheet, limits)
        if not rows:
            sheet.warnings.append("Sheet appears empty.")
        sheets.append(sheet)
    return Workbook(kind=KIND_XLS, sheet_names=names, sheets=sheets, warnings=warnings)


# --- .csv ----------------------------------------------------------------------


def decode_text(raw: bytes) -> tuple[str, str]:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8-replace"


def _read_csv(raw: bytes, limits: Limits, *, file_name: str = "") -> Workbook:
    try:
        text, encoding = decode_text(raw)
        delimiter = sniff_delimiter(text[:8192]) or ("\t" if file_name.lower().endswith(".tsv") else ",")
        rows: list[list[str]] = []
        total_rows = 0
        total_cols = 0
        truncated_rows = False
        truncated_cols = False
        count_capped = False
        for row in csv.reader(StringIO(text), delimiter=delimiter):
            compacted = trim_empty_tail([compact_cell(v, cell_chars=limits.cell_chars) for v in row])
            if not compacted:
                continue
            total_rows += 1
            total_cols = max(total_cols, len(compacted))
            if total_rows > limits.rows:
                truncated_rows = True
                if total_rows > _COUNT_CEILING:
                    count_capped = True
                    total_rows = _COUNT_CEILING
                    break
                continue
            if len(compacted) > limits.cols:
                truncated_cols = True
            rows.append(compacted[: limits.cols])
    except SpreadsheetError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SpreadsheetError(ERROR_PARSE_FAILED, f"The CSV could not be parsed: {type(exc).__name__}.") from exc
    if not rows:
        raise SpreadsheetError(ERROR_EMPTY, "This CSV did not contain readable rows.")
    stem = file_name.rsplit("/", 1)[-1].rsplit(".", 1)[0] if file_name else ""
    sheet = Sheet(
        name=stem or "CSV",
        rows=rows,
        total_rows=total_rows,
        total_cols=total_cols,
        truncated_rows=truncated_rows,
        truncated_cols=truncated_cols,
        count_capped=count_capped,
    )
    sheet.warnings = _row_warnings(sheet, limits)
    return Workbook(
        kind=KIND_CSV,
        sheet_names=[sheet.name],
        sheets=[sheet],
        encoding=encoding,
        delimiter=delimiter,
    )


# --- entry points ----------------------------------------------------------------


def read_workbook(
    raw: bytes,
    kind: str | None = None,
    *,
    limits: Limits = DEFAULT_LIMITS,
    declared_content_type: str = "",
    file_name: str = "",
) -> Workbook:
    """Rows for any spreadsheet; raises SpreadsheetError with a named cause."""

    kind = kind or sniff_kind(raw, declared_content_type=declared_content_type, file_name=file_name)
    if kind == KIND_XLSX:
        return _read_xlsx(raw, limits)
    if kind == KIND_XLS:
        return _read_xls(raw, limits)
    if kind == KIND_CSV:
        return _read_csv(raw, limits, file_name=file_name)
    raise SpreadsheetError(ERROR_NOT_A_WORKBOOK, "This file is not a spreadsheet.")


def read_warnings(workbook: Workbook) -> list[str]:
    """Everything that was left out, as sentences a reviewer can act on."""

    notes = list(workbook.warnings)
    for sheet in workbook.sheets:
        for warning in sheet.warnings:
            if warning == "Sheet appears empty.":
                continue
            notes.append(f"{sheet.name}: {warning}" if len(workbook.sheets) > 1 else warning)
    return notes


def ai_payload(workbook: Workbook, *, file_id: str, file_name: str, limits: Limits = DEFAULT_LIMITS) -> dict[str, Any]:
    """The JSON text block the per-file analysis prompt already understands."""

    if workbook.kind == KIND_CSV:
        sheet = workbook.sheets[0]
        return {
            "file_id": file_id,
            "file_name": file_name,
            "type": "csv_table",
            "encoding": workbook.encoding,
            "delimiter": workbook.delimiter,
            "rows_sampled": len(sheet.rows),
            "rows_total": sheet.total_rows,
            "rows_total_is_minimum": sheet.count_capped,
            "columns_sampled_limit": limits.cols,
            "warnings": list(sheet.warnings),
            "rows": sheet.rows,
        }
    return {
        "file_id": file_id,
        "file_name": file_name,
        "type": "xls_workbook" if workbook.kind == KIND_XLS else "xlsx_workbook",
        "sheet_names": workbook.sheet_names,
        "visible_sheets_included": [sheet.name for sheet in workbook.sheets],
        "warnings": list(workbook.warnings),
        "sheets": [
            {
                "name": sheet.name,
                "dimensions": {"rows": sheet.total_rows, "columns": sheet.total_cols, "rows_is_minimum": sheet.count_capped},
                "warnings": list(sheet.warnings),
                "rows": sheet.rows,
                "formulas": sheet.formulas,
            }
            for sheet in workbook.sheets
        ],
    }


def preview_payload(workbook: Workbook) -> dict[str, Any]:
    """What the review screens render: sheets as rows, plus what was cut."""

    return {
        "kind": workbook.kind,
        "sheet_names": workbook.sheet_names,
        "warnings": list(workbook.warnings),
        "encoding": workbook.encoding,
        "delimiter": workbook.delimiter,
        "sheets": [
            {
                "name": sheet.name,
                "rows": sheet.rows,
                "total_rows": sheet.total_rows,
                "total_cols": sheet.total_cols,
                "truncated_rows": sheet.truncated_rows,
                "truncated_cols": sheet.truncated_cols,
                "count_capped": sheet.count_capped,
                "warnings": list(sheet.warnings),
            }
            for sheet in workbook.sheets
        ],
    }
