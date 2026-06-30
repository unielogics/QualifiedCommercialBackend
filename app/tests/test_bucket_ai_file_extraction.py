from __future__ import annotations

import json
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

from openpyxl import Workbook

from app.services.bucket_ai import (
    MAX_SPREADSHEET_TEXT_CHARS,
    _extract_csv_text,
    _extract_xlsx_text,
    _is_csv_file,
    _is_legacy_xls_file,
    _is_xlsx_file,
    _limit_structured_text,
    _media_type,
)


def _file(name: str):
    return SimpleNamespace(id=uuid4(), file_name=name)


def test_extract_xlsx_includes_rows_formulas_and_sheet_warnings():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "May FS"
    sheet.append(["Account", "Amount"])
    sheet.append(["Gross sales", 1250000])
    sheet.append(["COGS", 850000])
    sheet["B4"] = "=B2-B3"

    hidden = workbook.create_sheet("Hidden")
    hidden.sheet_state = "hidden"
    hidden.append(["Should not", "appear"])

    raw = BytesIO()
    workbook.save(raw)

    extracted, skip = _extract_xlsx_text(_file("financials.xlsx"), raw.getvalue())

    assert skip is None
    payload = json.loads(extracted)
    assert payload["type"] == "xlsx_workbook"
    assert payload["sheet_names"] == ["May FS", "Hidden"]
    assert payload["visible_sheets_included"] == ["May FS"]
    assert payload["sheets"][0]["rows"][0] == ["Account", "Amount"]
    assert payload["sheets"][0]["rows"][1] == ["Gross sales", "1250000"]
    assert payload["sheets"][0]["formulas"][0]["cell"] == "B4"
    assert payload["sheets"][0]["formulas"][0]["formula"] == "=B2-B3"


def test_extract_csv_returns_structured_rows_and_warnings():
    raw = b"Name,Amount,Notes\nRent,4500,Monthly\nTaxes,6000,Annual\n"

    extracted, skip = _extract_csv_text(_file("rent_roll.csv"), raw)

    assert skip is None
    payload = json.loads(extracted)
    assert payload["type"] == "csv_table"
    assert payload["rows"] == [
        ["Name", "Amount", "Notes"],
        ["Rent", "4500", "Monthly"],
        ["Taxes", "6000", "Annual"],
    ]
    assert payload["warnings"] == []


def test_empty_csv_returns_parse_skip_reason():
    extracted, skip = _extract_csv_text(_file("empty.csv"), b"")

    assert extracted is None
    assert skip[0] == "csv_parse_failed"


def test_spreadsheet_file_type_detection_and_budget_limit():
    assert _is_xlsx_file(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "financials.bin",
    )
    assert _is_xlsx_file("application/octet-stream", "financials.xlsx")
    assert _is_legacy_xls_file("financials.xls")
    assert _is_csv_file("text/csv", "upload.bin")
    assert _is_csv_file("application/octet-stream", "rent_roll.csv")

    limited, truncated = _limit_structured_text("x" * (MAX_SPREADSHEET_TEXT_CHARS + 1), 10)
    assert limited == "x" * 10
    assert truncated is True


def test_existing_pdf_and_image_media_detection_unchanged():
    assert _media_type("application/pdf", "tax-return.bin") == "application/pdf"
    assert _media_type("application/octet-stream", "license.png") == "image/png"
    assert _media_type("image/jpeg", "photo.bin") == "image/jpeg"
