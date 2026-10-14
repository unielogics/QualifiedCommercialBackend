"""Content-based evidence remains reviewable regardless of uploaded names."""
import zipfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from openpyxl import Workbook

from app.services import application_programs as programs
from app.services.bucket_ai import (
    _append_review_file_content,
    _content_evidence_claims,
    _content_type_candidates,
    _heic_as_jpeg,
    detect_document_content_type,
)
from app.services.bucket_evidence import analysis_usability, reconcile_uploaded_file


def evidence(**changes):
    file = SimpleNamespace(id=uuid4(), file_name="scan 829.bin", bucket_id=uuid4(),
        content_hash="same", created_at=datetime.now(UTC), statement_period=None,
        requested_document_id=None, deleted_at=None, status="uploaded", source_detail=None)
    analysis = SimpleNamespace(status="completed", classification="current_p_and_l", confidence="high",
        content_hash="same", analysis={"key_facts": {}}, **changes)
    return file, analysis


def requirement(key="ytd_p_and_l_balance_sheet"):
    return SimpleNamespace(requirement_key=key, label="Year-to-date P&L and balance sheet",
        category="Financials", verification_required=False, completion_mode="ai_can_complete", review_checks=[])


def test_one_content_packet_satisfies_multiple_requirements_with_arbitrary_name():
    file, analysis = evidence(document_type_candidates=[
        {"classification": "current_p_and_l", "confidence": "high", "page_start": 1, "page_end": 2},
        {"classification": "balance_sheet", "confidence": "high", "page_start": 3, "page_end": 3},
    ])
    matched, complete, coverage = programs._matching_evidence_files(requirement(), None, [file], {file.id: analysis})
    assert matched == [file] and complete
    assert coverage["current"] == 2
    balance = requirement("balance_sheet")
    balance.label = "Balance sheet"
    assert programs._automatic_evidence_decision(requirement=balance, file=file, analysis=analysis,
        expected_entity=None, duplicate_content=False)[0] == "accepted"


def test_classifier_disagreement_keeps_targeted_upload_for_review():
    file, analysis = evidence()
    slot = SimpleNamespace(id=uuid4())
    file.requested_document_id = slot.id
    req = requirement("business_bank_statements_6_months")
    req.label = "Last 6 months business bank statements"
    matches, _, _ = programs._matching_evidence_files(req, slot, [file], {file.id: analysis})
    assert matches == [file]
    assert programs._automatic_evidence_decision(requirement=req, file=file, analysis=analysis,
        expected_entity=None, duplicate_content=False)[:2] == ("needs_more", "wrong_document")


def test_filename_cannot_establish_period_or_combined_financial_coverage():
    file, analysis = evidence()
    file.file_name = "profit-loss-balance-sheet_2025_2026-01.pdf"
    assert programs._coverage_for_files(requirement(), [file], {file.id: analysis})[0] is False
    assert programs._tax_years(file, analysis) == set()


@pytest.mark.asyncio
async def test_filename_alone_cannot_route_an_unassigned_upload():
    file, _ = evidence()
    file.file_name = "Bank Statement 2026-01.pdf"
    db = SimpleNamespace(get=AsyncMock(), execute=AsyncMock())
    assert await reconcile_uploaded_file(db, file) is None
    assert file.statement_period is None
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_automatic_links_are_retained_after_classification_changes():
    file, _ = evidence()
    link = SimpleNamespace(file_id=file.id, source="automatic", removed_at=None, removed_by_user_id=None)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [link]))),
        flush=AsyncMock(), add=Mock())
    assert await programs._sync_requirement_evidence(db, SimpleNamespace(id=uuid4()), {file.id: file}, {}) == [link]
    assert link.removed_at is None


def test_docx_paragraphs_and_table_text_are_read_from_bytes_despite_wrong_metadata():
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Acme LLC</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Revenue 100000</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>')
    content, skipped = [], []
    file, _ = evidence()
    file.file_name = "wrong.pdf"
    added, _, _ = _append_review_file_content(content=content, skipped=skipped, blocked_files=[], file=file,
        raw=buffer.getvalue(), content_type="application/pdf", attached_pdf_pages=0, spreadsheet_text_chars=0)
    assert added and not skipped
    assert "Acme LLC" in content[0]["text"] and "Revenue 100000" in content[0]["text"]


def test_workbook_bytes_override_misleading_image_name():
    workbook = Workbook()
    workbook.active.append(["Revenue", 100000])
    buffer = BytesIO()
    workbook.save(buffer)
    assert "spreadsheetml" in detect_document_content_type(buffer.getvalue(), "image/png")


