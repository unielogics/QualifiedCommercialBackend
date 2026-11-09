import json
import sys
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.capital_partners.services import listings, sync
from app.routers.dealer_ai_intake import _package_input_fingerprint
from app.services import application_profiles, financial_cockpit
from app.services import public_underwriting_packet_pdf as packet
from app.services.financial_metric_reporting import (
    current_intake_report,
    current_review_projection,
    metric_brief,
    operating_earnings_brief,
    validated_annual_pair,
)
from app.tests.test_financial_metric_review import build


def earnings_cockpit():
    cockpit = build()
    historical = cockpit["ebitda"]["headline"]
    historical.update(value=52000, currency="USD", period="2025", label="Computed EBITDA")
    historical["source_refs"] = [{"file_id": "business-2025", "file_name": "Business 2025.pdf", "currency": "USD",
                                  "identity_association": {"status": "accepted", "history": [{"reason": "PRIVATE REASON"}],
                                                           "current_decision": {"actor_name": "PRIVATE ACTOR"}}}]
    cockpit["ebitda"]["source_selection"] = {
        "mode": "manual", "selected_file_id": "business-2025", "excluded_file_ids": ["personal"],
        "source_stamp": "PRIVATE STAMP", "revision": 4, "stale": False, "reason": "PRIVATE REASON",
        "sources": [{"file_id": "business-2025", "file_name": "Business 2025.pdf", "eligible": True, "included": True, "selected": True},
                    {"file_id": "personal", "file_name": "PRIVATE PERSONAL.pdf", "eligible": False, "included": False, "selected": False}],
    }
    cockpit["ebitda"]["revenue_trend"] = {
        "status": "available", "periods": [
            {"year": "2024", "revenue": 400000, "average_monthly_revenue": 33333.33, "currency": "USD", "file_id": "business-2024", "file_name": "Business 2024.pdf"},
            {"year": "2025", "revenue": 500000, "average_monthly_revenue": 41666.67, "currency": "USD", "file_id": "business-2025", "file_name": "Business 2025.pdf"},
        ],
        "comparison": {"prior_year": "2024", "current_year": "2025", "prior_revenue": 400000, "current_revenue": 500000,
                       "change_amount": 100000, "change_pct": 25, "comparable": True},
        "forecast": {"status": "illustration", "method": "reviewed_assumptions", "target_year": 2026, "projected_revenue": 550000,
                     "change_pct": 10, "projected_ebitda": 57200, "ebitda_kind": "EBITDA", "margin": .104,
                     "assumptions": ["Reviewed revenue growth of 10%", "Current operating margin held constant"],
                     "source_refs": historical["source_refs"], "actor_name": "PRIVATE ACTOR"},
        "bank_context": {"average_monthly_deposits": 70000, "note": "Bank deposits are not revenue"},
    }
    return cockpit


def test_shared_report_snapshot_keeps_history_forecast_and_source_selection_separate():
    cockpit = earnings_cockpit()
    before = deepcopy(cockpit)
    earnings = operating_earnings_brief(cockpit)
    brief = metric_brief(cockpit)
    projected = current_review_projection({"key_metrics": {"annual_ebitda": 999999}}, cockpit)
    assert earnings["historical"]["value"] == 52000
    assert earnings["revenue_trend"]["forecast"]["projected_ebitda"] == 57200
    assert brief["ebitda"]["source_selection"]["selected_file_id"] == "business-2025"
    assert projected["operating_earnings"] == earnings
    assert projected["key_metrics"]["annual_ebitda"] != 57200
    serialized = json.dumps(brief)
    assert not any(value in serialized for value in ("PRIVATE REASON", "PRIVATE ACTOR", "PRIVATE STAMP"))
    assert cockpit == before


@pytest.mark.parametrize("selection", [
    {"selected_file_id": None, "stale": False},
    {"selected_file_id": "tax-only", "stale": False},
    {"selected_file_id": "old-source", "stale": True},
])
def test_excluded_or_stale_source_never_supplies_annual_program_ebitda(selection):
    cockpit = build()
    assert validated_annual_pair(cockpit) is not None
    cockpit["ebitda"]["source_selection"] = selection
    assert validated_annual_pair(cockpit) is None
    result = current_review_projection({"key_metrics": {"estimated_ebitda_or_cash_flow": 999999}}, cockpit)
    assert result["key_metrics"]["annual_ebitda"] is None
    assert result["key_metrics"]["estimated_ebitda_or_cash_flow"] is None


def test_marketplace_member_receives_figures_without_private_document_inventory():
    earnings = operating_earnings_brief(earnings_cockpit())
    result = listings.partner_financials(SimpleNamespace(source_snapshot={"financials": {"operating_earnings": earnings}}))
    assert result["operating_earnings"]["historical"]["value"] == 52000
    assert result["operating_earnings"]["revenue_trend"]["comparison"]["change_pct"] == 25
    assert result["operating_earnings"]["revenue_trend"]["forecast"]["projected_revenue"] == 550000
    text = json.dumps(result)
    assert not any(value in text for value in ("Business 2025.pdf", "PRIVATE PERSONAL", "business-2025", "personal", "source_refs", "PRIVATE REASON"))
    assert "assumptions" not in result["operating_earnings"]["revenue_trend"]["forecast"]


