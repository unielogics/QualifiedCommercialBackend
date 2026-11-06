from __future__ import annotations

import copy
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from openpyxl import load_workbook

from app.services import dealer_forms_pdf, financial_workspace, sheet_layout, sheets
from app.services import financial_templates_xlsx as xlsx
from app.services import real_estate_schema as res


def example():
    prop = {"id": str(uuid4()), "address": "100 Example Avenue", "city": "Example City", "legal_owner": "Example LLC", "mortgage_status": "active",
            "estimated_value": "500000", "monthly_gross_rent": "4000", **dict.fromkeys(res.EXPENSE_KEYS, "0")}
    prop["monthly_property_tax"], prop["monthly_insurance"] = "300", "100"
    first = {"id": str(uuid4()), "property_row_id": prop["id"], "lender": "Example Bank", "loan_reference": "First-loan", "status": "active", "balance": "200000", "monthly_payment": "1500", "payment_basis": "including_escrow", "monthly_escrow": "400"}
    second = {**first, "id": str(uuid4()), "loan_reference": "Second-loan", "balance": "10000", "monthly_payment": "100", "payment_basis": "principal_interest", "monthly_escrow": None}
    return {**res.empty_body(), "owner_name": "Example LLC", "as_of": "2026-09-30", "properties": [prop], "mortgages": [first, second]}


def test_two_distinct_same_lender_liens_and_escrow_safe_cash_flow():
    body = res.validate(example())
    totals = res.totals(body)
    assert totals["property_count"] == 1 and totals["active_mortgage_count"] == 2
    assert totals["mortgage_balance_total"] == Decimal("210000")
    assert totals["monthly_mortgage_payments_total"] == Decimal("1600")
    assert totals["monthly_operating_expenses_total"] == Decimal("400")
    assert totals["estimated_equity"] == Decimal("290000")
    assert totals["monthly_cash_flow"] == Decimal("2400")  # 4000 - 400 operating - 1200 P&I.


def test_fresh_empty_is_unknown_but_explicit_none_and_free_clear_are_zero():
    assert set(res.totals(res.empty_body()).values()) == {None}
    assert set(res.totals({**res.empty_body(), "no_real_estate": True}).values()) == {0}
    body = example()
    body["properties"][0]["mortgage_status"], body["mortgages"] = "free_clear", []
    totals = res.totals(res.validate(body))
    assert totals["mortgage_balance_total"] == totals["monthly_mortgage_payments_total"] == 0
    assert totals["monthly_cash_flow"] == Decimal("3600")


@pytest.mark.parametrize("change", ["payment_basis", "escrow", "operating", "balance", "mortgage_status", "property_status", "unlinked"])
def test_missing_financial_inputs_never_become_a_zero_or_a_cash_flow_estimate(change):
    body = example()
    if change == "payment_basis":
        body["mortgages"][0]["payment_basis"] = "unknown"
    elif change == "escrow":
        body["mortgages"][0]["monthly_escrow"] = None
    elif change == "operating":
        body["properties"][0]["monthly_insurance"] = None
    elif change == "balance":
        body["mortgages"][0]["balance"] = None
    elif change == "mortgage_status":
        body["mortgages"][0]["status"] = "unknown"
    elif change == "property_status":
        body["properties"][0]["mortgage_status"] = "unknown"
    else:
        body["mortgages"][0]["property_row_id"] = None
    totals = res.totals(res.validate(body))
    if change == "balance":
        assert totals["mortgage_balance_total"] is None and totals["estimated_equity"] is None
    else:
        assert totals["monthly_cash_flow"] is None


@pytest.mark.parametrize("change", ["paid_off_balance", "free_clear", "orphan", "negative", "precision", "percentage", "escrow", "declaration", "duplicate"])
def test_contradictory_or_invalid_inputs_are_rejected(change):
    body = example()
    if change == "paid_off_balance":
        body["mortgages"][0]["status"] = "paid_off"
    elif change == "free_clear":
        body["properties"][0]["mortgage_status"] = "free_clear"
    elif change == "orphan":
        body["mortgages"][0]["property_row_id"] = str(uuid4())
    elif change == "negative":
        body["properties"][0]["estimated_value"] = "-1"
    elif change == "precision":
        body["mortgages"][0]["rate"] = "2.1234"
    elif change == "percentage":
        body["properties"][0]["ownership_pct"] = "101"
    elif change == "escrow":
        body["mortgages"][0]["monthly_escrow"] = "1501"
    elif change == "declaration":
        body["no_real_estate"] = True
    else:
        body["mortgages"][1]["id"] = body["mortgages"][0]["id"]
    with pytest.raises(HTTPException) as error:
        financial_workspace.validate_body(res.KIND, body)
    assert error.value.status_code == 422