def test_claim_ranges_are_bounded_and_capabilities_must_be_supported_types():
    parsed = {"classification": "balance_sheet", "confidence": "high", "evidence_claims": [
        {"capability": "balance_sheet", "page_start": -1, "page_end": 3},
        {"capability": "invented_type", "page_start": 1},
    ]}
    claims = _content_evidence_claims(parsed, _content_type_candidates(parsed))
    assert len(claims) == 1 and claims[0]["page_start"] is None and claims[0]["page_end"] == 3


def test_unavailable_analysis_is_reviewable_not_unsafe():
    _, analysis = evidence()
    analysis.status, analysis.skip_reason = "skipped", "unsupported_content_type"
    assert analysis_usability(analysis) == "needs_review"
    analysis.skip_reason = "password_protected"
    assert analysis_usability(analysis) == "needs_replacement"


def test_real_heic_image_converts_to_model_jpeg():
    from PIL import Image
    from pillow_heif import from_pillow

    encoded = BytesIO()
    from_pillow(Image.new("RGB", (64, 32), (20, 100, 200))).save(encoded)
    raw = encoded.getvalue()
    assert detect_document_content_type(raw, "application/octet-stream") == "image/heic"
    converted = _heic_as_jpeg(raw)
    assert detect_document_content_type(converted) == "image/jpeg"
    with Image.open(BytesIO(converted)) as image:
        assert image.size == (64, 32)


def test_shadow_mode_audits_legacy_rejection_without_denying_upload(monkeypatch):
    monkeypatch.setattr(programs, "get_settings", lambda: SimpleNamespace(evidence_decision_mode="shadow"))
    file, analysis = evidence()
    comparison = programs._evidence_shadow_comparison(requirement(), file, analysis, True, ("needs_more", "duplicate"))
    assert comparison["legacy_decision"] == "rejected"
    assert comparison["proposed_decision"] == "needs_more" and comparison["changed"]
    monkeypatch.setattr(programs, "get_settings", lambda: SimpleNamespace(evidence_decision_mode="active"))
    assert programs._evidence_shadow_comparison(requirement(), file, analysis, True, ("needs_more", "duplicate")) is None


def test_configured_content_classes_override_label_hints():
    req = requirement("lender_operating_history")
    req.label = "Bank statements"
    req.collection_config = {"evidence_classes": ["merchant_processing_statement"]}
    assert programs._expected_classes(req) == {"merchant_processing_statement"}


def test_configured_months_and_distinct_file_minimum_are_both_required():
    req = requirement("lender_cashflow")
    req.collection_config = {"evidence_classes": ["bank_statement"], "required_months": 3, "minimum_count": 2}
    first, analysis = evidence()
    analysis.classification = "bank_statement"
    analysis.analysis = {"key_facts": {"months": [{"month": f"2026-{month:02d}"} for month in (1, 2, 3)]}}
    assert programs._coverage_for_files(req, [first], {first.id: analysis})[0] is False
    second, second_analysis = evidence()
    second_analysis.classification = "bank_statement"
    second_analysis.analysis = analysis.analysis
    # Different filenames or IDs do not turn identical content into two files.
    assert programs._coverage_for_files(req, [first, second], {first.id: analysis, second.id: second_analysis})[0] is False
    second.content_hash = "another-statement"
    complete, coverage = programs._coverage_for_files(req, [first, second], {first.id: analysis, second.id: second_analysis})
    assert complete and coverage["current"] == 3 and coverage["minimum_count"] == 2


def test_configured_tax_year_count_does_not_depend_on_requirement_key():
    req = requirement("lender_tax_package")
    req.collection_config = {"evidence_classes": ["tax_return"], "required_years": 2}
    file, analysis = evidence()
    analysis.classification = "tax_return"
    analysis.analysis = {"key_facts": {"tax_year": 2025}}
    complete, coverage = programs._coverage_for_files(req, [file], {file.id: analysis})
    assert not complete and coverage["required_years"] == 2 and coverage["years"] == ["2025"]


def test_configured_freshness_retains_but_does_not_count_old_uploads():
    req = requirement("lender_financials")
    req.collection_config = {"evidence_classes": ["current_p_and_l"], "expiration_days": 30}
    file, analysis = evidence()
    file.created_at = datetime.now(UTC) - timedelta(days=31)
    complete, coverage = programs._coverage_for_files(req, [file], {file.id: analysis})
    assert not complete and coverage["expired_file_ids"] == [str(file.id)]
    assert programs._automatic_evidence_decision(requirement=req, file=file, analysis=analysis,
        expected_entity=None, duplicate_content=False)[:2] == ("needs_more", "stale")


def test_configured_minimum_count_cannot_be_satisfied_by_reuploads():
    req = requirement("lender_proof")
    req.collection_config = {"evidence_classes": ["current_p_and_l"], "minimum_count": 2}
    first, analysis = evidence()
    second, _ = evidence()
    complete, coverage = programs._coverage_for_files(req, [first, second], {first.id: analysis, second.id: analysis})
    assert not complete and coverage["current"] == 1 and coverage["required"] == 2
