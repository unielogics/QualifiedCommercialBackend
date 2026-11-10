from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import fitz
import pytest
from fastapi import HTTPException

from app.models.financial_form_version import FinancialFormDraft
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.routers import dealer_ai_intake as core
from app.schemas.earnings_improvement import empty_body
from app.services import (
    application_programs,
    financial_cockpit,
    financial_workspace,
    investor_package,
    use_of_funds,
)
from app.services.investor_presentation_pdf import (
    investment_metrics,
    operating_ebitda_projection,
    render_investor_presentation_pdf,
)
from app.services.loan_schedule import build_schedule


def synthetic_financials():
    return {"current_key_metrics": {"dscr": -0.2, "dscr_earnings": -20_000, "dscr_earnings_label": "Tax-derived earnings estimate",
        "annual_ebitda": None, "annual_noi": None, "monthly_debt_service": 8_333.33},
        "use_of_funds": {"requested_amount": 200_000, "total": 200_000, "unallocated_amount": 0,
                        "category_totals": {"debt_refinance": 70_000, "working_capital": 130_000}},
        "operating_earnings": {"historical": {"currency": "USD", "kind": "tax_estimate"}, "improvement_plan": {
            "snapshot": {"state": "working_draft", "label": "Unreviewed working draft - conditional illustration"},
            "baseline": {"currency": "USD", "earnings": -20_000, "dscr": -.2},
            "projection": {"proposed": {"earnings": None, "noi": None, "dscr_current": None}},
            "disclosure": {"cost_mix": [{"category": "software", "annual_amount": 24_000}]},
            "expenses": [{"label": "Software subscriptions", "category": "software", "cost_basis": "per_unit", "unit_label": "seat",
                          "current_units_per_period": 10, "current_unit_cost": 150, "current_fixed_amount": 500,
                          "current_amount": 9999, "effective_period_amount": 2000, "cadence": "monthly", "source_names": ["Synthetic business return.pdf"]}],
            "adjustments": [{"label": "Software license review", "review_state": "proposed", "explanation": "Confirm active users before changing subscriptions.",
                             "source_note": "Vendor quote and timing are missing.", "source_names": ["Synthetic business return.pdf"],
                             "issues": ["Missing proposed amount", "Missing implementation cost", "Missing effective date"]}],
            "plan_notes": "Sensitivity only; no achieved or approved savings.", "missing": ["Current P&L", "Rent and payroll details"]}},
        "package_readiness": {"requirements": [{"label": "Current balance sheet", "effectively_open": True}]}}


def test_returns_require_complete_cash_flow_terms_and_separate_gross_from_net():
    assert investment_metrics({"amount": 100_000, "rate_pct": 15})["gross_contractual_roi_pct"] is None
    schedule = build_schedule(100_000, 12, 24, structure="amortizing")
    result = investment_metrics({"amount": 100_000, "term_months": 24, "rate_pct": 12, "schedule": schedule})
    assert result["gross_contractual_roi_pct"] == round((schedule["total_paid"] - 100_000) / 100_000 * 100, 2)
    assert result["net_investor_roi_pct"] is None
    assert "not annualized" in result["note"]


def test_principal_chart_uses_the_real_shared_terms_schedule_and_counts_maturity_balloon_once():
    from app.capital_partners.services.sync import proposed_terms

    terms = proposed_terms({"dscr": {"proposed": {"amount": 100_000, "rate_pct": 12, "term_months": 24,
        "structure": "amortizing", "payments_per_year": 12, "treatment": "refinance", "retained_annual_debt_service": 0,
        "repayment_config": {"amortization_months": 60, "after_balloon": "keep_payment", "balloons": []}},
        "annual_debt_service": 100_000, "headline": {"numerator": 120_000, "currency_comparable": True}}, "plan": {}})
    assert terms["schedule_rows"][-1]["balance_after_cash"] == 0
    assert terms["schedule_rows"][-1]["maturity_balloon"] == terms["schedule"]["balloon"]
    assert sum(row["cash_payment"] for row in terms["schedule_rows"]) == pytest.approx(terms["schedule"]["total_paid"], abs=.02)
    data = synthetic_financials()
    data["proposed_terms"] = terms
    pdf = render_investor_presentation_pdf(intake=SimpleNamespace(business_name="Synthetic LLC"), financials=data)
    page = fitz.open(stream=pdf, filetype="pdf")[2]
    text = " ".join(page.get_text().split())
    assert "$0 remains after all due principal, including the balloon, is received" in text
    assert "$66,973" in text