def test_unknown_positive_balance_requests_evidence_without_asserting_accepted_totals():
    body = example()
    prop, loan = body["properties"][0], body["mortgages"][0]
    loan["status"] = "unknown"
    assert res.active_mortgage(loan) and res.mortgage_requires_statement(prop, loan)
    assert res.totals(body)["mortgage_balance_total"] is None
    assert not res.active_mortgage({"status": "unknown", "balance": 0})
    assert not res.mortgage_requires_statement(prop, {"status": "paid_off", "balance": 0})
    assert res.mortgage_requires_statement(prop)


def test_worksheet_ids_paths_new_rows_and_explicit_opt_in_scope():
    body = res.validate(example())
    prop_id, loan_id = body["properties"][0]["id"], body["mortgages"][0]["id"]
    flat = sheet_layout.flatten(res.KIND, body)
    assert flat[f"mortgages.{loan_id}.property_row_id"] == prop_id
    assert sheet_layout.layout_for_body(res.KIND, body).by_key[f"properties.{prop_id}.address"][1].path == ("properties", prop_id, "address")
    new = str(uuid4())
    updated = sheet_layout.unflatten(res.KIND, {f"properties.{new}.address": "200 Other Avenue", "no_real_estate": "false"}, body)
    stored = financial_workspace.validate_body(res.KIND, updated)
    assert stored["properties"][-1]["id"] == new
    assert stored["no_real_estate"] is False
    assert res.KIND not in sheets._scoped(None)
    assert sheets._scoped([res.KIND]) == [res.KIND]
    assert {row.block for row in sheets._layout_for(res.KIND, body).rows if row.kind == "data"} == {"properties", "mortgages"}
    shape = sheet_layout.layout_for_body(res.KIND, body)
    association = shape.by_key[f"mortgages.{loan_id}.property_row_id"][1]
    assert association.type == "select" and prop_id in association.options
    assert body["properties"][0]["address"] in dict(association.option_labels)[prop_id]
    assert {cell.type for row in shape.rows for cell in row.cells} <= {"label", "text", "money", "date", "select", "rate", "formula"}
    assert all(cell.format == "count" for row in shape.rows for cell in row.cells if cell.compute in {"property_count", "active_mortgage_count"})


def test_extracted_ids_are_new_and_loans_keep_only_source_property_associations():
    raw = example()
    old_prop = raw["properties"][0]["id"]
    raw["mortgages"][0]["statement_file_id"] = str(uuid4())
    sanitized = financial_workspace._sanitize_extracted_body(res.KIND, raw)
    new_prop = sanitized["properties"][0]["id"]
    assert new_prop != old_prop
    assert sanitized["mortgages"][0]["property_row_id"] == new_prop
    assert "statement_file_id" not in sanitized["mortgages"][0]
    assert financial_workspace.validate_body(res.KIND, sanitized)["mortgages"][0]["balance"] == "200000.00"


def test_import_alignment_preserves_property_and_distinct_loan_ids_without_name_only_merges():
    current = res.validate(example())
    proposed = financial_workspace._sanitize_extracted_body(res.KIND, current)
    warnings = []
    aligned, ids = res.align_import(proposed, current, warnings)
    assert aligned["properties"][0]["id"] == current["properties"][0]["id"]
    assert [row["id"] for row in aligned["mortgages"]] == [row["id"] for row in current["mortgages"]]
    assert all(row["property_row_id"] == current["properties"][0]["id"] for row in aligned["mortgages"])
    assert len(ids) == 3 and not warnings
    for row in current["mortgages"]:
        row["loan_reference"] = None
    proposed = financial_workspace._sanitize_extracted_body(res.KIND, current)
    aligned, _, = res.align_import(proposed, current, warnings)
    assert not {row["id"] for row in current["mortgages"]}.intersection(row["id"] for row in aligned["mortgages"])
    assert warnings


def test_pdf_html_preserves_unknowns_status_linkage_and_escapes_user_text():
    body = res.validate(example())
    body["properties"][0]["address"] = "<script>bad</script>"
    body["mortgages"][0]["rate"] = None
    html = dealer_forms_pdf.build_real_estate_schedule_html(body=body)
    assert "&lt;script&gt;bad&lt;/script&gt;" in html and "<script>bad</script>" not in html
    assert "First-loan" in html and "Second-loan" in html
    assert "Not stated" in html and "$2,400.00" in html


@pytest.mark.asyncio
async def test_publication_never_projects_property_mortgages_into_business_debt():
    db = SimpleNamespace(add=Mock(), execute=AsyncMock(), flush=AsyncMock())
    await financial_workspace._project(db, SimpleNamespace(id=uuid4()), SimpleNamespace(kind=res.KIND), example(), {}, actor=None, submit=True, pdf_file_id=uuid4())
    db.add.assert_not_called()
    db.execute.assert_not_awaited()


