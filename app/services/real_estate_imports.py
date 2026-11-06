"""Review safeguards for source schedules; no publication or evidence approval."""

from __future__ import annotations

import copy
import io
import zipfile
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException

from app.services import real_estate_schema as res
from app.services import sheet_layout


def read_workbook(raw: bytes, *, allow_unmapped: bool = False) -> dict | None:
    """Read the named QC template, not guessed positions or formula totals."""
    from openpyxl import load_workbook

    from app.services.financial_templates_xlsx import read_real_estate_workbook

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > 2000 or sum(member.file_size for member in members) > 20_000_000:
                raise HTTPException(422, "This workbook is too large for real estate field mapping")
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)
        named_template = any(name.startswith("res.") for name in workbook.defined_names)
        workbook.close()
        if allow_unmapped and not named_template:
            return None
        body = read_real_estate_workbook(raw)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(422, "Use a readable Qualified Commercial real estate Excel template, or upload a PDF of the existing schedule") from error
    if not res.properties(body) and not res.mortgages(body) and body.get("no_real_estate") is not True:
        raise HTTPException(422, "This workbook has no real estate inputs in the supported template. Upload the existing schedule as a PDF or use the real estate Excel template")
    return body


def workbook_cells(raw: bytes) -> dict[str, str]:
    """Bounded source anchors from the same reader used by file analysis."""
    from openpyxl.utils import get_column_letter

    from app.services import spreadsheets

    workbook = spreadsheets.read_workbook(raw, "xlsx")
    return {f"{sheet.name}!{get_column_letter(column)}{row_index}": str(value)
            for sheet in workbook.sheets for row_index, row in enumerate(sheet.rows, 1)
            for column, value in enumerate(row, 1) if value not in (None, "")}


def with_conflicts(mappings: list[dict], current: dict) -> list[dict]:
    """Expose corrections against the current draft, including cached remaps."""
    before = sheet_layout.flatten(res.KIND, current)
    result = copy.deepcopy(mappings)
    for mapping in result:
        old, new = before.get(mapping["key"]), mapping["value"]
        mapping["before"] = old if old not in (None, "") else None
        if old in (None, "", "unknown"):
            mapping["conflict"] = False
            continue
        try:
            mapping["conflict"] = Decimal(str(old).replace(",", "")) != Decimal(str(new).replace(",", ""))
        except (InvalidOperation, ValueError, TypeError):
            mapping["conflict"] = " ".join(str(old).split()).casefold() != " ".join(str(new).split()).casefold()
    return result


def validate_selection(current: dict, mappings: dict) -> None:
    """An accepted figure must belong to a named property and linked loan."""
    merged = sheet_layout.unflatten(res.KIND, mappings, current)
    known_properties = {str(row.get("id")) for row in res.properties(current) if str(row.get("address") or "").strip()}
    selected = {tuple(key.split(".")[:2]) for key in mappings if key.count(".") == 2}
    properties = {str(row.get("id")): row for row in merged.get("properties") or [] if str(row.get("address") or "").strip()}
    for block, row_id in selected:
        if block == "properties" and row_id not in known_properties:
            if not str(mappings.get(f"properties.{row_id}.address") or "").strip():
                raise HTTPException(422, {"code": "real_estate_import_property_required", "message": "Select the street address when importing a new property so its figures belong to a named row."})
        if block == "mortgages":
            row = next((row for row in merged.get("mortgages") or [] if str(row.get("id")) == row_id), {})
            if not str(row.get("lender") or "").strip() or str(row.get("property_row_id")) not in properties:
                raise HTTPException(422, {"code": "real_estate_import_loan_link_required", "message": "Select the lender and its property link, including the new property's address if needed, before applying mortgage figures."})
