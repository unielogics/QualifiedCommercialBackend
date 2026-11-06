"""Current request projections must not rewrite submitted or issued terms."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services import application_terms, banker_submission, dealer_ai_intelligence_pdf


def intake():
    return SimpleNamespace(
        requested_loan_amount=70_000, variant="mca_refi_v1", full_name="Test Owner",
        email="test@example.com", phone=None, business_name="Test Company", loan_purpose="Refinance",
        asset_rows=[], intake_state={"current_funding_request": {"requested_amount": "200000.00", "revision": 1}},
    )


def test_banker_payload_uses_current_request_and_keeps_original():
    original = intake()
    result = banker_submission.build_banker_payload(
        original, key_metrics={}, program_fit={}, entity_structure={}, owners=[],
    )
    assert result["financing_request"]["requested_loan_amount"] == 200_000
    assert original.requested_loan_amount == 70_000


def test_intelligence_report_uses_current_request(monkeypatch):
    original = intake()
    captured = {}

    class HTML:
        def __init__(self, *, string):
            captured["html"] = string

        def write_pdf(self):
            return b"current-request-report"

    monkeypatch.setitem(__import__("sys").modules, "weasyprint", SimpleNamespace(HTML=HTML))
    rendered = dealer_ai_intelligence_pdf.render_dealer_intelligence_pdf(
        intake=original, files=[], missing_docs=[], result={},
    )
    assert rendered == b"current-request-report"
    assert "$200,000" in captured["html"]
    assert "$70,000" not in captured["html"]
    assert original.requested_loan_amount == 70_000


@pytest.mark.asyncio
@pytest.mark.parametrize("approved,expected", [(None, 200_000), (150_000, 150_000)])
async def test_new_term_defaults_use_current_request_without_replacing_approved_amount(approved, expected):
    original = intake()
    profile = SimpleNamespace(
        id=uuid4(), intake_id=uuid4(), underwriting_term_sheet_amount=None,
        underwriting_approved_amount=approved,
        current_funding_request={"requested_amount": "200000.00", "revision": 1},
    )
    db = SimpleNamespace(get=AsyncMock(return_value=original))
    context = application_terms.DscrContext("business", None, None, "Annual earnings", None, None, "fixture")
    with (
        patch.object(application_terms, "current_term_sheet", AsyncMock(return_value=None)),
        patch.object(application_terms, "loan_type_options", AsyncMock(return_value=[])),
        patch.object(application_terms, "_recipient_state", AsyncMock(return_value=(None, False))),
        patch.object(application_terms, "_dscr_context", AsyncMock(return_value=context)),
        patch.object(application_terms, "_plan_block", AsyncMock(return_value=None)),
    ):
        terms = await application_terms.read_terms(db, profile)
    assert terms.amount == expected
    assert profile.underwriting_approved_amount == approved
    assert original.requested_loan_amount == 70_000
