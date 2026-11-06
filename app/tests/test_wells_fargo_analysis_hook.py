from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import fitz
import pytest

from app.services import bucket_ai


def pdf_bytes(text=None):
    text = text or """Wells Fargo Bank, N.A.
Optimize Business Checking SM
Account number: 100000004321
September 1, 2026 - September 30, 2026 Page 1 of 1
FICTIONAL EXAMPLE LLC
Beginning balance Total credits Total debits Ending balance
$100.00
$500.00
-$300.00
$300.00
Credits
09/09
700.00 Online Transfer From Fictional Business Checking
xxxxxx8765 Ref #IbExample123 on09/09/26
"""
    document = fitz.open()
    page = document.new_page()
    page.insert_text((30, 30), text, fontsize=8)
    raw = document.tobytes()
    document.close()
    return raw


def objects(raw, facts=None):
    fingerprint = hashlib.sha256(raw).hexdigest()
    file = SimpleNamespace(id=uuid4(), bucket_id=uuid4(), file_name="statement.pdf", content_type="application/pdf", content_hash=fingerprint, size_bytes=len(raw))
    row = SimpleNamespace(id=uuid4(), bucket_file_id=file.id, status="completed", classification="bank_statement", content_hash=fingerprint,
                          analysis_version=4, confidence="high", analysis={"key_facts": facts if facts is not None else {"account_last4": "4321"}})
    return file, row


def test_parser_enrichment_preserves_all_financial_and_citation_facts_and_is_idempotent():
    raw = pdf_bytes()
    facts = {"account_last4": "4321", "beginning_balance": 777, "ending_balance": 888, "total_deposits_and_credits": 999,
             "total_withdrawals_and_debits": 111, "recurring_debits": [{"payee": "Fictional", "total_amount": 55}],
             "months": [{"statement_period": "2026-09", "overdraft_items": [{"date": "2026-09-02", "amount": 15}]}], "transactions_complete": False}
    file, row = objects(raw, copy.deepcopy(facts))
    siblings = {"fact_citations": {"ending_balance": [{"page": 1, "value": 888}]}, "profile_facts": {"entity_type": {"value": "LLC"}},
                "financial_fact_citations": {"source_hash": file.content_hash}, "profitability_diagnostics": [], "read_warnings": ["Existing warning"]}
    row.analysis.update(copy.deepcopy(siblings))
    bucket_ai._apply_verified_bank_movements(row, file=file, raw=raw, content_type="application/pdf")
    assert {key: row.analysis["key_facts"][key] for key in facts} == facts
    assert {key: row.analysis[key] for key in siblings} == siblings
    assert row.analysis["key_facts"]["currency"] == "USD"
    assert row.analysis["key_facts"]["currency_basis"] == "inferred_us_statement_context"
    assert row.analysis["key_facts"]["transfer_lines"][0]["amount"] == 700
    assert "source_row" not in row.analysis["key_facts"]["transfer_lines"][0]
    assert row.analysis["key_facts"]["transfer_lines"][0]["source_text_line"] > 0
    assert row.analysis["bank_movement_extraction"]["status"] == "applied"
    assert row.analysis["bank_movement_extraction"]["source_hash"] == file.content_hash
    assert row.analysis["bank_movement_extraction"]["summary_audit"][0]["ending_balance"] == 300
    first = copy.deepcopy(row.analysis)
    bucket_ai._apply_verified_bank_movements(row, file=file, raw=raw, content_type="application/pdf")
    assert row.analysis == first


@pytest.mark.parametrize(("change", "reason"), [("account", "existing_account_identity_conflict"), ("currency", "existing_currency_conflict"), ("hash", "source_hash_mismatch")])
def test_conflicting_existing_identity_currency_or_source_hash_stays_unchanged_and_needs_review(change, reason):
    raw = pdf_bytes()
    file, row = objects(raw, {"account_last4": "9999" if change == "account" else "4321", "currency": "CAD" if change == "currency" else "USD", "ending_balance": 555})
    before = copy.deepcopy(row.analysis["key_facts"])
    if change == "hash":
        row.content_hash = "a" * 64
    bucket_ai._apply_verified_bank_movements(row, file=file, raw=raw, content_type="application/pdf")
    assert row.analysis["key_facts"] == before
    assert row.analysis["bank_movement_extraction"]["status"] == "needs_review"
    assert reason in row.analysis["bank_movement_extraction"]["warning_codes"]
    assert len(row.analysis["read_warnings"]) == 1


