import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services import ach_fee_workflow, funding_programs, payments


def _profile(*, category="general_capital", vertical="main_street", points="2.5"):
    return SimpleNamespace(id=uuid4(), funding_category=category, vertical=vertical, forecast_fee_points=Decimal(points))


def _result(values):
    return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: values), scalar_one_or_none=lambda: values)


@pytest.mark.parametrize("category", ["mca", "mca_refi", "mca_refinance", "merchant_cash_advance_refinance"])
def test_mca_categories_receive_three_percent_cap(category):
    assert asyncio.run(funding_programs.published_qc_fee_cap_for_profile(SimpleNamespace(), _profile(category=category))) == 3.0


def test_legacy_mca_vertical_is_capped_with_noncanonical_funding_category():
    assert asyncio.run(funding_programs.published_qc_fee_cap_for_profile(SimpleNamespace(), _profile(category="refinance", vertical="mca"))) == 3.0


def test_non_mca_file_is_unaffected():
    assert asyncio.run(funding_programs.published_qc_fee_cap_for_profile(SimpleNamespace(), _profile(points="5"))) is None


def test_selected_mca_program_uses_published_terms_and_strictest_cap():
    db = SimpleNamespace(execute=AsyncMock(side_effect=[_result(["revenue_based_financing"]), _result([Decimal("2.75"), Decimal("3")])]))
    assert asyncio.run(funding_programs.published_qc_fee_cap_for_profile(db, _profile())) == 2.75


def test_published_mca_terms_can_never_raise_the_approved_three_percent_ceiling():
    db = SimpleNamespace(execute=AsyncMock(side_effect=[_result(["mca_refinance"]), _result([Decimal("5")])]))
    assert asyncio.run(funding_programs.published_qc_fee_cap_for_profile(db, _profile())) == 3.0


def test_legacy_fee_is_flagged_without_mutating_economics():
    profile = _profile(category="mca_refinance", points="4.25")
    review = asyncio.run(funding_programs.qc_fee_cap_review_for_profile(SimpleNamespace(), profile))
    assert review["review_required"] is True
    assert review["reason"] == "legacy_qc_fee_above_published_cap"
    assert profile.forecast_fee_points == Decimal("4.25")
    assert review["fee_label"] == "QC origination/success fee"
    assert "borrower_apr" in review["separate_from"]
    assert "consulting_fees" in review["separate_from"]


def test_new_fee_above_cap_is_rejected_and_exact_cap_is_allowed():
    profile = _profile(category="mca_refinance")
    with pytest.raises(HTTPException) as caught:
        asyncio.run(funding_programs.enforce_qc_fee_cap_for_profile(SimpleNamespace(), profile, Decimal("3.0001")))
    assert caught.value.status_code == 422
    assert caught.value.detail["maximum"] == 3.0
    assert asyncio.run(funding_programs.enforce_qc_fee_cap_for_profile(SimpleNamespace(), profile, Decimal("3.00")))["review_required"] is False


def test_prepared_legacy_obligation_above_cap_is_blocked_before_release():
    profile = _profile(category="mca_refinance", points="4.25")
    profile.underwriting_accepted_amount = Decimal("100000")
    profile.underwriting_approved_amount = Decimal("100000")
    profile.underwriting_funded_amount = Decimal("100000")
    profile.forecast_consulting_fee = Decimal("0")
    profile.estimated_close_date = None
    obligation = SimpleNamespace(
        id=uuid4(), application_profile_id=profile.id, superseded_at=None, status="prepared",
        accepted_amount=Decimal("100000"), origination_points=Decimal("4.25"),
        origination_fee_cents=425000, consulting_fee_cents=0, gross_fee_cents=425000,
        client_ach_cents=425000, bank_direct_cents=0, external_cents=0, deferred_cents=0, waived_cents=0,
        origination_client_ach_cents=425000, consulting_client_ach_cents=0,
    )
    allocation = SimpleNamespace(
        obligation_id=obligation.id, gross_fee_cents=425000,
        allocation={"client_ach_cents": 425000, "bank_direct_cents": 0, "external_cents": 0, "deferred_cents": 0, "waived_cents": 0},
        origination_client_ach_cents=425000, consulting_client_ach_cents=0,
    )
    db = SimpleNamespace(get=AsyncMock(return_value=profile))
    with (
        patch.object(payments, "_fee_agreement_is_current", AsyncMock(return_value=True)),
        patch.object(payments, "fee_lines_have_current_governing_agreements", AsyncMock(return_value=True)),
        patch.object(payments, "latest_allocation", AsyncMock(return_value=allocation)),
    ):
        blockers = asyncio.run(payments.fee_obligation_snapshot_blockers(db, obligation))
    assert blockers == ["QC origination/success fee exceeds the published program cap; review and replace the fee obligation"]
    assert obligation.origination_points == Decimal("4.25")


def test_fee_agreement_cannot_be_prepared_above_the_mca_cap():
    profile = _profile(category="mca_refinance", points="4")
    with patch.object(payments, "latest_allocation", AsyncMock()) as allocation:
        with pytest.raises(HTTPException) as caught:
            asyncio.run(ach_fee_workflow._agreement_snapshot(
                SimpleNamespace(), profile=profile, agreement_id=uuid4(),
                include_origination_fee=True, include_consulting_fee=False,
                consulting_milestone_confirmed=False, expected_allocation_version=None,
            ))
    assert caught.value.status_code == 422
    allocation.assert_not_awaited()
