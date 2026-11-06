from __future__ import annotations

import copy
import hashlib
import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from openpyxl import Workbook

from app.models.bucket import BucketFile
from app.models.financial_form_version import (
    FinancialFormDraft,
    FinancialFormFamily,
    FinancialFormImport,
)
from app.services import (
    bucket_ai,
    financial_templates_xlsx,
    real_estate_imports,
    real_estate_schema,
)
from app.services import financial_workspace as ws
from app.services.bucket_evidence import (
    classifications_for_requested_doc,
    filename_evidence_classification,
)
from app.tests.test_financial_import_identity import ImportDB


def schedule():
    first, second = str(uuid4()), str(uuid4())
    return {**real_estate_schema.empty_body(), "owner_name": "Example Holdings", "as_of": "2026-10-31",
        "properties": [{"id": first, "address": "100 Fictional Street", "city": "Sample", "mortgage_status": "active", "estimated_value": "500000"},
                       {"id": second, "address": "200 Fictional Street", "city": "Sample", "mortgage_status": "active", "estimated_value": "350000"}],
        "mortgages": [{"id": str(uuid4()), "property_row_id": first, "lender": "Example Bank", "loan_reference": "First-1111", "status": "active", "balance": "200000", "monthly_payment": "1200"},
                      {"id": str(uuid4()), "property_row_id": first, "lender": "Example Bank", "loan_reference": "Second-2222", "status": "active", "balance": "50000", "monthly_payment": "400"},
                      {"id": str(uuid4()), "property_row_id": second, "lender": "Other Bank", "loan_reference": "Third-3333", "status": "active", "balance": "150000", "monthly_payment": "900"}]}


def setup_source(body=None, raw=b"%PDF fictional schedule", name="Existing schedule.pdf", classification="real_estate_schedule"):
    db = ImportDB()
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4())
    family = FinancialFormFamily(id=uuid4(), profile_id=profile.id, kind="real_estate_schedule", identity={"subject": "Example Holdings", "as_of": "2026-10-31"}, identity_key="test", label="Real estate", current_version_id=None)
    draft = FinancialFormDraft(family_id=family.id, body=real_estate_schema.validate(body or real_estate_schema.empty_body()), revision=2)
    file = SimpleNamespace(id=uuid4(), bucket_id=profile.primary_bucket_id, deleted_at=None, status="uploaded", content_type="application/pdf" if name.endswith(".pdf") else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", file_name=name,
                           content_hash=hashlib.sha256(raw).hexdigest(), uploaded_by_name="Client", created_at=ws.now())
    analysis = SimpleNamespace(id=uuid4(), classification=classification, content_hash=file.content_hash, analysis={"key_facts": {"owner_name": "Example Holdings", "as_of": "2026-10-31"}})
    db.objects[(FinancialFormFamily, family.id)] = family
    db.objects[(BucketFile, file.id)] = file
    return db, profile, family, draft, file, analysis


