from copy import deepcopy

import pytest

from app.services.financial_earnings_potential import build


def evidence():
    return {"headline": {"annualized_value": -54369, "kind": "tax_estimate", "currency": "USD", "basis": "cash", "period": "2025", "provisional": True},
            "source_selection": {"selected_file_id": "current", "stale": False},
            "revenue_trend": {"periods": [{"file_id": "prior", "revenue": 40884}, {"file_id": "current", "revenue": 604785}],
                              "comparison": {"comparable": True, "direction": "up", "change_pct": 1379.27, "prior_year": "2024", "current_year": "2025"}}}


def test_negative_baseline_has_truthful_targets_without_compounding_old_growth():
    source = evidence()
    original = deepcopy(source)
    result = build(source, {"annual": 210939.12}, {})
    assert source == original
    assert result["baseline"]["margin_pct"] == -8.9898
    assert result["current"]["dscr"] == -0.26
    assert result["operating_break_even"]["improvement_required"] == 54369
    one, target = result["current"]["targets"]
    assert one["earnings_required"] == 210939.12 and one["improvement_required"] == 265308.12
    assert target["earnings_required"] == 263673.9 and target["margin_pct"] == 43.6
    assert result["forecast"] is None


def test_refinance_changes_required_earnings_but_not_source_earnings():
    result = build(evidence(), {"annual": 210939.12}, {"active": True, "debt_service_after_annual": 40000})
    assert result["baseline"]["earnings"] == -54369
    assert result["plan"]["dscr"] == -1.36
    assert result["plan"]["targets"][1]["earnings_required"] == 50000


def test_only_explicit_illustration_can_supply_forecast_dscr():
    source = evidence()
    source["revenue_trend"]["forecast"] = {"status": "illustration", "projected_ebitda": 100000, "projected_revenue": 700000, "ebitda_kind": "reviewed_assumption", "assumptions": ["Reviewed future margin"]}
    result = build(source, {"annual": 210939.12}, {"active": True, "debt_service_after_annual": 50000})
    assert result["forecast"]["dscr_current"] == 0.47 and result["forecast"]["dscr_plan"] == 2
    assert result["current"]["dscr"] == -0.26


@pytest.mark.parametrize("issue", ["stale", "missing_annual", "missing_revenue", "missing_basis", "foreign_currency", "unknown_debt"])
def test_missing_or_incomparable_evidence_does_not_fabricate_coverage(issue):
    source, service = evidence(), {"annual": 210939.12}
    if issue == "stale":
        source["source_selection"]["stale"] = True
    elif issue == "missing_annual":
        source["headline"]["annualized_value"] = None
    elif issue == "missing_revenue":
        source["revenue_trend"]["periods"][-1]["revenue"] = None
    elif issue == "missing_basis":
        source["headline"]["basis"] = None
    elif issue == "foreign_currency":
        source["headline"]["currency"] = "CAD"
    else:
        service["annual"] = None
    result = build(source, service, {})
    assert result["current"] is None
    assert result["forecast"] is None


def test_zero_debt_is_not_an_infinite_or_negative_ratio():
    result = build(evidence(), {"annual": 0}, {"active": True, "debt_service_after_annual": 0})
    assert result["current"]["no_debt_service"] and result["current"]["dscr"] is None
    assert result["current"]["targets"] == []


def test_unknown_replacement_payment_does_not_show_plan_targets():
    result = build(evidence(), {"annual": 210939.12}, {"active": True, "debt_service_after_annual": 0, "coverage_unavailable_reason": "Replacement payment needed"})
    assert result["plan"] is None
