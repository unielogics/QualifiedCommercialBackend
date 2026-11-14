"""One governed NOI basis reaches current, shared and projected read models."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.capital_partners.services.listings import partner_operating_earnings
from app.enums import Role
from app.schemas.earnings_improvement import empty_body
from app.services import (
    application_profiles,
    earnings_improvement,
    financial_cockpit,
    financial_noi_review,
    financial_version_selection,
)
from app.services.application_programs import _apply_current_business_coverage
from app.services.financial_calculation_policy import financial_authority
from app.services.financial_metric_reporting import (
    current_intake_report,
    current_review_projection,
    operating_earnings_brief,
)
from app.services.financial_noi import preview_scenario
from app.services.operating_plan_pdf import _before_after
from app.services.public_underwriting_packet_pdf import _operating_earnings_lines
from app.tests.test_financial_metric_reporting import profile_fixture
from app.tests.test_financial_metric_review import build, pnl, tax

ACTOR = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC, name="AUDIT_PRIVATE_ACTOR")
CANONICAL_FIELDS = ("policy_version", "status", "review_needed", "status_reason", "reason_codes")


def business_tax(annual_noi=-15000, *, currency="USD"):
    source = tax(
        tax_form="1120S", tax_year="2025", currency=currency, basis="cash",
        period_start="2025-01-01", period_end="2025-12-31",
        ordinary_business_income=annual_noi - 5000, interest=5000,
        depreciation=12000,
    )
    source.update(file_name="INVENTORY_PRIVATE_SOURCE.pdf", analysis_id="INVENTORY_PRIVATE_ANALYSIS")
    return source


def annual_pnl(**changes):
    return pnl(**{
        "period_start": "2025-01-01", "period_end": "2025-12-31", "basis": "cash",
        "gross_revenue": 500000, "cost_of_goods_sold": 100000,
        "total_operating_expenses": 380000, "operating_income": 20000,
        "net_income": 15000, "interest": 5000, "income_taxes": 0,
        "depreciation_and_amortization": 10000, **changes,
    })


def review_state(cockpit, **changes):
    payload = {
        "mode": "manual", "annual_value": 42000, "year": 2024, "currency": "EUR",
        "reason": "AUDIT_PRIVATE_REVIEW_REASON", "expected_revision": 0,
        "source_stamp": cockpit["noi"]["review"]["source_stamp"],
        "idempotency_key": "AUDIT_PRIVATE_IDEMPOTENCY", **changes,
    }
    return financial_noi_review.change(None, cockpit["noi"]["review"], actor=ACTOR, **payload)[0]


def disclosure():
    return {
        **empty_body(), "business_name": "Example LLC", "currency": "USD", "basis": "cash",
        "period_start": "2025-01-01", "period_end": "2025-12-31",
        "projection_start": "2026-01-01", "no_operating_expenses_confirmed": True,
    }


def assert_public_noi(shared, canonical):
    for key in CANONICAL_FIELDS:
        assert shared[key] == canonical[key]
    assert "authority_stamp" not in shared
    assert "review" not in shared
    assert "AUDIT_PRIVATE_" not in json.dumps(shared)
    assert "INVENTORY_PRIVATE_" not in json.dumps(shared)


@pytest.mark.asyncio
@pytest.mark.parametrize("annual_noi", [-15000, 0, 60000])
async def test_filed_return_noi_is_the_same_annual_value_across_current_and_shared_consumers(
    monkeypatch, annual_noi,
):
    source = business_tax(annual_noi)
    untouched = deepcopy(source)
    cockpit = build([source])
    noi = cockpit["noi"]
    assert noi["status"] == "source_supported" and not noi["review_needed"]
    assert noi["status_reason"] and noi["reason_codes"] == []
    assert noi["headline"]["annualized_value"] == annual_noi
    assert noi["headline"]["currency"] == "USD"
    assert str(noi["headline"]["year"]) == "2025"
    assert noi["headline"]["tax_derived_estimate"]
    assert not noi["headline"].get("staff_reviewed")
    assert noi["review"]["current"] is None  # Reading never creates an approval or review.
    # Expensed depreciation stays in NOI, while the existing tax earnings basis is distinct.
    assert cockpit["ebitda"]["headline"]["annualized_value"] == annual_noi + 12000
    assert cockpit["dscr"]["headline"]["numerator"] == annual_noi + 12000

    projected = current_review_projection({"key_metrics": {"noi": 999999, "annual_noi": 999999}}, cockpit)
    metrics = projected["key_metrics"]
    assert metrics["noi"] == metrics["annual_noi"] == annual_noi
    assert metrics["noi_status"] == noi["status"]
    assert metrics["noi_review_needed"] is False
    assert metrics["noi_status_reason"] == noi["status_reason"]
    assert metrics["noi_reason_codes"] == noi["reason_codes"]
    assert metrics["noi_policy_version"] == noi["policy_version"]
    brief = projected["operating_earnings"]["noi"]
    assert brief["authority_stamp"] == noi["authority_stamp"]
    assert brief["historical"]["annualized_value"] == annual_noi
    assert_public_noi(partner_operating_earnings(projected["operating_earnings"])["noi"], noi)

    db, profile, intake = profile_fixture()
    intake.id = profile.intake_id
    original_snapshot = deepcopy(intake.result_snapshot)
    monkeypatch.setattr(financial_cockpit, "load_cockpit", AsyncMock(return_value=cockpit))
    monkeypatch.setattr(financial_version_selection, "accepted_debt_schedule_snapshot", AsyncMock(return_value=None))
    intelligence = await application_profiles.intelligence_state(db, profile)
    metric = next(row for row in intelligence.metrics if row.key == "noi")
    assert metric.value == annual_noi and metric.unit == "USD"
    assert metric.period == noi["headline"]["period"]
    assert metric.calculation_status == "source_supported"
    assert metric.review_needed is False and metric.status == "ready"
    assert metric.status_reason == noi["status_reason"] and metric.action is None
    assert intake.result_snapshot == original_snapshot

    monkeypatch.setattr(application_profiles, "find_profile", AsyncMock(return_value=profile))
    public = await current_intake_report(db, intake, original_snapshot, external=True)
    assert public["key_metrics"]["annual_noi"] == annual_noi
    assert public["key_metrics"]["noi_status"] == "source_supported"
    assert_public_noi(public["operating_earnings"]["noi"], noi)
    assert "financial_calculation_review" not in public
    assert "financial_calculation_authority" not in public
    assert source == untouched


def test_stale_explicit_review_clears_current_and_cached_values_without_reviving_automatic_source():
    sources = [annual_pnl(), business_tax()]
    first = build(sources)
    state = review_state(first, mode="source", selected_file_id="source", annual_value=20000,
                         year=2025, currency="USD")
    changed = deepcopy(sources)
    changed[0]["analysis_id"] = "new-current-pnl-analysis"
    changed[0]["key_facts"].update(total_operating_expenses=370000, operating_income=30000)
    cockpit = build(changed, state=state)
    noi = cockpit["noi"]
    assert noi["source_headline"] is not None  # An automatic value exists but is not a fallback.
    assert noi["status"] == "stale" and noi["review_needed"]
    assert noi["headline"] is None and noi["review"]["current"]["annual_value"] == 20000
    cached = {"key_metrics": {"noi": 999999, "annual_noi": 999999, "noi_status": "reviewed"}}
    original = deepcopy(cached)
    result = current_review_projection(cached, cockpit)
    assert result["key_metrics"]["noi"] is None and result["key_metrics"]["annual_noi"] is None
    assert result["key_metrics"]["noi_status"] == "stale"
    assert result["key_metrics"]["noi_review_needed"] is True
    brief = result["operating_earnings"]["noi"]
    assert brief["historical"] is None and brief["status_reason"] == noi["status_reason"]
    shared = partner_operating_earnings(result["operating_earnings"])["noi"]
    assert_public_noi(shared, noi)
    assert shared["historical"] is None
    lines = "\n".join(_operating_earnings_lines({"operating_earnings": result["operating_earnings"]}))
    assert noi["status_reason"] in lines
    assert "AUDIT_PRIVATE_" not in lines
    assert cached == original


def test_reviewed_noi_has_its_own_year_currency_and_governance_in_operating_and_public_reports():
    source = annual_pnl()
    first = build([source])
    cockpit = build([source], state=review_state(first))
    noi = cockpit["noi"]
    assert noi["status"] == "reviewed" and not noi["review_needed"]
    assert cockpit["ebitda"]["headline"] == first["ebitda"]["headline"]
    assert cockpit["dscr"]["headline"] == first["dscr"]["headline"]
    before, after = {"fact_sources": {}}, {"fact_sources": {}}
    _apply_current_business_coverage(before, first, accepted_bank_months=0)
    _apply_current_business_coverage(after, cockpit, accepted_bank_months=0)
    assert after["cash_flow"] == before["cash_flow"] == 30000
    assert after["dscr"] == before["dscr"] == 2.5
    assert after["fact_sources"]["cash_flow"]["verification_status"] == before["fact_sources"]["cash_flow"]["verification_status"]

    evaluation = earnings_improvement.evaluate(disclosure(), cockpit)
    baseline = evaluation["baseline"]
    assert baseline["noi"] == 42000 and baseline["currency"] == "USD"
    governance = baseline["noi_governance"]
    assert governance["status"] == "reviewed" and not governance["review_needed"]
    assert str(governance["year"]) == "2024" and governance["currency"] == "EUR"
    assert governance["period"] == noi["headline"]["period"]
    assert governance["kind"] == "staff_reviewed_noi"
    package = earnings_improvement.package_summary(evaluation)
    brief = operating_earnings_brief(cockpit)
    brief["improvement_plan"] = package
    public = partner_operating_earnings(brief)
    assert_public_noi(public["noi"], noi)
    assert public["noi"]["historical"]["annualized_value"] == 42000
    assert public["noi"]["historical"]["currency"] == "EUR"
    assert str(public["noi"]["historical"]["year"]) == "2024"
    assert public["improvement_plan"]["baseline"]["noi_governance"]["currency"] == "EUR"
    assert "AUDIT_PRIVATE_" not in json.dumps(public)
    pdf_html = _before_after(package, "USD")
    assert "EUR" in pdf_html and "2024" in pdf_html
    assert "AUDIT_PRIVATE_" not in pdf_html


def test_status_only_change_invalidates_authority_and_operating_context_without_changing_amount():
    original = build([annual_pnl()])
    changed = deepcopy(original)
    changed["noi"].update(
        status="provisional", review_needed=True,
        status_reason="Current operating classifications need review.",
        reason_codes=["source_classification_review"], authority_stamp="f" * 64,
    )
    assert changed["noi"]["headline"] == original["noi"]["headline"]
    assert financial_authority(changed)["signature"] != financial_authority(original)["signature"]
    assert earnings_improvement.context_stamp(changed) != earnings_improvement.context_stamp(original)
    reloaded = deepcopy(original)
    reloaded["generated_at"] = "2099-01-01T00:00:00Z"
    assert financial_authority(reloaded) == financial_authority(original)
    assert earnings_improvement.context_stamp(reloaded) == earnings_improvement.context_stamp(original)


@pytest.mark.parametrize("amount,status", [(12345, "provisional"), (None, "unavailable")])
def test_legacy_partner_snapshot_is_flagged_without_rewriting_history_or_private_audit(amount, status):
    stored = {
        "noi": {
            "historical": {
                "kind": "tax_derived_noi", "label": "Historical tax estimate", "value": amount,
                "annualized_value": amount, "year": "2024", "period": "2024", "currency": "USD",
                "complete": True, "provisional": False,
                "source_refs": [{"file_name": "INVENTORY_PRIVATE_SOURCE.pdf"}],
                "review_receipt": {"reason": "AUDIT_PRIVATE_REVIEW_REASON"},
            },
        },
    }
    original = deepcopy(stored)
    shared = partner_operating_earnings(stored)["noi"]
    assert shared["policy_version"] == "legacy-unversioned"
    assert shared["status"] == status and shared["review_needed"]
    assert shared["reason_codes"] == ["legacy_noi_snapshot"]
    assert "refresh" in shared["status_reason"].lower()
    assert shared["historical"]["annualized_value"] == amount
    assert "AUDIT_PRIVATE_" not in json.dumps(shared)
    assert "INVENTORY_PRIVATE_" not in json.dumps(shared)
    assert "authority_stamp" not in shared
    assert stored == original


@pytest.mark.parametrize("case", ["tax_usd", "tax_eur", "loan_payments_in_expenses"])
def test_saved_and_live_noi_use_identical_illustration_gates(case):
    source = (
        annual_pnl(notable_expense_lines={"bank_loan_payments_ytd": 6000})
        if case == "loan_payments_in_expenses"
        else business_tax(currency="EUR" if case == "tax_eur" else "USD")
    )
    cockpit = build([source], plan={"retire": ["row:debt"], "new_payment_monthly": 300})
    noi = cockpit["noi"]
    # Loan payments inside a P&L's expenses need reconciling; an accepted return stands.
    provisional = case == "loan_payments_in_expenses"
    assert noi["status"] == ("provisional" if provisional else "source_supported") and noi["review_needed"] is provisional
    saved = noi["scenario"]
    live = preview_scenario(noi, cockpit["plan"]["evaluation"])
    for key in (*CANONICAL_FIELDS, "authority_stamp"):
        assert live[key] == saved[key] == noi[key]
    assert live["current"] == saved["current"] == noi["headline"]["annualized_value"]
    assert live["plan"] == saved["plan"]
    assert live["annual_debt_service_plan"] == saved["annual_debt_service_plan"] == 3600
    assert live["after_debt_service_plan"] == saved["after_debt_service_plan"]
    if case == "tax_usd":
        assert saved["after_debt_service_current"] == -27000
        assert saved["after_debt_service_plan"] == -18600
    else:
        assert saved["after_debt_service_current"] is None
        assert saved["after_debt_service_plan"] is None