@pytest.mark.asyncio
async def test_pdf_schema_mapping_keeps_multiple_properties_liens_and_page_references():
    source = schedule()
    for index, row in enumerate(source["properties"], 1):
        old = row["id"]
        row["id"] = f"source-property-{index}"
        for loan in source["mortgages"]:
            if loan["property_row_id"] == old:
                loan["property_row_id"] = row["id"]
    source["mortgages"][0]["statement_file_id"] = str(uuid4())
    source["mortgages"][0]["mortgage_statement_import_ids"] = [str(uuid4())]
    source["mortgages"][0]["id"] = "source-loan-1"
    references = {"res.properties.source-property-1.address": {"page": 1, "quote": "100 Fictional Street"},
                  "mortgages.source-loan-1.balance": {"page": 2, "quote": "Current balance 200000"}}
    response = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps({"body": source, "references": references}))])
    acro = {**real_estate_schema.empty_body(), "no_real_estate": None}
    with (patch.object(ws, "_extract_acroform", return_value=(acro, {1: "100 Fictional Street", 2: "Current balance 200000"})),
          patch.object(bucket_ai, "_append_review_file_content", return_value=(True, 2, 0)),
          patch("app.services.ai.bedrock_client.get_client", return_value=object()),
          patch("app.services.ai.bedrock_client.model_heavy", return_value="test-model"),
          patch("app.services.ai.usage.tracked_messages_create", AsyncMock(return_value=response)) as model):
        body, refs, warnings, pages = await ws._extract_full_form(AsyncMock(), "real_estate_schedule", SimpleNamespace(id=uuid4()), b"%PDF fictional")
    assert len(body["properties"]) == 2 and len(body["mortgages"]) == 3
    assert body["mortgages"][0]["property_row_id"] == body["properties"][0]["id"]
    assert body["mortgages"][1]["property_row_id"] == body["properties"][0]["id"]
    assert body["mortgages"][2]["property_row_id"] == body["properties"][1]["id"]
    assert [row["loan_reference"] for row in body["mortgages"]] == ["First-1111", "Second-2222", "Third-3333"]
    assert "statement_file_id" not in body["mortgages"][0] and "mortgage_statement_import_ids" not in body["mortgages"][0]
    mappings = ws._mapping_records("real_estate_schedule", body, SimpleNamespace(id=uuid4()), None, "a" * 64, refs, pages)
    balance = next(row for row in mappings if row["key"] == f"mortgages.{body['mortgages'][0]['id']}.balance")
    assert balance["source"]["page"] == 2 and balance["confidence"] == "high"
    assert not warnings
    prompt = model.call_args.kwargs["messages"][0]["content"][0]["text"]
    assert "multiple liens" in prompt and "payment basis" in prompt


def test_alignment_preserves_controls_but_does_not_claim_omitted_old_answers():
    current = real_estate_schema.validate(schedule())
    current["mortgages"][0]["statement_file_id"] = str(uuid4())
    proposed = ws._sanitize_extracted_body("real_estate_schedule", schedule())
    proposed["mortgages"][0].pop("monthly_payment")
    aligned, _ = real_estate_schema.align_import(proposed, current, [])
    assert aligned["mortgages"][0]["id"] == current["mortgages"][0]["id"]
    assert aligned["mortgages"][0]["statement_file_id"] == current["mortgages"][0]["statement_file_id"]
    assert "monthly_payment" not in aligned["mortgages"][0]
    mappings = ws._mapping_records("real_estate_schedule", aligned, SimpleNamespace(id=uuid4()), None, "a" * 64)
    assert f"mortgages.{aligned['mortgages'][0]['id']}.monthly_payment" not in {row["key"] for row in mappings}


def test_duplicate_model_property_alias_does_not_link_to_arbitrary_property():
    body = schedule()
    body["properties"][0]["id"] = body["properties"][1]["id"] = "same-source-id"
    body["mortgages"][0]["property_row_id"] = "same-source-id"
    sanitized = ws._sanitize_extracted_body("real_estate_schedule", body)
    assert sanitized["mortgages"][0]["property_row_id"] is None


def test_unknown_extraction_defaults_cannot_overwrite_known_property_or_loan_status():
    current = real_estate_schema.validate(schedule())
    incoming = ws._sanitize_extracted_body("real_estate_schedule", current)
    for row in incoming["properties"]:
        row.pop("mortgage_status")
    for row in incoming["mortgages"]:
        row.pop("status")
    incoming = real_estate_schema.validate(incoming)
    proposed, _ = real_estate_schema.align_import(incoming, current, [])
    mappings = ws._mapping_records("real_estate_schedule", proposed, SimpleNamespace(id=uuid4()), None, "a" * 64)
    accepted = ws.sheet_layout.unflatten("real_estate_schedule", {row["key"]: row["value"] for row in mappings}, current)
    assert all(row["mortgage_status"] == "active" for row in accepted["properties"])
    assert all(row["status"] == "active" for row in accepted["mortgages"])


