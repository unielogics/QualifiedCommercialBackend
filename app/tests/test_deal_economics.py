from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.enums import Role
from app.services.deal_economics import calculate_deal_earnings


def test_earnings_use_accepted_amount_not_approved_or_requested_amount() -> None:
    result = calculate_deal_earnings(
        accepted_amount=Decimal("325000"),
        origination_points=Decimal("2.5"),
        consulting_fee=Decimal("1500"),
    )

    assert result.accepted_amount == Decimal("325000.00")
    assert result.origination_earnings == Decimal("8125.00")
    assert result.consulting_fee == Decimal("1500.00")
    assert result.total == Decimal("9625.00")


def test_fixed_consulting_fee_is_forecastable_before_amount_is_accepted() -> None:
    result = calculate_deal_earnings(
        accepted_amount=None,
        origination_points=2,
        consulting_fee=750,
    )

    assert result.origination_earnings is None
    assert result.total == Decimal("750.00")


def test_no_calculable_component_remains_unknown_but_explicit_zero_is_zero() -> None:
    unknown = calculate_deal_earnings(
        accepted_amount=100_000,
        origination_points=None,
        consulting_fee=None,
    )
    zero = calculate_deal_earnings(
        accepted_amount=100_000,
        origination_points=0,
        consulting_fee=None,
    )

    assert unknown.total is None
    assert zero.total == Decimal("0.00")


@pytest.mark.asyncio
async def test_operator_profile_fee_patch_updates_one_canonical_row_and_preserves_budget_revision():
    from app.routers import operator_files
    from app.schemas.deal_budget import DealCostRow
    from app.services import deal_budget

    points = DealCostRow(id=uuid4(), label="Reviewed points", category="origination", method="percent", value=2).model_dump(mode="json")
    management = DealCostRow(id=uuid4(), label="Management", category="management", payer="investor", timing="monthly", value=100).model_dump(mode="json")
    profile = SimpleNamespace(id=uuid4(), loan_id=None, underwriting_accepted_amount=300000,
        forecast_fee_points=2, forecast_consulting_fee=None, financial_plan={"new_loan_amount": 400000},
        deal_budget={"revision": 7, "costs_confirmed": True, "rows": [points, management],
            "funding_mode": "multiple_investors", "investor_funding_amount": 200000, "minimum_investor_contribution": 50000})
    actor = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), refresh=AsyncMock())
    output = object()
    with (patch.object(operator_files.profiles, "resolve_profile", AsyncMock(return_value=profile)),
          patch.object(operator_files.profiles, "log_profile_action", AsyncMock()),
          patch.object(operator_files, "_all_rows", AsyncMock(return_value=[object()])),
          patch.object(operator_files, "_row_matches_source", return_value=True),
          patch.object(operator_files, "_economics_read", return_value=output),
          patch.object(operator_files.funding_programs, "enforce_qc_fee_cap_for_profile", AsyncMock()),
          patch.object(operator_files.funding_programs, "qc_fee_cap_review_for_profile", AsyncMock(return_value={}))):
        result = await operator_files.update_operator_file_economics("intake", uuid4(),
            operator_files.UnifiedFileEconomicsPatch(origination_fee_points=Decimal("2.1250")), actor, db)
    assert result is output
    budget = profile.deal_budget
    assert budget["revision"] == 8 and budget["costs_confirmed"] is False
    assert budget["investor_funding_amount"] == 200000 and budget["minimum_investor_contribution"] == 50000
    assert len(budget["rows"]) == 2 and budget["rows"][1] == management
    assert budget["rows"][0]["id"] == points["id"] and Decimal(budget["rows"][0]["value"]) == Decimal("2.1250")
    assert profile.forecast_fee_points == Decimal("2.1250")
    # Accepted-deal revenue remains on the accepted amount, not the proposal.
    assert calculate_deal_earnings(accepted_amount=profile.underwriting_accepted_amount,
        origination_points=profile.forecast_fee_points, consulting_fee=None).origination_earnings == Decimal("6375.00")
    assert deal_budget.snapshot(profile)["revision"] == 8