def test_negative_earnings_incomplete_plan_and_current_request_render_without_guessed_results():
    intake = SimpleNamespace(business_name="Synthetic Wellness LLC", full_name="Test Owner", requested_loan_amount=70_000, intake_state={})
    pdf = render_investor_presentation_pdf(intake=intake, financials=synthetic_financials(), generated_at=datetime(2026, 11, 10, tzinfo=UTC))
    document = fitz.open(stream=pdf, filetype="pdf")
    text = " ".join(" ".join(page.get_text().split()) for page in document)
    assert all(page.rect.width == 960 and page.rect.height == 540 for page in document)
    assert "$200,000" in text
    assert "-$20,000" in text and "-0.20x" in text
    assert "Unreviewed working draft" in text
    assert "Terms needed" in text and "Pending evidence" in text
    assert "Validated P&L EBITDA is unavailable" in text
    assert "per seat: quantity 10" in text and "$2,000 per month" in text and "$9,999" not in text
    assert "Missing proposed amount" in text and "Current balance sheet" in text
    assert "Working Capital: $130,000" in text


def test_long_expense_and_action_explanations_paginate_without_losing_final_words():
    data = synthetic_financials()
    plan = data["operating_earnings"]["improvement_plan"]
    plan["expenses"][0]["notes"] = "Long source note. " * 240 + "EXPENSE-END-MARKER"
    plan["adjustments"][0]["explanation"] = "Long operating explanation. " * 220 + "ACTION-END-MARKER"
    plan["plan_notes"] = "Review assumption. " * 500 + "PLAN-END-MARKER"
    pdf = render_investor_presentation_pdf(intake=SimpleNamespace(business_name="Synthetic LLC"), financials=data)
    document = fitz.open(stream=pdf, filetype="pdf")
    text = "\n".join(page.get_text() for page in document)
    assert all(marker in text for marker in ("EXPENSE-END-MARKER", "ACTION-END-MARKER", "PLAN-END-MARKER"))
    assert len(document) > 8


def documented_ebitda_financials():
    data = synthetic_financials()
    data["current_key_metrics"].update(annual_ebitda=120_000, dscr_earnings=120_000, dscr=1.2, dscr_earnings_label="P&L EBITDA")
    data["operating_earnings"]["historical"] = {"kind": "computed_ebitda", "complete": True, "provisional": False,
        "currency": "USD", "basis": "accrual", "period": "2025", "annualized_value": 120_000}
    data["operating_earnings"]["improvement_plan"]["baseline"].update(earnings_kind="computed_ebitda", provisional=False,
        currency="USD", basis="accrual", period="2025", earnings=120_000)
    data["operating_earnings"]["improvement_plan"]["projection"] = {
        "proposed": {"status": "unreviewed_illustration", "earnings": 138_000},
        "approved": {"status": "reviewed_illustration", "earnings": 132_000}}
    data["plan"] = {"active": True, "impact": {"ebitda": {"annual_after": 120_000, "after": 10_000}}}
    return data


def test_documented_matching_annual_ebitda_shows_operating_effect_and_annual_refinance_basis():
    data = documented_ebitda_financials()
    assert operating_ebitda_projection(data) == 138_000
    assert operating_ebitda_projection(data, "approved") == 132_000
    pdf = render_investor_presentation_pdf(intake=SimpleNamespace(business_name="Synthetic LLC"), financials=data)
    page = fitz.open(stream=pdf, filetype="pdf")[1]
    text = " ".join(page.get_text().split())
    assert "Annual EBITDA (validated P&L) $120,000 $120,000 $138,000" in text
    assert "Staff-reviewed operating EBITDA projection: $132,000" in text
    assert "$10,000" not in text  # Period EBITDA must not replace the annual row.