@pytest.mark.asyncio
async def test_reimport_updates_reviewed_fields_only_keeps_rows_links_and_original_immutable():
    initial = real_estate_schema.validate(schedule())
    statement = str(uuid4())
    initial["mortgages"][0]["statement_file_id"] = statement
    db, profile, family, draft, file, analysis = setup_source(initial)
    incoming = ws._sanitize_extracted_body("real_estate_schedule", initial)
    incoming["mortgages"][0]["balance"] = "190000"
    incoming["mortgages"][0].pop("monthly_payment")
    incoming["mortgages"][1]["balance"] = "45000"
    before = copy.deepcopy(draft.body)
    with (patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))),
          patch.object(ws, "_read_file", return_value=b"%PDF fictional schedule"),
          patch.object(ws, "_extract_full_form", AsyncMock(return_value=(incoming, {}, [], {}))),
          patch.object(ws, "load_workspace", AsyncMock(return_value={}))):
        result = await ws.propose_import(db, profile, kind="real_estate_schedule", file_id=file.id, family_id=family.id, analysis=analysis)
        assert draft.body == before and family.current_version_id is None
        assert len(db.versions) == 1 and db.versions[0].body == {} and db.versions[0].status == "original"
        chosen = f"mortgages.{initial['mortgages'][0]['id']}.balance"
        mapping = next(row for row in result["mappings"] if row["key"] == chosen)
        assert mapping["before"] == "200000.00" and mapping["conflict"] is True
        await ws.apply_import(db, profile, db.imports[-1].id, expected_revision=2, selected_keys=[chosen])
    assert len(draft.body["properties"]) == 2 and len(draft.body["mortgages"]) == 3
    assert draft.body["mortgages"][0]["balance"] == "190000.00"
    assert draft.body["mortgages"][0]["monthly_payment"] == "1200.00"
    assert draft.body["mortgages"][0]["statement_file_id"] == statement
    assert draft.body["mortgages"][1]["balance"] == "50000.00"
    assert draft.body["field_sources"][chosen]["file_id"] == str(file.id)
    assert family.current_version_id is None and len(db.versions) == 1


@pytest.mark.asyncio
async def test_applied_source_remap_keeps_established_ids_after_manual_address_correction():
    body = real_estate_schema.validate(schedule())
    db, profile, family, draft, file, analysis = setup_source(body)
    mapped = ws._mapping_records("real_estate_schedule", body, file, analysis, file.content_hash)
    cached = FinancialFormImport(id=uuid4(), family_id=family.id, source_file_id=file.id, content_hash=file.content_hash, base_revision=1,
                                 proposed_body=copy.deepcopy(body), mappings=mapped, warnings=[], status="applied")
    db.add(cached)
    draft.body["properties"][0]["address"] = "100 Corrected Fictional Street"
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "_extract_full_form", AsyncMock()) as model:
        result = await ws.propose_import(db, profile, kind="real_estate_schedule", file_id=file.id, family_id=family.id, analysis=analysis)
    assert result["id"] != str(cached.id) and result["base_revision"] == 2
    assert result["proposed_body"]["properties"][0]["id"] == body["properties"][0]["id"]
    address = next(row for row in result["mappings"] if row["key"] == f"properties.{body['properties'][0]['id']}.address")
    assert address["before"] == "100 Corrected Fictional Street" and address["conflict"] is True
    model.assert_not_awaited()


@pytest.mark.asyncio
async def test_cached_remap_updates_property_fk_values_when_rows_are_reconciled():
    body = real_estate_schema.validate(schedule())
    db, profile, family, draft, file, analysis = setup_source(body)
    proposed = ws._sanitize_extracted_body("real_estate_schedule", body)
    mappings = ws._mapping_records("real_estate_schedule", proposed, file, analysis, file.content_hash)
    cached = FinancialFormImport(id=uuid4(), family_id=family.id, source_file_id=file.id, content_hash=file.content_hash, base_revision=1,
                                 proposed_body=proposed, mappings=mappings, warnings=[], status="applied")
    db.add(cached)
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))):
        result = await ws.propose_import(db, profile, kind="real_estate_schedule", file_id=file.id, family_id=family.id, analysis=analysis)
    loan = body["mortgages"][0]
    parent = next(row for row in result["mappings"] if row["key"] == f"mortgages.{loan['id']}.property_row_id")
    assert parent["value"] == loan["property_row_id"]


