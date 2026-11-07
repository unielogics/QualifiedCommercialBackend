"""Unknown retained debt cannot become a zero-denominator funding assumption."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services import application_terms as terms
from app.services import financial_cockpit as cockpit
from app.services import offer_suggestions as offers


def fixture(existing, *, effective=None):
    candidate = {"key": "profit_and_loss", "label": "P&L", "numerator": 120000,
                 "value": None, "numerator_label": "Annual EBITDA"}
    return {"dscr": {"headline": None, "candidates": [candidate],
                     "annual_debt_service": existing * 12 if existing is not None else None},
            "debt": {"service": {"monthly": existing, "source": "none"}},
            "plan": {"record": {}, "evaluation": {"active": effective is not None,
                     "debt_service_before_new_payment_monthly": effective}, "modifications": []}}


@pytest.mark.asyncio
@pytest.mark.parametrize("existing,effective,expected", [(None, None, None), (0, None, 0), (1000, 0, 0), (None, 2000, 24000)])
async def test_client_terms_preserve_unknown_zero_and_explicit_debt_assumptions(existing, effective, expected):
    with patch.object(cockpit, "load_cockpit", AsyncMock(return_value=fixture(existing, effective=effective))):
        context = await terms._cockpit_context(None, SimpleNamespace())
    assert context.current_annual_debt_service == expected
    calculation = terms._calculation(context=context, payment=1000, payment_count=12,
                                    payments_per_year=12, new_annual_debt_service=12000,
                                    amount=12000, total_repayment=12000)
    if expected is None:
        assert calculation.dscr_after is None and calculation.projected_annual_debt_service is None
        assert "current annual debt service" in calculation.dscr_explanation
    else:
        assert calculation.dscr_after == round(120000 / (expected + 12000), 3)


@pytest.mark.asyncio
async def test_offer_reader_does_not_replace_unknown_service_with_zero():
    with (patch.object(cockpit, "load_cockpit", AsyncMock(return_value=fixture(None))),
          patch.object(offers.programs, "active_selections", AsyncMock(return_value=[])),
          patch.object(offers.programs, "published_candidates", AsyncMock(return_value=[]))):
        result = await offers.suggest_offers(None, SimpleNamespace(id=uuid4()))
    assert result["existing_debt_service_monthly"] is None


def test_offer_sizing_and_requested_coverage_require_known_existing_debt():
    assert offers.capacity_monthly(120000, None) is None
    result = offers.suggest_product(program_key="term", label="Term loan", income_annual=120000,
                                    existing_monthly_debt_service=None, requested_amount=50000,
                                    shape=offers.program_shape(None))
    assert result["requested_monthly_payment"] is not None
    for key in ("amount", "affordable_amount", "room_monthly", "dscr", "requested_dscr"):
        assert result[key] is None
    assert "Existing debt service is unknown" in result["reason"]
    assert offers.capacity_monthly(120000, 0) == 8000


@pytest.mark.parametrize("annual,expected", [(None, None), (0, 10), (12000, 5)])
def test_pro_forma_coverage_does_not_assume_existing_debt_is_zero(annual, expected):
    result = cockpit._dscr_candidate("profit_and_loss", "P&L", numerator=120000,
                                    numerator_label="Annual EBITDA", period="2025",
                                    annual_debt_service=annual, proposed_annual=12000, confidence="statement")
    assert result["pro_forma_value"] == expected


@pytest.mark.parametrize("annual,retained,known", [(None, None, False), (0, None, True), (None, 0, True)])
def test_proposed_schedule_risk_uses_only_known_existing_or_retained_debt(annual, retained, known):
    source = fixture(None)
    source["dscr"].update(annual_debt_service=annual, headline={"numerator": 120000},
                          proposed={"amount": 100000, "rate_pct": 12, "term_months": 120,
                                    "treatment": "refinance" if retained is not None else "additive",
                                    "retained_annual_debt_service": retained})
    result = cockpit.proposed_terms_block(source)
    assert result["monthly_payment"] is not None and result["schedule_rows"]
    assert (result["risk"] is not None) is known
