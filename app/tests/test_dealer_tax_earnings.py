"""Dealer metrics use the business return, never the owner's personal return."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.dealer_os.models import DealerBusiness, DealerTaxFiling
from app.dealer_os.services import engines

ENTITY = "Diversity Capital Partners LLC"


def filing(**facts):
    return SimpleNamespace(detail={"entity_name": ENTITY, "form_type": "1065",
                                   "tax_year": "calendar year 2025", **facts},
                           year=2025, entity_name=None, form_type=None, revenue_reported=None)


@pytest.mark.parametrize("form", ["1040", "Form 1040 with Schedule C", "Schedule C"])
def test_personal_tax_returns_cannot_supply_business_earnings_even_if_named_for_business(form):
    source = filing(form_type=form, ordinary_business_income=100000, interest=5000, depreciation=10000)
    assert engines._tax_ebitda(source, business_entity=ENTITY) is None


def test_matching_business_aliases_preserve_loss_and_nested_amortization():
    source = filing(ordinary_business_income_loss="(410,155)", interest_expense=163121,
                    other_deductions_detail={"amortization": 53333})
    assert engines._tax_ebitda(source, business_entity="Diversity Capital Partners") == -193701
    # An explicitly combined D&A figure is not added to its components again.
    source.detail["depreciation_and_amortization"] = 60000
    assert engines._tax_ebitda(source, business_entity=ENTITY) == -187034


@pytest.mark.parametrize("changes", [
    {"entity_name": "Other Capital Partners LLC"},
    {"currency": "EUR"},
    {"tax_year": "2025", "period_start": "2025-01-01", "period_end": "2025-06-30"},
    {"tax_year": "2025", "period_start": "2025-01-01"},
])
def test_wrong_entity_currency_and_explicit_short_or_unknown_duration_are_not_annual_usd_earnings(changes):
    source = filing(ordinary_business_income=100000, **changes)
    assert engines._tax_ebitda(source, business_entity=ENTITY) is None


def test_row_metadata_cannot_hide_a_personal_return_and_unknown_business_is_not_guessed():
    source = filing(tax_form="1040", ordinary_business_income=100000)
    source.form_type = "1065"
    assert engines._tax_ebitda(source, business_entity=ENTITY) is None
    assert engines._tax_ebitda(filing(ordinary_business_income=100000)) is None


@pytest.mark.parametrize("source_identity", ["business_name", "taxpayer"])
def test_printed_source_identity_wins_over_old_row_identity(source_identity):
    source = filing(ordinary_business_income=100000)
    source.detail.pop("entity_name")
    source.detail[source_identity] = "Different Person or Company"
    source.entity_name = ENTITY
    assert engines._tax_ebitda(source, business_entity=ENTITY) is None


def test_latest_matching_business_source_wins_over_owner_and_unrelated_returns():
    business = filing(ordinary_business_income=5720, interest=72275, amortization_within_other_deductions=53333)
    sources = [filing(form_type="1040", net_income=900000),
               filing(entity_name="Different Company", ordinary_business_income=800000), business]
    assert engines._latest_business_tax_earnings(sources, business_entity=ENTITY) == 131328
    # The lower-priority older period is not used when the current business has a loss.
    current = filing(ordinary_business_income_loss=-410155, interest_expense=163121, amortization=53333)
    assert engines._latest_business_tax_earnings([current, *sources], business_entity=ENTITY) == -193701


def test_engine_preserves_haircut_and_separate_verified_adjustments_on_tax_estimate():
    result = engines.compute_metrics([], 12000, {},
                                     {"tax_ebitda_annual": -193701, "debt_schedule_monthly": 1000})
    assert result["ebitda"]["reported_ttm"] == -193701
    assert result["ebitda"]["adjusted"] == -181701
    assert result["ebitda"]["bankable"] == -174432.96
    assert result["ebitda"]["basis"] == "tax_earnings_estimate"
    assert result["ebitda"]["provisional"]
    assert result["dscr"]["current"] == round(-174432.96 / 12000, 3)


def test_funding_goal_cannot_price_coverage_with_unknown_existing_debt():
    result = engines.compute_metrics([], 0, {}, {"tax_ebitda_annual": 120000, "goal_monthly_payment": 1000})
    assert result["dscr"]["at_goal"] is None
    known_zero = engines.compute_metrics([], 0, {}, {"tax_ebitda_annual": 120000,
        "goal_monthly_payment": 1000, "debt_schedule_authoritative": True, "debt_schedule_monthly": 0})
    assert known_zero["dscr"]["at_goal"] == 9.6


class Result:
    def __init__(self, rows=(), scalar=None):
        self.rows, self.scalar = rows, scalar

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        return self.scalar


@pytest.mark.asyncio
async def test_loader_considers_current_business_filings_without_requiring_revenue():
    personal = filing(form_type="1040", net_income=900000)
    business = filing(ordinary_business_income_loss=-410155, interest_expense=163121, amortization=53333)
    queries = []

    async def execute(statement):
        entity = statement.column_descriptions[0].get("entity")
        if entity is DealerBusiness:
            return Result(scalar=SimpleNamespace(legal_name=ENTITY, name="Diversity", funding_goal=None))
        if entity is DealerTaxFiling:
            queries.append(str(statement))
            return Result(rows=[personal, business])
        return Result()

    db = SimpleNamespace(execute=AsyncMock(side_effect=execute))
    with patch.object(engines.financial_version_selection, "accepted_debt_schedule_snapshot_for_dealer", AsyncMock(return_value=None)):
        result = await engines.load_metric_inputs(db, uuid4())
    assert result.fallbacks["tax_ebitda_annual"] == -193701
    assert len(queries) == 1 and "is_current_document_version" in queries[0]
    assert "revenue_reported IS NOT NULL" not in queries[0] and "LIMIT" not in queries[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("source_entity,form,allowed", [
    (ENTITY, "1040", False),
    ("Different Company", "1065", False),
    ("Diversity Capital Partners", "1065", True),
])
async def test_tax_detail_writer_does_not_replace_corporate_facts_with_personal_or_unrelated_income(source_entity, form, allowed):
    from app.dealer_os.router import _store_tax_detail

    original = {"entity_name": ENTITY, "ordinary_business_income": 1000}
    row = SimpleNamespace(detail=original, entity_name=ENTITY, form_type="1065", document_id=uuid4())
    new = {"entity_name": source_entity, "form_type": form, "net_income": 900000}
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(legal_name=ENTITY, name="Diversity")),
                         execute=AsyncMock(return_value=Result(scalar=row)), flush=AsyncMock())
    await _store_tax_detail(db, uuid4(), [{"year": 2025}], {"key_facts": new}, document_id=uuid4())
    if allowed:
        assert row.detail == new
        db.flush.assert_awaited_once()
    else:
        assert row.detail is original and row.entity_name == ENTITY and row.form_type == "1065"
        db.execute.assert_not_awaited()
        db.flush.assert_not_awaited()