@pytest.mark.asyncio
async def test_source_bytes_changed_after_mapping_cannot_be_applied_even_with_old_metadata_hash():
    db, profile, family, draft, file, analysis = setup_source(schedule())
    imported = FinancialFormImport(id=uuid4(), family_id=family.id, source_file_id=file.id, content_hash=file.content_hash, base_revision=2,
                                  proposed_body=schedule(), mappings=[{"key": "owner_name", "value": "Changed", "source": {}}], warnings=[], status="proposed")
    db.add(imported)
    before = copy.deepcopy(draft.body)
    with (patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "_read_file", return_value=b"changed source")):
        with pytest.raises(HTTPException) as error:
            await ws.apply_import(db, profile, imported.id, expected_revision=2)
    assert error.value.status_code == 409
    assert draft.body == before and draft.revision == 2 and imported.status == "proposed"


@pytest.mark.asyncio
async def test_mortgage_statement_never_becomes_original_schedule():
    db, profile, family, draft, file, analysis = setup_source(classification="payoff_or_mortgage_statement")
    with patch.object(ws, "_extract_full_form", AsyncMock()) as model:
        with pytest.raises(HTTPException) as error:
            await ws.propose_import(db, profile, kind="real_estate_schedule", file_id=file.id, family_id=family.id, analysis=analysis)
    assert error.value.detail["code"] == "real_estate_mortgage_statement"
    assert not db.versions and not db.imports
    model.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("role,document_type,accepted", [(None, "mortgage_statement", False), ("mortgage_statement", "mortgage_statement", True),
                                                        ("mortgage_statement", "payoff_letter", False), ("mortgage_statement", None, False)])
async def test_schema_extractor_keeps_schedule_and_periodic_mortgage_roles_separate(role, document_type, accepted):
    response = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps({"body": schedule(), "document_type": document_type}))])
    with (patch.object(ws, "_extract_acroform", return_value=({**real_estate_schema.empty_body(), "no_real_estate": None}, {})),
          patch.object(bucket_ai, "_append_review_file_content", return_value=(True, 2, 0)),
          patch("app.services.ai.bedrock_client.get_client", return_value=object()),
          patch("app.services.ai.bedrock_client.model_heavy", return_value="test-model"),
          patch("app.services.ai.usage.tracked_messages_create", AsyncMock(return_value=response)) as model):
        if accepted:
            body, _, _, _ = await ws._extract_full_form(AsyncMock(), "real_estate_schedule", SimpleNamespace(id=uuid4()), b"%PDF fictional", document_role=role)
            assert body["mortgages"]
            prompt = model.call_args.kwargs["messages"][0]["content"][0]["text"]
            assert "targeted mortgage statement review" in prompt and "This import is a real estate schedule" not in prompt
        else:
            with pytest.raises(HTTPException):
                await ws._extract_full_form(AsyncMock(), "real_estate_schedule", SimpleNamespace(id=uuid4()), b"%PDF fictional", document_role=role)


@pytest.mark.asyncio
async def test_extracted_owner_period_mismatch_is_rejected_before_original_or_draft_change():
    db, profile, family, draft, file, analysis = setup_source()
    incoming = schedule()
    incoming["as_of"] = "2025-10-31"
    with (patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))),
          patch.object(ws, "_read_file", return_value=b"%PDF fictional schedule"),
          patch.object(ws, "_extract_full_form", AsyncMock(return_value=(incoming, {}, [], {})))):
        with pytest.raises(HTTPException) as error:
            await ws.propose_import(db, profile, kind="real_estate_schedule", file_id=file.id, family_id=family.id, analysis=analysis)
    assert error.value.detail["code"] == "financial_family_mismatch"
    assert not db.imports and not db.versions and draft.revision == 2


