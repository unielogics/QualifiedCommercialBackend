"""The operating plan shares version, scope and collection boundaries."""
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.models.application_profile import ApplicationRequirementState
from app.models.bucket import BucketRequestedDocument
from app.routers import application_profiles
from app.schemas.application_profile import FinancialStatementWrite
from app.schemas.file_request import FileRequestItem
from app.services import (
    application_programs,
    chat_form_links,
    client_financial_forms,
    file_requests,
    financial_statements,
    financial_workspace,
    operating_plan_forms,
    operating_plan_pdf,
    operating_plan_schema,
    requirement_library,
    sheet_layout,
    sheets,
)
from app.tests.test_real_estate_request_adoption import MemoryDB

KIND = "operating_plan"
KEY = "operating_expenses_and_earnings_plan"


def example():
    return operating_plan_schema.validate({**operating_plan_schema.empty_body(),
        "business_name": "Example Business LLC", "period_start": "2025-01-01", "period_end": "2025-12-31", "basis": "cash",
        "expenses": [{"id": str(uuid4()), "category": "rent", "label": "Office rent", "current_amount": 1200}],
        "adjustments": [{"id": str(uuid4()), "kind": "expense_reduction", "label": "Negotiate office rent", "current_amount": 1200,
            "proposed_amount": 1000, "driver_key": "office-rent", "effective_date": "2026-12-01", "explanation": "Renew the lease at the quoted rate.",
            "source_note": "Landlord's renewal quote", "implementation_cost": 0}]})


def test_disclosure_proposal_is_never_a_parallel_historical_income_source():
    body = example()
    facts = financial_workspace.facts_for_body(KIND, body)
    assert not {"ebitda", "net_income", "operating_income", "gross_revenue", "total_monthly_debt_service"}.intersection(facts)
    assert facts["operating_plan"]["business_name"] == body["business_name"]
    assert not operating_plan_schema.STAFF_FIELDS.intersection(facts["operating_plan"]["adjustments"][0])
    assert operating_plan_schema.completion(body)["complete"]
    assert KIND not in application_profiles.PACKET_KINDS
    assert KIND not in chat_form_links.target_for("open_worksheet", None)[1]
    assert chat_form_links.target_for("complete_now", KEY) == (KIND, ())
    assert KIND not in sheets._scoped(None)


@pytest.mark.asyncio
async def test_plan_projection_cannot_write_book_statement_or_debt_tables():
    db = SimpleNamespace(add=AsyncMock(), execute=AsyncMock())
    await financial_workspace._project(db, SimpleNamespace(), SimpleNamespace(kind=KIND), example(), {}, actor=None, submit=True, pdf_file_id=uuid4())
    db.add.assert_not_called()
    db.execute.assert_not_awaited()


def test_client_cannot_forge_or_read_staff_review_or_reconciliation_fields():
    body = example()
    row = body["adjustments"][0]
    reviewer = str(uuid4())
    row.update(status="approved", reviewed_by_user_id=reviewer, reviewed_at=datetime.now(UTC).isoformat(),
        review_reason="Staff-only explanation", review_source_stamp="f" * 64, baseline_treatment="included_in_baseline", baseline_reconciliation_note="Staff-only reconciliation")
    incoming = deepcopy(body)
    incoming["adjustments"][0].update(proposed_amount=900, reviewed_by_user_id=str(uuid4()), review_reason="forged", baseline_treatment="incremental_to_baseline")
    merged = client_financial_forms.merge_client_body(KIND, body, incoming)
    assert merged["adjustments"][0]["proposed_amount"] == 900
    assert merged["adjustments"][0]["reviewed_by_user_id"] == reviewer
    assert merged["adjustments"][0]["review_reason"] == "Staff-only explanation"
    visible = client_financial_forms.client_body(KIND, merged)
    assert not (operating_plan_schema.STAFF_FIELDS - {"status"}).intersection(visible["adjustments"][0])
    computed = sheets._computed(KIND, merged)
    assert computed == {"disclosed_expense_rows": 1, "adjustment_rows": 1}
    assert "Staff-only" not in str(computed)
    shape = sheet_layout.layout_for_body(KIND, body)
    assert not any(any(field in key for field in operating_plan_schema.STAFF_FIELDS) for key in shape.by_key)