@pytest.mark.parametrize("change", ["tax_estimate", "currency", "period", "basis", "amount", "provisional", "incomplete"])
def test_operating_ebitda_never_relabels_tax_or_mismatched_incomplete_figures(change):
    data = documented_ebitda_financials()
    baseline = data["operating_earnings"]["improvement_plan"]["baseline"]
    if change == "tax_estimate":
        baseline["earnings_kind"] = "tax_estimate"
        data["operating_earnings"]["historical"]["kind"] = "tax_estimate"
    elif change == "currency":
        baseline["currency"] = "CAD"
    elif change == "period":
        baseline["period"] = "2024"
    elif change == "basis":
        baseline["basis"] = "cash"
    elif change == "amount":
        baseline["earnings"] = 100_000
    elif change == "provisional":
        baseline["provisional"] = True
    else:
        data["operating_earnings"]["improvement_plan"]["projection"]["proposed"]["status"] = "unavailable"
    assert operating_ebitda_projection(data) is None


@pytest.mark.asyncio
async def test_working_plan_snapshot_is_explicit_read_only_and_cannot_carry_approval():
    family = SimpleNamespace(id=uuid4(), current_version_id=None)
    source_id = str(uuid4())
    body = {**empty_body(), "business_name": "Synthetic LLC", "period_start": "2025-01-01", "period_end": "2025-12-31",
        "expenses": [{"id": str(uuid4()), "label": "Software", "category": "software", "current_amount": 24_000, "cadence": "annual", "source_file_ids": [source_id]}],
        "adjustments": [{"id": str(uuid4()), "label": "Review licenses", "kind": "expense_reduction", "status": "approved", "review_reason": "private staff note"}]}
    original = deepcopy(body)
    draft = SimpleNamespace(body=body, revision=9)
    db = SimpleNamespace(get=AsyncMock(return_value=draft))
    cockpit = {"operating_evidence": {"files": [{"file_id": source_id, "file_name": "Synthetic.pdf"}]}}
    with patch.object(financial_workspace, "existing_family", AsyncMock(return_value=family)):
        assert await investor_package.operating_snapshot(db, SimpleNamespace(id=uuid4()), cockpit, {"include_operating_plan": False}) is None
        db.get.assert_not_awaited()
        result = await investor_package.operating_snapshot(db, SimpleNamespace(id=uuid4()), cockpit,
            {"include_operating_plan": True, "include_working_operating_plan": True})
    db.get.assert_awaited_once_with(FinancialFormDraft, family.id)
    assert body == original
    assert result["snapshot"]["revision"] == 9
    assert result["snapshot"]["state"] == "working_draft"
    assert result["adjustments"][0]["review_state"] == "proposed"
    assert result["adjustments"][0]["included_in_approved_projection"] is False
    assert "private staff note" not in str(result)
    assert result["expenses"][0]["source_names"] == ["Synthetic.pdf"]