def test_source_choice_and_forecast_changes_invalidate_package_and_listing_figures(monkeypatch):
    cockpit = earnings_cockpit()
    monkeypatch.setattr(financial_cockpit, "listing_summary", lambda _: {"generated_at": "ignored", "ebitda": 52000})
    monkeypatch.setattr(financial_cockpit, "plan_summary", lambda _: {"active": False, "modifications": [], "impact": {}, "note": None})
    monkeypatch.setattr(financial_cockpit, "proposed_terms_block", lambda _: None)
    original = sync.fingerprints(sync.file_sections(cockpit, program_keys=[], package=None))
    financials = {"operating_earnings": operating_earnings_brief(cockpit)}
    package_before = _package_input_fingerprint(source_metadata={}, context_snapshot={}, financials=financials)
    cockpit["ebitda"]["source_selection"]["selected_file_id"] = "replacement-same-amount"
    changed = sync.fingerprints(sync.file_sections(cockpit, program_keys=[], package=None))
    assert original["figures"] != changed["figures"] and original["programs"] == changed["programs"]
    financials["operating_earnings"] = operating_earnings_brief(cockpit)
    assert _package_input_fingerprint(source_metadata={}, context_snapshot={}, financials=financials) != package_before
    before_forecast = changed["figures"]
    cockpit["ebitda"]["revenue_trend"]["forecast"]["projected_revenue"] = 560000
    assert sync.fingerprints(sync.file_sections(cockpit, program_keys=[], package=None))["figures"] != before_forecast


@pytest.mark.parametrize("executive", [True, False])
def test_both_package_pdfs_print_current_source_and_forecast_not_cached_ebitda(executive):
    earnings = operating_earnings_brief(earnings_cockpit())
    financials = {"operating_earnings": earnings, "current_key_metrics": {"estimated_dscr": None}, "bank_months": []}
    captured = {}

    def capture(html):
        captured["html"] = html
        return b"%PDF-reviewed-earnings"

    intake = SimpleNamespace(full_name="Test Owner", business_name="Example LLC", requested_loan_amount=200000, loan_purpose="Refinance")
    with patch.dict(sys.modules, {"weasyprint": None}), patch.object(packet, "_render_html_pymupdf", side_effect=capture), patch.object(packet, "_apply_watermark", side_effect=lambda value: value):
        common = {"intake": intake, "result": {"key_metrics": {"estimated_ebitda_or_cash_flow": 999999}}, "executive_summary": {"key_metrics": [{"label": "EBITDA", "value": "$999,999"}]}, "financials": financials}
        result = packet.render_executive_summary_pdf(**common) if executive else packet.render_underwriting_packet_pdf(**common, files=[], missing_docs=[])
    assert result == b"%PDF-reviewed-earnings"
    html = captured["html"]
    assert "$52,000" in html and "$57,200" in html and "$550,000" in html
    assert "Business 2025.pdf" in html and "+25.0%" in html and "+10.0%" in html
    assert "$999,999" not in html and "PRIVATE PERSONAL" not in html
    assert "Reviewed-assumptions illustration" in html and "do not establish current program eligibility" in html


@pytest.mark.asyncio
async def test_client_intelligence_projection_uses_current_earnings_and_hides_sources():
    cockpit = earnings_cockpit()
    with patch.object(application_profiles, "find_profile", AsyncMock(return_value=SimpleNamespace(id=uuid4()))), patch.object(financial_cockpit, "load_cockpit", AsyncMock(return_value=cockpit)):
        result = await current_intake_report(None, SimpleNamespace(id=uuid4(), variant="mca_refi_v1"), {"key_metrics": {"annual_ebitda": 999999}}, external=True)
    assert result["operating_earnings"]["historical"]["value"] == 52000
    assert "financial_calculation_review" not in result and "financial_calculation_authority" not in result
    assert "source_refs" not in json.dumps(result["operating_earnings"])


@pytest.mark.asyncio
async def test_failed_current_intelligence_clears_cached_earnings_without_changing_real_estate():
    cached = {"key_metrics": {"annual_ebitda": 999999, "estimated_dscr": 99}}
    with patch.object(application_profiles, "find_profile", AsyncMock(side_effect=RuntimeError("unavailable"))):
        result = await current_intake_report(None, SimpleNamespace(id=uuid4(), variant="mca_refi_v1"), cached)
    assert result["key_metrics"]["annual_ebitda"] is None and result["key_metrics"]["estimated_dscr"] is None
    real_estate = await current_intake_report(None, SimpleNamespace(variant="real_estate_dscr_v1"), cached)
    assert real_estate == cached and real_estate is not cached