def test_unit_cost_drivers_survive_client_edit_worksheet_and_dated_pdf_body():
    body = example()
    expense = body["expenses"][0]
    expense.update(cost_basis="per_unit", unit_label="staff seat", current_units_per_period=10,
        current_unit_cost=100, current_fixed_amount=200)
    action = body["adjustments"][0]
    action.update(cost_basis="per_unit", unit_label="staff seat", expense_row_id=expense["id"],
        current_units_per_period=10, proposed_units_per_period=8, current_unit_cost=100,
        proposed_unit_cost=90, current_fixed_amount=200, proposed_fixed_amount=200,
        current_amount=None, proposed_amount=None)
    merged = client_financial_forms.merge_client_body(KIND, body, deepcopy(body))
    assert merged["expenses"][0]["current_units_per_period"] == 10
    assert merged["adjustments"][0]["proposed_units_per_period"] == 8
    shape = sheet_layout.layout_for_body(KIND, merged)
    _, quantity = shape.by_key[f"expenses.{expense['id']}.current_units_per_period"]
    assert quantity.type == "number"
    assert shape.by_key[f"expenses.{expense['id']}.cost_basis"][1].options == ("total", "per_unit")
    values = sheet_layout.flatten(KIND, merged)
    assert values[quantity.key] == "10"
    html = operating_plan_pdf.build_html(body=merged)
    assert "Quantity: 10" in html and "staff seat" in html
    assert "$1,200.00" in html and "$920.00" in html
    assert "proposed units per period: 8" in html


def test_request_catalog_and_batch_use_one_reviewed_online_form():
    definition = requirement_library.baseline_definitions()[KEY]
    assert definition["requirement_kind"] == "form"
    assert definition["verification_required"]
    assert requirement_library.form_kinds_for(KEY) == (KIND,)
    item = FileRequestItem(document_type=KIND, name=operating_plan_forms.LABEL)
    config = file_requests.item_config(item, uuid4())
    assert config["form_kind"] == KIND and config["evidence_classes"] == [KIND]
    assert config["quantity"] == 1 and config["unit"] == "files"


@pytest.mark.asyncio
async def test_existing_batch_form_request_is_reused_without_second_requirement_or_destination():
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), extra_requirements=[])
    slot = BucketRequestedDocument(id=uuid4(), bucket_id=profile.primary_bucket_id, name=operating_plan_forms.LABEL,
        category="Financials", status="requested", requirement_key="staff_request_batch_0",
        requirement_source={"collection_config": {"form_kind": KIND}})
    state = ApplicationRequirementState(id=uuid4(), profile_id=profile.id, requested_document_id=slot.id,
        requirement_key=slot.requirement_key, label=slot.name, category="financials", required_level="required", status="requested",
        provenance={"requirement_kind": "form", "collection_config": {"form_kind": KIND}})
    db = MemoryDB([slot, state])
    with patch.object(financial_workspace, "lock_financial_profile", AsyncMock()), patch.object(requirement_library, "plan_add", AsyncMock()) as add, patch.object(application_programs, "get_program_readiness", AsyncMock()) as refresh:
        result = await operating_plan_forms.ensure_request(db, profile, actor=SimpleNamespace(id=uuid4()))
    assert result == (slot, True)
    add.assert_not_awaited()
    refresh.assert_not_awaited()
    assert len(db.rows) == 2