@pytest.mark.asyncio
async def test_unknown_original_family_identity_does_not_merge_a_different_owner_into_known_draft():
    db, profile, family, draft, file, analysis = setup_source(schedule())
    family.identity = {"subject": "business", "source_file_id": str(uuid4())}
    incoming = schedule()
    incoming["owner_name"] = "Another Owner"
    with (patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))),
          patch.object(ws, "_read_file", return_value=b"%PDF fictional schedule"),
          patch.object(ws, "_extract_full_form", AsyncMock(return_value=(incoming, {}, [], {})))):
        with pytest.raises(HTTPException) as error:
            await ws.propose_import(db, profile, kind="real_estate_schedule", file_id=file.id, family_id=family.id, analysis=analysis)
    assert error.value.detail["code"] == "financial_family_mismatch"
    assert not db.versions and draft.revision == 2


@pytest.mark.parametrize("selected", ["property_value", "loan_balance", "loan_without_property", "loan_with_unselected_new_property"])
def test_selected_import_requires_named_property_and_loan_link(selected):
    source = schedule()
    prop, loan = source["properties"][0]["id"], source["mortgages"][0]["id"]
    choices = {"property_value": {f"properties.{prop}.estimated_value": "500000"},
               "loan_balance": {f"mortgages.{loan}.balance": "200000"},
               "loan_without_property": {f"mortgages.{loan}.lender": "Example Bank", f"mortgages.{loan}.balance": "200000"},
               "loan_with_unselected_new_property": {f"mortgages.{loan}.lender": "Example Bank", f"mortgages.{loan}.property_row_id": prop}}
    with pytest.raises(HTTPException) as error:
        real_estate_imports.validate_selection(real_estate_schema.empty_body(), choices[selected])
    assert error.value.status_code == 422


def test_new_linked_loan_can_target_existing_property_without_reselecting_address():
    body = schedule()
    prop, loan = body["properties"][0]["id"], str(uuid4())
    mappings = {f"mortgages.{loan}.lender": "New Example Bank", f"mortgages.{loan}.property_row_id": prop, f"mortgages.{loan}.balance": "123"}
    real_estate_imports.validate_selection(body, mappings)


@pytest.mark.asyncio
async def test_named_xlsx_schedule_is_reviewed_without_model_and_original_has_no_fake_pdf():
    source = schedule()
    raw = financial_templates_xlsx.build_real_estate_workbook(source)
    db, profile, family, draft, file, analysis = setup_source(raw=raw, name="Real estate schedule.xlsx")
    with (patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))),
          patch.object(ws, "_read_file", return_value=raw),
          patch.object(ws, "_extract_full_form", AsyncMock()) as model,
          patch.object(ws, "load_workspace", AsyncMock(return_value={}))):
        result = await ws.propose_import(db, profile, kind="real_estate_schedule", file_id=file.id, family_id=family.id, analysis=analysis)
        assert draft.body["properties"] == [] and family.current_version_id is None
        await ws.apply_import(db, profile, db.imports[-1].id, expected_revision=2)
    model.assert_not_awaited()
    assert len(draft.body["properties"]) == 2 and len(draft.body["mortgages"]) == 3
    assert len(db.versions) == 1 and db.versions[0].source_file_id == file.id and db.versions[0].pdf_file_id is None
    assert result["warnings"] and not any(row["key"].endswith("estimated_equity") for row in result["mappings"])
    assert family.current_version_id is None


def test_unrelated_xlsx_does_not_guess_columns_or_create_zero_schedule():
    workbook = Workbook()
    workbook.active.append(["Property", "Value"])
    workbook.active.append(["Example", 500000])
    raw = BytesIO()
    workbook.save(raw)
    with pytest.raises(HTTPException) as error:
        real_estate_imports.read_workbook(raw.getvalue())
    assert error.value.status_code == 422 and "template" in error.value.detail


