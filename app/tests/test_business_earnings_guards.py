"""Business coverage cannot revive personal, short-period or mismatched operands."""

from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest

from app.services import application_profiles, application_terms, financial_cockpit
from app.services.financial_metric_reporting import current_review_projection
from app.services.financial_tax_earnings import tax_source_is_business
from app.tests.test_financial_metric_reporting import profile_fixture
from app.tests.test_financial_metric_review import build, file, pnl


@pytest.mark.parametrize("form", ["Form 1065 Schedule K-1", "1120S K-1", "Schedule K1", "Form 1040-SR"])
def test_owner_tax_attachments_never_become_business_coverage(form):
    owner_source = file("tax_return", {
        "form": form, "entity_name": "Example LLC", "tax_year": "2025",
        "ordinary_business_income": 27911, "interest": 0,
        "depreciation_and_amortization": 0,
    }, key="owner-tax")
    assert not tax_source_is_business(owner_source["key_facts"])
    cockpit = build([owner_source])
    assert cockpit["tax"]["years"] == []
    assert cockpit["dscr"]["headline"] is None
    assert cockpit["plan"]["evaluation"]["income_after_annual"] is None
    assert cockpit["metric_review"]["excluded_income_sources"][0]["file_id"] == "owner-tax"
    assert current_review_projection({"key_metrics": {"estimated_dscr": 99}}, cockpit)["key_metrics"]["estimated_dscr"] is None


@pytest.mark.parametrize("start,end", [
    ("2025-07-01", "2025-12-31"),
    ("2025-01-01", None),
    (None, "2025-12-31"),
    ("2025-12-31", "2025-01-01"),
])
def test_explicit_short_partial_or_reversed_tax_period_is_not_an_annual_numerator(start, end):
    source = file("tax_return", {
        "form": "1065", "entity_name": "Example LLC", "tax_year": "2025",
        "period_start": start, "period_end": end,
        "ordinary_business_income": 60000, "interest": 0,
        "depreciation_and_amortization": 0,
    }, key="short-return")
    cockpit = build([source])
    assert cockpit["tax"]["years"][0]["ebitda"] == 60000
    assert cockpit["tax"]["years"][0]["annual_coverage_eligible"] is False
    assert cockpit["ebitda"]["headline"]["value"] == 60000
    assert cockpit["ebitda"]["headline"]["annualized_value"] is None
    assert cockpit["dscr"]["headline"] is None
    assert not any(row["key"] == "tax_return" for row in cockpit["dscr"]["candidates"])
    assert cockpit["plan"]["evaluation"]["income_after_annual"] is None


def test_latest_short_year_blocks_annual_coverage_and_prior_year_citations_stay_comparative():
    full = file("tax_return", {
        "form": "1065", "entity_name": "Example LLC", "tax_year": "2024",
        "period_start": "2024-01-01", "period_end": "2024-12-31",
        "ordinary_business_income": 60000, "interest": 0, "depreciation_and_amortization": 0,
    }, key="full-return")
    short = file("tax_return", {
        "form": "1065", "entity_name": "Example LLC", "tax_year": "2025",
        "period_start": "2025-07-01", "period_end": "2025-12-31",
        "ordinary_business_income": 900000, "interest": 0, "depreciation_and_amortization": 0,
    }, key="short-return")
    cockpit = build([full, short])
    assert cockpit["dscr"]["headline"] is None
    assert cockpit["dscr"]["candidates"] == []
    historical = cockpit["dscr"]["comparative_candidates"][0]
    assert historical["source_file_id"] == "full-return"
    assert historical["numerator"] == 60000 and historical["value"] == 5
    assert historical["source_refs"][0]["file_id"] == "full-return"
    assert all("full-return" in key for key in historical["input_keys"] if key.startswith("tax_return:"))
    assert cockpit["ebitda"]["headline"]["value"] == 900000
    assert cockpit["ebitda"]["headline"]["annualized_value"] is None
    assert not any(row.get("selected") for row in cockpit["metric_review"]["calculations"] if row["key"].startswith("dscr."))
    assert not any(item.get("used_by_headline") for item in cockpit["metric_review"]["inputs"])


@pytest.mark.asyncio
async def test_failed_current_read_cannot_restore_cached_owner_income_or_coverage():
    db, profile, intake = profile_fixture()
    before = deepcopy(intake.result_snapshot)
    with patch("app.services.financial_version_selection.accepted_debt_schedule_snapshot", AsyncMock(return_value=None)), \
         patch.object(financial_cockpit, "load_cockpit", AsyncMock(side_effect=RuntimeError("source unavailable"))):
        intelligence = await application_profiles.intelligence_state(db, profile)
    dscr = next(row for row in intelligence.metrics if row.key == "dscr")
    assert dscr.value is None and dscr.status == "needs_evidence"
    assert intelligence.dscr_inputs["bankable_ebitda"] is None
    assert intake.result_snapshot == before


@pytest.mark.asyncio
async def test_cad_ebitda_keeps_its_currency_and_cannot_reappear_as_usd_client_terms_coverage():
    db, profile, intake = profile_fixture()
    intake.intake_state = {}
    before = deepcopy(intake.result_snapshot)
    cockpit = build([pnl(currency="CAD")])
    assert cockpit["dscr"]["headline"] is None
    with patch("app.services.financial_version_selection.accepted_debt_schedule_snapshot", AsyncMock(return_value=None)), \
         patch.object(financial_cockpit, "load_cockpit", AsyncMock(return_value=cockpit)):
        intelligence = await application_profiles.intelligence_state(db, profile)
        assert next(row for row in intelligence.metrics if row.key == "ebitda").unit == "CAD"
        assert next(row for row in intelligence.metrics if row.key == "ebitda").value == 52000
        assert intelligence.dscr_inputs["bankable_ebitda"] is None
        assert intelligence.dscr_inputs["cash_flow_value"] is None
        with patch.object(application_profiles, "intelligence_state", AsyncMock(return_value=intelligence)):
            context = await application_terms._dscr_context(db, profile)
    assert context.before is None and context.cash_flow is None
    calculation = application_terms._calculation(
        context=context, payment=1000, payment_count=12, payments_per_year=12,
        new_annual_debt_service=12000, amount=10000, total_repayment=12000,
    )
    assert calculation.dscr_after is None
    assert calculation.dscr_status == "needs_evidence"
    assert intake.result_snapshot == before
