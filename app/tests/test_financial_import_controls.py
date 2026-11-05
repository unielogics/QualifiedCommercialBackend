import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from app.services import bucket_ai
from app.services import financial_workspace as ws


@pytest.mark.asyncio
async def test_full_pdf_model_cannot_dismiss_exclude_or_link_an_imported_debt():
    hostile_id, unrelated_file = uuid4(), uuid4()
    candidate = {
        "business_name": "Client business", "current_version_id": str(uuid4()),
        "field_sources": {"balance": {"file_id": str(unrelated_file)}},
        "reconciliation_history": [{"op": "duplicate"}],
        "debts": [{
            "id": str(hostile_id), "lender": "Oakwood", "balance": "100", "monthly_payment": "10",
            "origin": "admin", "status": "dismissed", "duplicate_of_id": str(uuid4()),
            "count_in_dscr": False, "vendor_key": "UNRELATED VENDOR", "payoff_amount": "1",
            "document_id": str(uuid4()), "evidence": {"source": "forged"},
            "evidence_links": [{"file_id": str(unrelated_file), "role": "payoff", "is_current": True}],
        }],
    }
    response = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps({"body": candidate}))])
    with patch.object(ws, "_extract_acroform", return_value=(ws.empty_body("debt_schedule"), {})), patch.object(
        bucket_ai, "_append_review_file_content", return_value=(True, 1, 0)
    ), patch("app.services.ai.bedrock_client.get_client", return_value=object()), patch(
        "app.services.ai.bedrock_client.model_heavy", return_value="test-model"
    ), patch("app.services.ai.usage.tracked_messages_create", AsyncMock(return_value=response)):
        body, _, _, _ = await ws._extract_full_form(AsyncMock(), "debt_schedule", SimpleNamespace(id=uuid4()), b"%PDF source")
    row = body["debts"][0]
    assert UUID(row["id"]) != hostile_id
    assert row["lender"] == "Oakwood" and row["balance"] == "100"
    assert row["origin"] == "ai_draft" and row["status"] == "active"
    assert not {"duplicate_of_id", "count_in_dscr", "vendor_key", "payoff_amount", "document_id", "evidence", "evidence_links"}.intersection(row)
    assert not {"current_version_id", "field_sources", "reconciliation_history"}.intersection(body)
    # The accepted source values still enter normal active financial facts.
    assert ws.facts_for_body("debt_schedule", body)["total_outstanding_balance"] == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("exact_monthly,expected", [(None, "100.00"), ("900", "900.00"), ("0", "0.00")])
async def test_exact_native_terms_keep_printed_monthly_and_warn_on_conflict(exact_monthly, expected):
    candidate = {"debts": [{"lender": "Kapfi", "payment_amount": "20", "payment_frequency": "daily", "monthly_payment": "100"}]}
    exact = {"payment_amount": "40", "payment_frequency": "daily"}
    if exact_monthly is not None:
        exact["monthly_payment"] = exact_monthly
    acro = {"debts": [exact]}
    response = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps({"body": candidate}))])
    with patch.object(ws, "_extract_acroform", return_value=(acro, {})), patch.object(
        bucket_ai, "_append_review_file_content", return_value=(True, 1, 0)
    ), patch("app.services.ai.bedrock_client.get_client", return_value=object()), patch(
        "app.services.ai.bedrock_client.model_heavy", return_value="test-model"
    ), patch("app.services.ai.usage.tracked_messages_create", AsyncMock(return_value=response)):
        body, _, warnings, _ = await ws._extract_full_form(AsyncMock(), "debt_schedule", SimpleNamespace(id=uuid4()), b"%PDF source")
    row = body["debts"][0]
    assert row["payment_amount"] == "40.00" and row["payment_frequency"] == "daily"
    assert row["monthly_payment"] == expected and row["monthly_payment_source"] == "stated"
    assert any("stated monthly payment" in warning and "differs" in warning for warning in warnings)


def test_pfs_sanitization_keeps_declared_property_status_and_drops_application_metadata():
    supplied_id = uuid4()
    body = ws._sanitize_extracted_body("pfs", {
        "applicant": {"name": "Jane", "ssn": "not-a-permitted-field", "owner_id": str(uuid4())},
        "assets": {"cash_on_hand": "100", "fake_asset": "999999"},
        "schedules": {"real_estate": [{"id": str(supplied_id), "property_address": "100 Main Street", "status": "Owned",
            "present_market_value": "300000", "count_in_dscr": False, "evidence_links": [{"file_id": str(uuid4())}]}],
            "unknown_schedule": [{"value": "untrusted"}]},
        "family_id": str(uuid4()), "owner_ids": [str(uuid4())],
    })
    property_row = body["schedules"]["real_estate"][0]
    assert property_row["status"] == "Owned" and UUID(property_row["id"]) != supplied_id
    assert body["applicant"]["name"] == "Jane" and body["assets"]["cash_on_hand"] == "100"
    assert not {"ssn", "owner_id"}.intersection(body["applicant"])
    assert "fake_asset" not in body["assets"] and "unknown_schedule" not in body["schedules"]
    assert not {"count_in_dscr", "evidence_links"}.intersection(property_row)
    assert not {"family_id", "owner_ids"}.intersection(body)


@pytest.mark.parametrize("kind", ["p_and_l", "balance_sheet"])
def test_statement_model_cannot_add_extra_fields_or_projection_controls(kind):
    clean = ws._sanitize_extracted_body(kind, {
        "header": {"business_name": "Client business", "statement_id": str(uuid4())},
        "sections": {"unknown_section": {"net_income": "999999"}},
        "status": "submitted", "bucket_file_id": str(uuid4()), "financial_version": {"current": True},
    })
    assert clean["header"]["business_name"] == "Client business"
    assert "statement_id" not in clean["header"] and "unknown_section" not in clean["sections"]
    assert not {"status", "bucket_file_id", "financial_version"}.intersection(clean)