@pytest.mark.asyncio
async def test_public_form_requires_bound_family_and_view_links_cannot_submit():
    held = SimpleNamespace(kind=KIND, financial_family_id=None)
    with patch.object(financial_statements, "link_for_token", AsyncMock(return_value=held)):
        with pytest.raises(HTTPException) as error:
            await application_profiles._open_link_or_404(None, "credential")
    assert error.value.status_code == 404
    held.financial_family_id, held.permission = uuid4(), "view"
    db = SimpleNamespace(commit=AsyncMock())
    with patch.object(application_profiles, "_open_link_or_404", AsyncMock(return_value=held)):
        with pytest.raises(HTTPException) as error:
            await application_profiles.submit_public_financial_form("credential", FinancialStatementWrite(body=example()), db)
    assert error.value.status_code == 403
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_staff_share_and_client_submission_keep_the_exact_family_and_revision():
    family_id = uuid4()
    held = SimpleNamespace(kind=KIND, id=uuid4(), profile_id=uuid4(), statement_id=None, financial_family_id=family_id,
        permission="edit", completed_at=None, invitee_email="owner@example.com", expires_at=None)
    profile = SimpleNamespace(id=held.profile_id)
    user = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    snapshot = {"kind": KIND, "family_id": str(family_id), "body": example(), "draft": {"revision": 4, "base_version_id": None}, "versions": []}
    db = SimpleNamespace(get=AsyncMock(return_value=profile), commit=AsyncMock())
    with patch.object(application_profiles.profiles, "load_profile", AsyncMock(return_value=profile)), patch.object(operating_plan_forms, "ensure_workspace", AsyncMock(return_value=snapshot)), patch.object(application_profiles, "managed_snapshot", AsyncMock(return_value=snapshot)), patch.object(financial_statements, "mint_link", AsyncMock(return_value=(held, "credential"))), patch.object(application_profiles.profiles, "log_profile_action", AsyncMock()):
        result = await application_profiles.mint_financial_form_link(profile.id, KIND, user, db)
    assert f"/forms/{KIND}/credential" in result["url"]
    assert held.financial_family_id == family_id
    payload = FinancialStatementWrite(body=example(), expected_revision=4, idempotency_key="save-operating-plan-1")
    with patch.object(application_profiles, "_open_link_or_404", AsyncMock(return_value=held)), patch.object(application_profiles, "managed_snapshot", AsyncMock(return_value=snapshot)), patch.object(client_financial_forms, "save_submission", AsyncMock(return_value=snapshot)) as publish:
        result = await application_profiles.submit_public_financial_form("credential", payload, db)
    assert result["completed"]
    assert publish.await_args.args[2]["family_id"] == str(family_id)
    assert publish.await_args.kwargs["expected_revision"] == 4
    assert publish.await_args.kwargs["actor"].id is None


def test_generated_pdf_labels_conditional_proposals_and_escapes_input():
    body = example()
    body["business_name"] = "Example <script>alert('x')</script>"
    output = operating_plan_pdf.build_html(body=body)
    assert "<script>" not in output and "&lt;script&gt;" in output
    assert "Client submissions await staff review" in output
    assert "Negotiate office rent" in output and "1,200.00" in output
    assert "historical P&amp;L, NOI, EBITDA" in output


def test_client_removal_keeps_rows_recoverable_before_publication():
    body = example()
    result = client_financial_forms.merge_client_body(KIND, body, {"expenses": [], "adjustments": []})
    assert result["expenses"][0]["id"] == body["expenses"][0]["id"]
    assert result["expenses"][0]["excluded"] is True
    assert result["adjustments"][0]["id"] == body["adjustments"][0]["id"]
    assert result["adjustments"][0]["excluded"] is True
    shape = sheets._layout_for(KIND, result, exact="expenses")
    assert not [row for row in shape.rows if row.block == "expenses" and row.kind == "data"]


def test_pdf_before_after_uses_saved_server_evaluation_and_omits_private_review_data():
    import unicodedata

    import fitz

    body = example()
    row = body["adjustments"][0]
    file_id = str(uuid4())
    row.update(source_file_ids=[file_id], review_reason="private-review-phrase", baseline_reconciliation_note="private-baseline-phrase")
    financials = {"evidence_sources": [{"file_id": file_id, "file_name": "Landlord renewal quote.pdf"}],
        "improvement_plan": {"baseline": {"earnings": -10000, "noi": -12000, "dscr": -0.2, "period": "2025", "currency": "USD", "basis": "cash"},
            "projection": {"proposed": {"earnings": -7600, "noi": -9600, "dscr_current": -0.15}, "approved": {"earnings": -10000, "noi": -12000, "dscr_current": -0.2}},
            "totals": {"proposed": {"annual_impact": 2400, "monthly_impact": 200, "first_year_impact": 200, "one_time_cost": 0}, "approved": {"annual_impact": 0, "monthly_impact": 0, "first_year_impact": 0, "one_time_cost": 0}},
            "adjustments": [{"id": row["id"], "review_state": "proposed", "net_annual_impact": 2400, "first_year_impact": 200}],
            "missing": ["Confirm the new lease before relying on the saving."], "risk_flags": ["Earnings remain negative."]}}
    document = operating_plan_pdf.build_html(body=body, financial_totals=financials)
    assert "private-review-phrase" not in document and "private-baseline-phrase" not in document
    assert "Landlord renewal quote.pdf" in document and file_id not in document
    assert "-$10,000.00" in document and "-$7,600.00" in document and "$2,400.00" in document
    raw = operating_plan_pdf._with_footer(operating_plan_pdf._render_pymupdf(document))
    pdf = fitz.open(stream=raw, filetype="pdf")
    text = unicodedata.normalize("NFKC", " ".join(page.get_text() for page in pdf))
    assert "-$7,600.00" in text and "Landlord renewal quote.pdf" in text
    assert "Page 1 of" in text and "Confirm the new lease" in text


