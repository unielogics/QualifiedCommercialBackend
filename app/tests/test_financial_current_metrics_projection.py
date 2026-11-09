from copy import deepcopy

from app.services.financial_metric_reporting import current_review_projection
from app.tests.test_financial_metric_review import build, pnl, tax


def test_current_projection_replaces_stale_numbers_without_rewriting_historical_result():
    original = {"executive_summary": "Historical review", "key_metrics": {
        "estimated_dscr": 99, "bankable_ebitda": 999999, "annual_debt_service": 1}}
    before = deepcopy(original)
    cockpit = build([tax(ordinary_business_income=-410155, interest=163121,
                         depreciation_and_amortization=53333)])
    result = current_review_projection(original, cockpit)
    metrics = result["key_metrics"]
    assert metrics["estimated_dscr"] == cockpit["dscr"]["headline"]["value"]
    assert metrics["dscr_earnings"] == -193701
    assert metrics["tax_earnings_estimate"] == -193701
    assert metrics["bankable_ebitda"] is None and metrics["annual_ebitda"] is None
    assert metrics["annual_debt_service"] == 12000
    assert result["executive_summary"] == "Historical review" and original == before
    assert result["financial_calculation_review"]["primary_dscr"]["value"] == metrics["estimated_dscr"]


def test_unavailable_live_value_clears_cached_coverage_and_ebitda():
    source = {"key_metrics": {"estimated_dscr": 99, "dscr": 99,
                             "annual_ebitda": 999999, "bankable_ebitda": 999999}}
    result = current_review_projection(source, build([pnl(period_end="2026-05-31")]))
    assert result["key_metrics"]["estimated_dscr"] is None
    assert result["key_metrics"]["annual_ebitda"] is None
    assert result["key_metrics"]["financial_metrics_source"] == "current_financial_cockpit"
    failed = current_review_projection(source, None)
    assert failed["key_metrics"]["dscr"] is None
    assert failed["key_metrics"]["annual_debt_service"] is None
    assert failed["key_metrics"]["financial_metrics_source"] == "current_financial_evidence_unavailable"
    assert source["key_metrics"]["estimated_dscr"] == 99


def test_latest_validated_pl_supplies_primary_coverage_and_annual_earnings():
    cockpit = build([tax(), pnl()])
    result = current_review_projection({}, cockpit)
    metrics = result["key_metrics"]
    assert metrics["bankable_ebitda"] == 52000
    assert metrics["dscr_basis"] == "profit_and_loss"
    assert metrics["dscr_earnings"] == 52000
    assert metrics["estimated_dscr"] == cockpit["dscr"]["headline"]["value"]
    assert result["financial_calculation_review"]["validated_annual"]["ebitda_annual"] == 52000


def test_cross_currency_candidate_cannot_resurrect_coverage_via_current_projection():
    result = current_review_projection({"key_metrics": {"estimated_dscr": 99}}, build([pnl(currency="EUR")]))
    assert result["key_metrics"]["estimated_dscr"] is None
    assert result["key_metrics"]["bankable_ebitda"] is None