@pytest.mark.parametrize("kind", ["unrecognized", "partial", "error"])
def test_unsupported_or_failed_parser_cannot_reject_a_readable_bank_or_change_model_facts(kind):
    raw = pdf_bytes("A different bank's September statement.") if kind == "unrecognized" else pdf_bytes()
    if kind == "partial":
        raw = pdf_bytes("Wells Fargo Optimize Business Checking\nAccount number: 100000004321\nSeptember 1, 2026 - September 30, 2026\nCredits\n09/09\n700.00 Online Transfer From Fictional xxxx8765")
    file, row = objects(raw, {"account_last4": "4321", "ending_balance": 555})
    before = copy.deepcopy(row.analysis["key_facts"])
    if kind == "error":
        with patch("app.services.wells_fargo_bank_movements.parse_wells_fargo_pdf", side_effect=ValueError("untrusted private source detail")):
            bucket_ai._apply_verified_bank_movements(row, file=file, raw=raw, content_type="application/pdf")
    else:
        bucket_ai._apply_verified_bank_movements(row, file=file, raw=raw, content_type="application/pdf")
    assert row.status == "completed"
    assert row.analysis["key_facts"] == before
    assert row.analysis["bank_movement_extraction"]["status"] == {"partial": "needs_review"}.get(kind, kind)
    assert len(row.analysis["read_warnings"]) == (0 if kind == "unrecognized" else 1)
    assert "untrusted private source detail" not in str(row.analysis)


@pytest.mark.parametrize("warnings", [[], ["Existing independent reading warning"]])
def test_unrelated_bank_keeps_existing_transfer_facts_warnings_and_usability(warnings):
    raw = pdf_bytes("Another Bank\nSeptember 2026 business checking statement.")
    facts = {"account_last4": "4321", "ending_balance": 555,
             "transfer_lines": [{"date": "2026-09-09", "amount": 25, "direction": "in", "page": 1}]}
    file, row = objects(raw, copy.deepcopy(facts))
    row.analysis["read_warnings"] = list(warnings)
    before_usability = bucket_ai.analysis_usability(row)
    bucket_ai._apply_verified_bank_movements(row, file=file, raw=raw, content_type="application/pdf")
    assert row.analysis["bank_movement_extraction"]["status"] == "unrecognized"
    assert row.analysis["key_facts"] == facts
    assert row.analysis["read_warnings"] == warnings
    assert bucket_ai.analysis_usability(row) == before_usability


def test_nonbank_and_nonpdf_documents_do_not_run_the_parser():
    raw = pdf_bytes()
    file, row = objects(raw)
    with patch("app.services.wells_fargo_bank_movements.parse_wells_fargo_pdf") as parse:
        bucket_ai._apply_verified_bank_movements(row, file=file, raw=raw, content_type="text/plain")
        row.classification = "debt_schedule"
        bucket_ai._apply_verified_bank_movements(row, file=file, raw=raw, content_type="application/pdf")
    parse.assert_not_called()
    assert "bank_movement_extraction" not in row.analysis


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [True, False])
async def test_cached_and_fresh_analysis_deliver_verified_rows_to_consumers_without_extra_model_calls(cached):
    raw = pdf_bytes()
    file, row = objects(raw)
    response = SimpleNamespace(stop_reason="end_turn", model="test-model", usage=None,
        content=[SimpleNamespace(type="text", text=json.dumps({"classification": "bank_statement", "confidence": "high", "summary": "Statement summary, no model transfer rows.", "key_facts": {"account_last4": "4321", "ending_balance": 555}}))])
    db = SimpleNamespace(flush=AsyncMock())

    async def consumer(db, *, file, analysis):
        assert analysis.analysis["bank_movement_extraction"]["status"] == "applied"
        assert analysis.analysis["key_facts"]["transfer_lines"][0]["transfer_reference"] == "IbExample123"

    with (
        patch.object(bucket_ai, "_lock_analysis_scope", AsyncMock()),
        patch.object(bucket_ai, "reconcile_uploaded_file", AsyncMock()),
        patch.object(bucket_ai, "_fetch_file", return_value=(raw, "application/pdf")) as fetch,
        patch.object(bucket_ai, "_cached_file_analysis", AsyncMock(return_value=row if cached else None)),
        patch.object(bucket_ai, "_get_or_create_analysis_row", AsyncMock(return_value=row)),
        patch.object(bucket_ai, "model_heavy", return_value="test-model"),
        patch.object(bucket_ai, "get_client", return_value=object()),
        patch.object(bucket_ai, "tracked_messages_create", AsyncMock(return_value=response)) as provider,
        patch.object(bucket_ai, "_reconcile_analysis_consumers", AsyncMock(side_effect=consumer)) as reconcile,
        patch.object(bucket_ai.merchant_processing, "is_offer_document", return_value=False),
    ):
        result = await bucket_ai.analyze_bucket_file(db, file, force=not cached)
    assert result.status == "completed"
    assert provider.await_count == (0 if cached else 1)
    fetch.assert_called_once()
    reconcile.assert_awaited_once()
    assert "transactions_complete" not in result.analysis["key_facts"]
    assert bucket_ai.CURRENT_FILE_ANALYSIS_VERSION == 4