@pytest.mark.asyncio
async def test_email_batch_adopts_existing_library_form_request_and_keeps_different_period_separate():
    from app.services import collection_tasks
    from app.tests.test_file_requests import MemoryDB as BatchMemoryDB
    from app.tests.test_file_requests import profile as batch_profile

    owner, db = batch_profile(), BatchMemoryDB()
    slot = BucketRequestedDocument(id=uuid4(), bucket_id=owner.primary_bucket_id, name=operating_plan_forms.LABEL,
        category="Financials", status="requested", requirement_key=KEY,
        requirement_source={"collection_config": {"form_kind": KIND}})
    state = ApplicationRequirementState(id=uuid4(), profile_id=owner.id, requested_document_id=slot.id,
        requirement_key=KEY, label=slot.name, category="financials", required_level="required", status="requested", verification_required=True,
        provenance={"requirement_kind": "form", "collection_config": {"form_kind": KIND}})
    db.rows.extend([slot, state])
    await db.flush()
    item = FileRequestItem(document_type=KIND, name=slot.name)
    request_id = uuid4()
    with patch.object(file_requests, "lock_profile", AsyncMock()), patch("app.services.arf_channel.profile_uses_arf", AsyncMock(return_value=False)), patch.object(collection_tasks, "sync_collection_tasks", AsyncMock(return_value=[])), patch.object(file_requests.profiles, "log_profile_action", AsyncMock()):
        first = await file_requests.create_batch(db, profile=owner, request_id=request_id, items=[item])
        repeated = await file_requests.create_batch(db, profile=owner, request_id=request_id, items=[item])
        assert first == repeated == [state]
        assert state.verification_required and state.provenance["requirement_kind"] == "form"
        assert len([row for row in db.rows if isinstance(row, BucketRequestedDocument)]) == 1
        different = await file_requests.create_batch(db, profile=owner, request_id=uuid4(), items=[item.model_copy(update={"period": "2026"})])
    assert different[0].id != state.id
    assert len([row for row in db.rows if isinstance(row, BucketRequestedDocument)]) == 2


def long_note_example():
    body = {**operating_plan_schema.empty_body(), "business_name": "Long Notes QA LLC", "period_start": "2025-01-01", "period_end": "2025-12-31", "basis": "cash"}
    markers = []
    for index in range(35):
        marker = f"ExpenseEnd{index:03d}"
        markers.append(marker)
        note = ("Reviewed expense detail. " * 220)[:3950] + marker if index < 2 else "Reviewed expense detail. " + marker
        body["expenses"].append({"id": str(uuid4()), "category": "rent", "label": f"Expense {index + 1}", "current_amount": 1200, "notes": note})
    for index in range(6):
        end, source = f"ExplanationEnd{index:03d}", f"SourceNoteEnd{index:03d}"
        markers += [end, source]
        body["adjustments"].append({"id": str(uuid4()), "label": f"Improvement {index + 1}", "current_amount": 1200, "proposed_amount": 1000,
            "explanation": ("Reviewed change description. " * 180)[:3950] + end, "source_note": ("Supporting evidence description. " * 160)[:3950] + source})
    body["notes"] = ("Plan notes. " * 900)[:9900] + "FinalPlanNotesSentinel"
    markers.append("FinalPlanNotesSentinel")
    return operating_plan_schema.validate(body), markers


def test_paginated_pdf_preserves_long_notes_explanations_and_all_rows():
    import re
    import unicodedata

    import fitz

    body, markers = long_note_example()
    raw = operating_plan_pdf._render_pymupdf(operating_plan_pdf.build_html(body=body))
    pdf = fitz.open(stream=raw, filetype="pdf")
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", " ".join(page.get_text() for page in pdf)))
    assert len(pdf) > 3
    assert all(marker in text for marker in markers)
    expected = [body["notes"], *(row["notes"] for row in body["expenses"]), *(row[field] for row in body["adjustments"] for field in ("explanation", "source_note"))]
    assert all(re.sub(r"\s+", "", value) in text for value in expected)