def test_xlsx_snapshot_preserves_ids_totals_all_columns_and_recomputes_edited_inputs():
    body = res.validate(example())
    raw = xlsx.build_real_estate_workbook(body)
    workbook = load_workbook(BytesIO(raw))
    worksheet = workbook.active
    assert not worksheet.column_dimensions["C"].hidden
    assert any("Current export totals" in str(cell.value) for row in worksheet for cell in row)
    destination = list(workbook.defined_names["res.monthly_cash_flow"].destinations)[0][1]
    assert worksheet[destination].value == 2400
    reread = xlsx.read_real_estate_workbook(raw)
    assert [row["id"] for row in reread["mortgages"]] == [row["id"] for row in body["mortgages"]]
    assert reread["mortgages"][0]["property_row_id"] == body["properties"][0]["id"]
    assert res.totals(reread)["monthly_cash_flow"] == Decimal("2400")
    destination = list(workbook.defined_names["res.properties.r1.monthly_gross_rent"].destinations)[0][1]
    worksheet[destination] = 5000
    edited = BytesIO()
    workbook.save(edited)
    assert res.totals(xlsx.read_real_estate_workbook(edited.getvalue()))["monthly_cash_flow"] == Decimal("3400")
    assert res.KIND not in xlsx.PACKET_KINDS


def test_declared_acroform_fields_map_both_collections_without_authoring_a_pdf():
    reader = SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: "100 Example Avenue")], get_fields=lambda: {
        "res.owner_name": {"/V": "Example LLC"}, "res.no_real_estate": {"/V": "/Off"},
        "res.properties.r1.address": {"/V": "100 Example Avenue"},
        "res.mortgages.r1.property_row_id": {"/V": "r1"}, "res.mortgages.r1.lender": {"/V": "Example Bank"},
        "res.properties.r9999.address": {"/V": "Outside bounded source"},
    })
    with patch("pypdf.PdfReader", return_value=reader):
        body, pages = financial_workspace._extract_acroform(res.KIND, b"mock source")
    assert body["properties"][0]["address"] == "100 Example Avenue"
    assert body["mortgages"][0]["property_row_id"] == "r1"
    assert len(body["properties"]) == 1 and pages == {1: "100 Example Avenue"}
    assert body["no_real_estate"] == "false"


def test_blank_xlsx_inputs_can_explicitly_link_a_mortgage_to_property_row_r1():
    workbook = load_workbook(BytesIO(xlsx.build_real_estate_workbook()))
    worksheet = workbook.active
    inputs = {"properties.r1.address": "100 Example Avenue", "properties.r1.mortgage_status": "active",
              "mortgages.r1.property_row_id": "r1", "mortgages.r1.lender": "Example Bank", "mortgages.r1.status": "active"}
    for key, value in inputs.items():
        destination = list(workbook.defined_names[f"res.{key}"].destinations)[0][1]
        worksheet[destination] = value
    output = BytesIO()
    workbook.save(output)
    body = xlsx.read_real_estate_workbook(output.getvalue())
    assert body["mortgages"][0]["property_row_id"] == body["properties"][0]["id"]
    assert res.totals(body)["mortgage_balance_total"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("blank_property", [False, True])
async def test_publish_requires_a_property_association_but_partial_drafts_remain_editable(blank_property):
    body = example()
    if blank_property:
        body["properties"][0] = {"id": body["properties"][0]["id"]}
    else:
        body["mortgages"][0]["property_row_id"] = None
    assert financial_workspace.validate_body(res.KIND, body)["mortgages"]  # Autosave accepts partial work.
    family = SimpleNamespace(id=uuid4(), current_version_id=None)
    draft = SimpleNamespace(revision=1, base_version_id=None, body=copy.deepcopy(body))
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)))
    with (patch.object(financial_workspace, "resolve_family", AsyncMock(return_value=(family, draft))),
          patch.object(financial_workspace, "_render", Mock()) as render,
          patch.object(financial_workspace, "_project", AsyncMock()) as project):
        with pytest.raises(HTTPException) as error:
            await financial_workspace.publish_version(db, SimpleNamespace(primary_bucket_id=uuid4()), kind=res.KIND,
                expected_revision=1, idempotency_key="real-estate-missing-property")
    assert error.value.status_code == 422 and error.value.detail["code"] == "real_estate_property_required"
    assert error.value.detail["missing_fields"]
    render.assert_not_called()
    project.assert_not_awaited()
    assert draft.body == body
