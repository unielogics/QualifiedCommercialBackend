"""Standard MCA AI policy reaches review, chat and individual source reads."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services import bucket_ai as ai
from app.services import mca_funding_policy as policy
from app.services import mca_funding_prompts as prompts


@pytest.mark.parametrize("builder", [ai.build_review_system, ai.build_chat_system, ai.build_file_analysis_system])
def test_standard_mca_selects_only_its_own_policy_with_no_generic_credit_stage(builder):
    result = builder("main_street_v1", funding_intent=policy.PROGRAM_KEY)
    assert prompts.PRODUCT_NOTES in result
    for legacy in (ai.MAIN_STREET_REVIEW_RULES, ai.MAIN_STREET_CHAT_RULES,
                   ai.MAIN_STREET_FILE_ANALYSIS_HINT, ai.MCA_REVIEW_RULES, ai.MCA_CHAT_RULES):
        assert legacy not in result
    assert "two EXISTING active MCAs BEFORE" in result
    assert "BEFORE interest, depreciation or amortization addbacks" in result
    assert "Unknown declarations/evidence remain pending review" in result
    assert "The soft credit pull is the last step" not in result
    assert "the soft credit pull is the last step" not in result


@pytest.mark.parametrize("builder", [ai.build_review_system, ai.build_chat_system, ai.build_file_analysis_system])
@pytest.mark.parametrize("variant", [None, "dealer_gatekeeper_v1", "real_estate_dscr_v1", "mca_refi_v1"])
def test_product_intent_cannot_move_an_unrelated_vertical_into_standard_mca(builder, variant):
    assert builder(variant, funding_intent=policy.PROGRAM_KEY) == builder(variant)


def test_standard_mca_admin_keeps_internal_context_without_borrower_interview():
    result = ai.build_chat_system("main_street_v1", funding_intent=policy.PROGRAM_KEY, audience="admin")
    assert prompts.PRODUCT_NOTES in result
    assert ai.ADMIN_THREAD_CHAT_RULES in result
    assert prompts.CHAT_RULES not in result
    assert "Baseline evidence: 6 months bank statements, 2 years" not in result
    spanish = ai.build_chat_system("main_street_v1", funding_intent=policy.PROGRAM_KEY,
                                   client_language="es", audience="uploader")
    assert "Respond ONLY in professional, natural Spanish" in spanish
    assert prompts.CHAT_RULES in spanish


@pytest.mark.parametrize("key", ["intent", "funding_intent", "funding_program_key"])
def test_legacy_mca_public_context_cannot_leak_inherited_checklist_or_staff_notes(key):
    result = ai._public_ai_context(SimpleNamespace(ai_context={
        "review_type": "main_street_v1", key: policy.PROGRAM_KEY, "industry": "other",
        "stage_1_required_items": ["two tax years", "P&L", "credit authorization"],
        "custom_instructions": "private staff-only text", "private_decision": "do not expose",
    }))
    assert result["funding_intent"] == policy.PROGRAM_KEY
    assert result["stage_1_required_items"] == [d["name"] for d in policy.intake_documents()]
    assert len(result["baseline_document_policy"]["allowed_document_categories"]) == 3
    assert result["baseline_document_policy"]["conditional_after_industry_known"] == []
    assert "private staff-only text" not in json.dumps(result)
    assert "private_decision" not in result


@pytest.mark.parametrize("key", ["intent", "funding_intent", "funding_program_key"])
@pytest.mark.parametrize("review", [None, SimpleNamespace(result={"executive_summary": "private internal decision"},
                                                         error="private provider error", status="failed")])
def test_mca_generic_room_summary_never_exposes_staff_context_or_internal_review(key, review):
    bucket = SimpleNamespace(ai_context={"review_type": "main_street_v1", key: policy.PROGRAM_KEY,
                                         "private_staff_notes": "not applicant context"})
    assert ai.upload_link_visible_summary(review, bucket) is None


def test_generic_room_summary_remains_available_for_other_main_street_products():
    bucket = SimpleNamespace(ai_context={"review_type": "main_street_v1", "intent": "term_loan_10_year"},
                             files=[], requested_documents=[])
    result = ai.upload_link_visible_summary(None, bucket)
    assert result is not None and result["review_status"] == "not_started"


@pytest.mark.asyncio
async def test_profile_file_read_uses_its_loaded_mca_bucket_identity_without_model_or_storage():
    raw = b"Tax year 2025. Ordinary business income (100)."
    file = SimpleNamespace(id=uuid4(), bucket_id=uuid4(), file_name="return.txt", content_type="text/plain",
                           content_hash=None, size_bytes=len(raw), bucket=SimpleNamespace(ai_context=policy.ai_context()))
    row = SimpleNamespace(id=uuid4(), bucket_file_id=file.id, status="completed", classification=None, analysis={})
    response = SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps({
        "classification": "tax_return", "confidence": "high", "summary": "Ordinary business loss.",
        "key_facts": {"tax_form": "1120S", "tax_year": "2025", "ordinary_business_income": -100},
    }))], stop_reason="end_turn", model="mock", usage=None)
    db = SimpleNamespace(flush=AsyncMock())
    with (
        patch.object(ai, "_lock_analysis_scope", AsyncMock()),
        patch.object(ai, "reconcile_uploaded_file", AsyncMock()),
        patch.object(ai, "_fetch_file", return_value=(raw, "text/plain")),
        patch.object(ai, "_get_or_create_analysis_row", AsyncMock(return_value=row)),
        patch.object(ai, "model_heavy", return_value="mock"),
        patch.object(ai, "get_client", return_value=object()),
        patch.object(ai, "tracked_messages_create", AsyncMock(return_value=response)) as provider,
        patch.object(ai, "_reconcile_analysis_consumers", AsyncMock()),
    ):
        result = await ai.analyze_bucket_file(db, file, force=True)
    assert prompts.FILE_ANALYSIS_HINT in provider.await_args.kwargs["system"]
    assert ai.MAIN_STREET_FILE_ANALYSIS_HINT not in provider.await_args.kwargs["system"]
    assert result.analysis["key_facts"]["ordinary_business_income"] == -100
    assert ai.CURRENT_FILE_ANALYSIS_VERSION == 4  # no unbounded re-read of unrelated files
