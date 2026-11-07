"""Processing savings use one current annual numerator/denominator pair."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services import merchant_processing as processing


def current(**extra):
    return {"financial_metrics_source": "current_financial_cockpit", "dscr_earnings": 100000,
            "annual_debt_service": 80000, "dscr_basis": "tax_return", "dscr_source_refs": [{"file_id": "current-tax"}], **extra}


def test_stale_estimated_income_and_rounded_ratio_cannot_change_current_operand_math():
    metrics = current(estimated_dscr=99, estimated_ebitda_or_cash_flow=1000)
    before = deepcopy(metrics)
    result = processing.pro_forma_dscr(metrics, 10000)
    assert result["dscr_now"] == 1.25 and result["dscr_with_saving"] == 1.38
    assert result["annual_debt_service"] == 80000 and result["earnings_annual"] == 100000
    assert result["income_basis"] == "tax_return"
    assert result["source_refs"] == [{"file_id": "current-tax"}]
    assert metrics == before


@pytest.mark.parametrize("earnings,expected_before,expected_after", [
    (0, 0, 0.12), (-10000, -0.12, 0), (-193701, -2.42, -2.3),
])
def test_zero_and_signed_loss_earnings_remain_usable_without_dividing_by_income(earnings, expected_before, expected_after):
    result = processing.pro_forma_dscr(current(dscr_earnings=earnings), 10000)
    assert result["dscr_now"] == expected_before and result["dscr_with_saving"] == expected_after


@pytest.mark.parametrize("change", [
    {"financial_metrics_source": None}, {"financial_metrics_source": "current_financial_evidence_unavailable"},
    {"dscr_earnings": None}, {"annual_debt_service": None}, {"annual_debt_service": 0},
    {"annual_debt_service": -1}, {"dscr_earnings": float("nan")}, {"dscr_earnings": float("inf")},
    {"annual_debt_service": float("inf")}, {"dscr_earnings": True},
])
def test_missing_invalid_or_unscoped_operands_do_not_resurrect_cached_ratios(change):
    assert processing.pro_forma_dscr(current(estimated_dscr=99, estimated_ebitda_or_cash_flow=999999, **change), 10000) is None


@pytest.mark.parametrize("saving", [None, 0, -1, True, float("inf"), float("nan")])
def test_savings_must_be_known_positive_finite_annual_amount(saving):
    assert processing.pro_forma_dscr(current(), saving) is None


def test_small_valid_current_ratio_is_calculated_from_exact_operands_not_rounded_ratio():
    result = processing.pro_forma_dscr(current(dscr_earnings=1, annual_debt_service=1000000, estimated_dscr=0), 10000)
    assert result["dscr_now"] == 0 and result["dscr_with_saving"] == .01


@pytest.mark.asyncio
async def test_read_helper_loads_current_cockpit_and_never_uses_the_saved_ai_result():
    from app.tests.test_financial_metric_review import build, tax

    profile = SimpleNamespace(id=uuid4(), intake_id=uuid4())
    cockpit = build([tax(ordinary_business_income=-10000, interest=0, depreciation_and_amortization=0)])
    db = SimpleNamespace(get=AsyncMock(return_value=profile))
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=cockpit)) as load:
        result = await processing.current_pro_forma_dscr(db, profile.id, 20000, intake_id=profile.intake_id)
    load.assert_awaited_once_with(db, profile)
    assert result["dscr_now"] == -.83 and result["dscr_with_saving"] == .83
    assert result["earnings_annual"] == -10000 and result["annual_debt_service"] == 12000
    assert not hasattr(db, "flush") and not hasattr(db, "commit")


@pytest.mark.asyncio
async def test_read_helper_cannot_cross_the_offers_intake_or_fall_back_after_source_failure():
    profile = SimpleNamespace(id=uuid4(), intake_id=uuid4())
    db = SimpleNamespace(get=AsyncMock(return_value=profile))
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock(side_effect=RuntimeError("Source unavailable"))) as load:
        assert await processing.current_pro_forma_dscr(db, profile.id, 10000, intake_id=uuid4()) is None
        load.assert_not_awaited()
        assert await processing.current_pro_forma_dscr(db, profile.id, 10000, intake_id=profile.intake_id) is None
        load.assert_awaited_once_with(db, profile)


@pytest.mark.asyncio
async def test_read_helper_does_not_read_financials_without_a_known_saving():
    db = SimpleNamespace(get=AsyncMock())
    assert await processing.current_pro_forma_dscr(db, uuid4(), None) is None
    db.get.assert_not_awaited()