@pytest.mark.parametrize("selected", [None, "tax"])
def test_published_program_fact_respects_latest_period_and_manual_unavailable_receipt(selected):
    from app.enums import Role
    from app.services.financial_earnings_sources import change
    from app.services.program_earnings_facts import annual_ebitda_fact
    from app.tests.test_financial_metric_review import pnl, tax

    analyses = [pnl(), tax()]
    current = build(analyses)
    selection = current["ebitda"]["source_selection"]
    if selected == "tax":
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as error:
            change(None, selection, mode="manual", selected_file_id=selected, excluded_file_ids=["source"],
                reason="Try the older tax period", expected_revision=0, source_stamp=selection["source_stamp"],
                idempotency_key="source-for-program-test", actor=SimpleNamespace(id=uuid4(), name="Reviewer", role=Role.LOAN_EXEC))
        assert error.value.status_code == 422
        assert "latest business reporting period" in error.value.detail
        value, fact = annual_ebitda_fact(analyses, entity="Example LLC")
        assert value == 52000 and fact["selected_file_id"] == "source"
        return
    state, _ = change(None, selection, mode="manual", selected_file_id=selected, excluded_file_ids=["source"],
                      reason="Reviewed the current source choice", expected_revision=0, source_stamp=selection["source_stamp"],
                      idempotency_key="source-for-program-test", actor=SimpleNamespace(id=uuid4(), name="Reviewer", role=Role.LOAN_EXEC))
    value, fact = annual_ebitda_fact(analyses, entity="Example LLC", state=state)
    assert value is None and fact["verification_status"] == "unknown"
    assert fact["selected_file_id"] == selected
    assert "Reviewed the current source choice" not in str(fact)


def test_published_program_fact_rejects_stale_selection_and_forecast_cannot_qualify():
    from app.enums import Role
    from app.services.financial_earnings_sources import change
    from app.services.program_earnings_facts import annual_ebitda_fact
    from app.tests.test_financial_metric_review import pnl

    analyses = [pnl()]
    selection = build(analyses)["ebitda"]["source_selection"]
    state, _ = change(None, selection, mode="manual", selected_file_id="source", excluded_file_ids=[],
                      reason="Reviewed current source and forecast assumptions", expected_revision=0, source_stamp=selection["source_stamp"],
                      idempotency_key="source-for-program-test", actor=SimpleNamespace(id=uuid4(), name="Reviewer", role=Role.LOAN_EXEC),
                      forecast_assumptions={"revenue_growth_pct": 100, "ebitda_margin_pct": 90})
    assert annual_ebitda_fact(analyses, entity="Example LLC", state=state)[0] == 52000
    analyses[0]["analysis_id"] = "reread-replacement"
    value, fact = annual_ebitda_fact(analyses, entity="Example LLC", state=state)
    assert value is None and fact["source_selection_stale"]


def test_program_source_catalog_matches_cockpit_including_denied_evidence():
    from app.enums import Role
    from app.services import document_associations
    from app.services.financial_earnings_sources import change
    from app.services.program_earnings_facts import annual_ebitda_fact
    from app.tests.test_financial_metric_review import pnl, tax

    profile_id = str(uuid4())
    analyses = [pnl(), tax(entity_name="Another LLC")]
    associations = document_associations.attach_rows(analyses, profile_id=profile_id, expected_entity="Example LLC")
    other = next(row for row in associations if row["file_id"] == "tax")["identity_association"]
    identity_state = {"revision": 1, "events": [{"id": "identity-review", "identity_file_id": "tax", "document_family_id": "tax",
                                                "action": "deny", "source_stamp": other["source_stamp"]}]}
    current = build(analyses, state=identity_state, profile_id=profile_id)
    selection = current["ebitda"]["source_selection"]
    state, _ = change(identity_state, selection, mode="manual", selected_file_id="source", excluded_file_ids=[],
                      reason="Reviewed full current source catalog", expected_revision=1, source_stamp=selection["source_stamp"],
                      idempotency_key="current-full-program-catalog", actor=SimpleNamespace(id=uuid4(), name="Reviewer", role=Role.LOAN_EXEC))
    full_rows = document_associations.attach_rows(analyses, profile_id=profile_id, expected_entity="Example LLC", state=state)
    assert next(row for row in full_rows if row["file_id"] == "tax")["identity_association"]["status"] == "denied"
    value, fact = annual_ebitda_fact(full_rows, entity="Example LLC", state=state)
    assert value == 52000 and not fact["source_selection_stale"]
    # A partial source inventory must fail closed rather than silently turning
    # the exact saved selection into another effective receipt.
    assert annual_ebitda_fact([row for row in full_rows if row["file_id"] != "tax"], entity="Example LLC", state=state)[0] is None