@pytest.mark.asyncio
async def test_existing_arbitrary_xlsx_uses_bounded_schema_mapping_cell_sources_and_unknown_formulas():
    workbook = Workbook()
    properties = workbook.active
    properties.title = "Properties"
    properties.append(["Address", "Market value"])
    properties.append(["100 Fictional Street", "=100+200"])
    properties.append(["200 Fictional Street", 350000])
    for _ in range(502):
        properties.append(["More listed source rows"])
    liens = workbook.create_sheet("Liens")
    liens.append(["Lender", "Balance", "Loan reference"])
    liens.append(["Example Bank", 200000, "First-1111"])
    output = BytesIO()
    workbook.save(output)
    raw = output.getvalue()
    source = schedule()
    # The model must not supply a calculation for an uncached formula.
    refs = {"properties.r1.estimated_value": {"sheet": "Properties", "cell": "B2", "quote": "=100+200"},
            "properties.r2.estimated_value": {"sheet": "Properties", "cell": "B3", "quote": "350000"},
            "mortgages.r1.balance": {"sheet": "Liens", "cell": "B2", "quote": "200000"}}
    response = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps({"body": source, "references": refs}))])
    file = SimpleNamespace(id=uuid4(), file_name="Existing property portfolio.xlsx", content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert real_estate_imports.read_workbook(raw, allow_unmapped=True) is None
    with (patch.object(ws, "_extract_acroform") as acro,
          patch("app.services.ai.bedrock_client.get_client", return_value=object()),
          patch("app.services.ai.bedrock_client.model_heavy", return_value="test-model"),
          patch("app.services.ai.usage.tracked_messages_create", AsyncMock(return_value=response)) as model):
        body, references, warnings, anchors = await ws._extract_full_form(AsyncMock(), "real_estate_schedule", file, raw)
    acro.assert_not_called()
    assert body["properties"][0]["estimated_value"] is None
    assert body["properties"][1]["estimated_value"] == "350000.00"
    assert any("cached" in warning for warning in warnings) and any("500" in warning for warning in warnings)
    records = ws._mapping_records("real_estate_schedule", body, file, None, hashlib.sha256(raw).hexdigest(), references, anchors)
    balance = next(row for row in records if row["key"] == f"mortgages.{body['mortgages'][0]['id']}.balance")
    assert balance["source"]["sheet"] == "Liens" and balance["source"]["cell"] == "B2" and balance["confidence"] == "high"
    assert "page" not in balance["source"]
    content = model.call_args.kwargs["messages"][0]["content"]
    assert "Never execute or calculate formulas" in content[0]["text"]
    assert any("Extracted workbook/table data" in item.get("text", "") for item in content)


@pytest.mark.asyncio
async def test_existing_arbitrary_xlsx_uses_same_review_endpoint_and_preserves_original():
    workbook = Workbook()
    workbook.active.append(["Address", "Lender", "Balance"])
    workbook.active.append(["100 Fictional Street", "Example Bank", 200000])
    output = BytesIO()
    workbook.save(output)
    raw = output.getvalue()
    db, profile, family, draft, file, analysis = setup_source(raw=raw, name="Existing portfolio.xlsx")
    extracted = ws._sanitize_extracted_body("real_estate_schedule", schedule())
    with (patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "_read_file", return_value=raw),
          patch.object(ws, "_extract_full_form", AsyncMock(return_value=(extracted, {}, [], {}))) as model):
        result = await ws.propose_import(db, profile, kind="real_estate_schedule", file_id=file.id, family_id=family.id, analysis=analysis)
    model.assert_awaited_once()
    assert result["status"] == "proposed" and result["proposed_body"]["properties"]
    assert not draft.body["properties"] and family.current_version_id is None
    assert len(db.versions) == 1 and db.versions[0].source_file_id == file.id and db.versions[0].pdf_file_id is None


@pytest.mark.parametrize("name", ["Real Estate Schedule.pdf", "Schedule of Real Estate Owned.pdf", "REO Schedule.xlsx"])
def test_high_signal_schedule_upload_recognition_is_distinct_from_statement(name):
    assert filename_evidence_classification(name) == "real_estate_schedule"
    assert classifications_for_requested_doc("Real estate schedule", None) == {"real_estate_schedule"}
    assert filename_evidence_classification("Mortgage Statement.pdf") == "payoff_or_mortgage_statement"
    assert filename_evidence_classification("Statement.pdf") == "bank_statement"
