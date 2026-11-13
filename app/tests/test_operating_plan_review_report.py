"""The downloadable review follows a saved version, never today's draft."""
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.enums import Role
from app.services import earnings_improvement as engine
from app.services import financial_workspace as ws
from app.services import operating_plan_pdf
from app.services import operating_plan_report as report
from app.tests.test_financial_workspace import Memory, context
from app.tests.test_operating_plan_service import cockpit, proposal
from app.tests.test_refinance_reinvestment_independent import accounting_change, saved_loan


def snapshot(amount=6000):
    body, source = accounting_change(amount), saved_loan()
    source["plan"]["evaluation"].update(debt_service_after_annual=78000, coverage_unavailable_reason=None)
    evaluation = engine.evaluate(body, source, include_expense_changes=True)
    saved = report.capture(evaluation, version_id=uuid4(), version_number=3, saved_at=datetime(2026, 11, 13, 12, tzinfo=UTC))
    return body, evaluation, saved


def test_report_copies_canonical_review_outputs_with_accounting_separation():
    body, evaluation, saved = snapshot()
    assert saved["working"]["noi"] == 24000
    assert saved["working"]["earnings"] == 30000
    assert saved["working"]["cash_after_debt_after_annual"] == -48000
    assert saved["working"]["dscr_with_refinance_after"] == .3077
    assert saved["working"]["noi_delta"] == 6000
    for key in report.WORKING_FIELDS:
        assert saved["working"][key] == evaluation["draft_feedback"]["summary"]["working"].get(key)
    evaluation["draft_feedback"]["summary"]["working"]["noi"] = 999999
    body["notes"] = "Later working draft"
    assert saved["working"]["noi"] == 24000 and "Later working draft" not in str(saved)


def test_report_has_version_signed_deltas_currencies_and_no_forged_ebitda():
    _, _, saved = snapshot()
    text = report.build_html(saved)
    assert "Saved plan v3" in text and "2026-11-13T12:00:00+00:00" in text
    assert "+$6,000.00" in text and "+$42,000.00" in text and "0.3077x" in text
    assert "Annual EBITDA" in text and "0.2500x" in text
    saved["summary"].update(earnings_kind="tax_derived_estimate", noi_currency="CAD", earnings_currency="USD")
    text = report.build_html(saved)
    assert "Annual earnings (tax estimate)" in text and "Annual EBITDA" not in text
    assert "CAD 24,000.00" in text and "$30,000.00" in text


def test_partial_or_changed_source_is_not_a_complete_saved_forecast():
    _, _, saved = snapshot()
    original = deepcopy(saved)
    saved["summary"].update(complete=False, status="source_blocked", source_comparable=False)
    text = report.build_html(saved)
    assert "Needs source inputs" in text and "Some saved inputs still need review" in text
    assert "$24,000.00" not in text and "+$6,000.00" not in text
    assert saved["working"] == original["working"]
    assert report.build_html(None) == ""


def test_two_saved_reports_render_as_different_readable_immutable_pdfs():
    import fitz

    body, _, first = snapshot()
    second_body, _, second = snapshot(4000)
    second.update(version_id=str(uuid4()), version_number=4)
    first_bytes = operating_plan_pdf.render_pdf(body=body, financial_totals={"operating_review": first})
    second_bytes = operating_plan_pdf.render_pdf(body=second_body, financial_totals={"operating_review": second})
    first_text = " ".join(page.get_text() for page in fitz.open(stream=first_bytes, filetype="pdf"))
    second_text = " ".join(page.get_text() for page in fitz.open(stream=second_bytes, filetype="pdf"))
    assert "Saved plan v3" in first_text and "$24,000.00" in first_text
    assert "Saved plan v4" in second_text and "$22,000.00" in second_text
    assert "$22,000.00" not in first_text and first_bytes != second_bytes
    assert "Cash after debt" in first_text and "+0.0577x" in first_text


@pytest.mark.asyncio
async def test_save_version_captures_report_before_render_and_replay_preserves_pdf():
    db = Memory()
    profile, family, draft = context("operating_plan")
    draft.body = proposal()
    actor = SimpleNamespace(id=uuid4(), name="Desk", role=Role.LOAN_EXEC)
    slot = SimpleNamespace(id=uuid4(), status="requested")
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), \
         patch.object(ws, "load_workspace", AsyncMock(return_value={})), \
         patch.object(ws, "_render", return_value=b"%PDF immutable saved report") as render, \
         patch.object(ws, "_write_pdf") as storage, \
         patch("app.routers.buckets._bucket_storage_config", return_value=("bucket", "prefix", None)), \
         patch("app.routers.application_profiles._requested_slot", AsyncMock(return_value=slot)), \
         patch("app.services.financial_publication.after_publication", AsyncMock()), \
         patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=cockpit())), \
         patch.object(ws, "_project", AsyncMock()):
        first = await ws.publish_version(db, profile, kind="operating_plan", expected_revision=4, idempotency_key="saved-report", actor=actor)
        repeat = await ws.publish_version(db, profile, kind="operating_plan", expected_revision=4, idempotency_key="saved-report", actor=actor)
    assert first["saved_version"]["report_schema"] == report.SCHEMA
    assert repeat["saved_version"] == first["saved_version"]
    captured = render.call_args.args[2]["operating_review"]
    assert captured["version_id"] == first["saved_version"]["id"]
    assert captured["saved_at"] == first["saved_version"]["created_at"]
    assert captured["version_number"] == 1 and storage.call_count == render.call_count == 1
    saved = deepcopy(db.versions[0].key_facts["operating_review"])
    draft.body["notes"] = "Unpublished edits"
    assert db.versions[0].key_facts["operating_review"] == saved