@pytest.mark.asyncio
async def test_package_rehydrates_orm_intake_after_readiness_expires_eager_relationships():
    intake = PublicUnderwritingIntake(id=uuid4(), bucket_id=uuid4(), business_name="Synthetic LLC", full_name="Test Owner")
    phases = []
    profile = SimpleNamespace(id=uuid4())
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))))
    readiness = SimpleNamespace(selection_mode="manual", selections=[], requirements=[], programs=[], can_advance=False, automatic_stage_status="not_ready")

    async def refresh_readiness(*_):
        phases.append("readiness refreshed intake identity")
        return readiness

    async def rehydrate(*_):
        phases.append("relationships eager loaded")
        return intake

    async def context(*_, **__):
        assert phases == ["readiness refreshed intake identity", "relationships eager loaded"]
        return {}

    with (patch.object(core.profiles_service, "provision_profile_for_intake", AsyncMock(return_value=profile)),
          patch.object(application_programs, "get_program_readiness", refresh_readiness),
          patch.object(core, "_load_admin_dealer_lead", side_effect=rehydrate) as eager,
          patch.object(core, "_lead_management_context", side_effect=context),
          patch.object(core, "_collect_packet_financials", AsyncMock(return_value={})),
          patch.object(financial_cockpit, "load_cockpit", AsyncMock(return_value={})),
          patch.object(use_of_funds, "source_funding_data", AsyncMock(return_value=(Decimal(200_000), "profile.current_funding_request", None, None))),
          patch.object(use_of_funds, "summarize", return_value={}), patch.object(use_of_funds, "ai_context", return_value={}),
          patch.object(investor_package, "operating_snapshot", AsyncMock(return_value={"snapshot": {"state": "working_draft"}, "baseline": {"earnings": None}}))):
        _, metadata, context_snapshot, financials, _ = await core._package_generation_inputs(db, intake,
            {"include_operating_plan": True, "include_working_operating_plan": True})
    eager.assert_awaited_once_with(db, intake.id)
    assert context_snapshot["requested_loan_amount"] == 200_000
    assert metadata["package_document_types"] == ["executive_summary", "lender_packet", "investor_presentation"]
    assert financials["operating_earnings"]["improvement_plan"]["snapshot"]["state"] == "working_draft"
    assert financials["operating_earnings"]["improvement_plan"]["baseline"]["earnings"] is None


@pytest.mark.asyncio
async def test_new_package_delivery_requires_its_same_generation_presentation():
    generation = str(uuid4())
    summary = SimpleNamespace(body_json={"_package": {"status": "current", "generation_id": generation, "bucket_file_id": str(uuid4())},
        "_pdf": {"package_document_types": list(core._PACKAGE_READINESS_TYPES), "package_input_sha256": "f" * 64}})
    packet = SimpleNamespace(body_json={"_package": {"status": "current", "generation_id": generation, "bucket_file_id": str(uuid4())}})
    with patch.object(core, "_latest_artifact", AsyncMock(side_effect=[summary, packet, None])):
        with pytest.raises(HTTPException) as error:
            await core._fresh_package_artifact_pair(SimpleNamespace(), SimpleNamespace(id=uuid4()))
    assert error.value.status_code == 409
    assert "investor presentation" in error.value.detail


@pytest.mark.asyncio
async def test_presentation_render_failure_rolls_back_without_superseding_existing_package():
    from app.enums import Role

    intake = SimpleNamespace(id=uuid4(), bucket_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    db = SimpleNamespace(execute=AsyncMock(), rollback=AsyncMock(), commit=AsyncMock())

    async def created(*_, **kwargs):
        kwargs["cleanup_s3_keys"].append("new-synthetic.pdf")
        return SimpleNamespace()

    with (patch.object(core, "_load_admin_dealer_lead", AsyncMock(return_value=intake)),
          patch.object(core, "_latest_artifact", AsyncMock(return_value=None)),
          patch.object(core, "_package_generation_inputs", AsyncMock(return_value=([], {}, {}, {}, "f" * 64))),
          patch.object(core, "_create_executive_summary_artifact", side_effect=created),
          patch.object(core, "_create_lender_packet_artifact", side_effect=created),
          patch.object(core, "_create_investor_presentation_artifact", AsyncMock(side_effect=ValueError("synthetic render failure"))),
          patch.object(core, "_publish_package_artifact_to_bucket", AsyncMock()) as publish,
          patch.object(core, "_delete_s3_object") as cleanup):
        with pytest.raises(ValueError, match="synthetic render failure"):
            await core.generate_package_readiness_documents(intake.id, user, db)
    publish.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()
    assert cleanup.call_count == 2
