"""One operating-income selection/review policy, independent of debt and EBITDA."""

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services import financial_noi, financial_noi_review
from app.tests.test_financial_metric_review import ACTOR, build, pnl, tax


def annual_tax(year=2025, **facts):
    row = tax(**{"tax_form": "1120S", "tax_year": str(year), "currency": "USD", "basis": "cash",
        "period_start": f"{year}-01-01", "period_end": f"{year}-12-31", **facts})
    row["file_id"] = f"tax-{year}"
    return row


def annual_pl(year=2025, **facts):
    row = pnl(**{"period_start": f"{year}-01-01", "period_end": f"{year}-12-31",
        "cost_of_goods_sold": 10000, "total_operating_expenses": 150000, "basis": "cash", **facts})
    row["file_id"] = f"pl-{year}"
    return row


def review(rows, *, mode="source", selected="tax-2025", year=2025, amount=105000):
    first = build(rows)
    return financial_noi_review.change(None, first["noi"]["review"], actor=ACTOR, mode=mode,
        selected_file_id=selected, annual_value=amount, year=year, currency="USD", reason="PRIVATE provenance note",
        expected_revision=0, source_stamp=first["noi"]["review"]["source_stamp"], idempotency_key="review-noi-policy-test")[0]


@pytest.mark.parametrize("income,interest,expected", [(-10000, 1000, -9000), (0, 0, 0), (10000, 0, 10000)])
def test_automatic_tax_noi_is_computable_provisional_and_never_adds_da(income, interest, expected):
    result = build([annual_tax(ordinary_business_income=income, interest=interest, depreciation=999999)])["noi"]
    assert result["headline"]["value"] == result["headline"]["annualized_value"] == expected
    assert result["status"] == "provisional" and result["review_needed"]
    assert result["headline"]["kind"] == "tax_derived_noi" and not result["headline"]["staff_reviewed"]
    assert result["headline"]["year"] == "2025" and result["headline"]["currency"] == "USD"
    assert result["policy_version"] == "business-noi-v2" and len(result["authority_stamp"]) == 64
    assert result["review"]["current"] is None


@pytest.mark.parametrize("missing", ["ordinary_business_income", "interest"])
def test_automatic_tax_does_not_invent_missing_operands(missing):
    result = build([annual_tax(**{missing: None})])["noi"]
    assert result["headline"] is None and result["status"] == "unavailable"
    assert result["review_needed"] and result["scenario"]["current"] is None


def test_latest_business_period_wins_and_pnl_is_preferred_within_the_same_period():
    newest_tax = annual_tax()
    result = build([annual_pl(2024), newest_tax])["noi"]
    assert result["headline"]["source_refs"][0]["file_id"] == "tax-2025"
    result = build([annual_pl(), newest_tax])["noi"]
    assert result["headline"]["source_refs"][0]["file_id"] == "pl-2025"
    assert result["status"] == "source_supported" and not result["review_needed"]


def test_incomplete_newer_pl_can_show_dated_older_tax_estimate_with_explicit_fallback_reason():
    result = build([annual_tax(2024), annual_pl(2025, total_operating_expenses=None)])["noi"]
    assert result["headline"]["year"] == "2024" and result["headline"]["annualized_value"] == 105000
    assert result["status"] == "provisional" and result["review_needed"]
    assert "older_tax_fallback" in result["reason_codes"]
    assert "older tax" in result["status_reason"]


def test_unknown_tax_duration_does_not_invent_an_annual_noi():
    row = annual_tax(period_start=None, period_end=None)
    result = build([row])["noi"]
    assert result["headline"]["value"] == 105000
    assert result["headline"]["annualized_value"] is None
    assert result["headline"]["period_start"] is None
    assert result["scenario"]["current"] is None
    assert "annual_period_unconfirmed" in result["reason_codes"]


def test_personal_or_different_business_tax_never_supplies_business_noi():
    for facts in ({"tax_form": "1040"}, {"entity_name": "Different Entity LLC"}):
        result = build([annual_tax(**facts)])["noi"]
        assert result["headline"] is None and result["status"] == "unavailable"


def test_noi_is_independent_of_ebitda_source_exclusions_and_stale_picker():
    rows = [annual_pl()]
    first = financial_noi.build_noi(rows, entity="Example LLC")
    second = financial_noi.build_noi(rows, entity="Example LLC", source_selection={
        "stale": True, "selected_file_id": None, "excluded_file_ids": ["pl-2025"]})
    assert second["headline"]["value"] == first["headline"]["value"]
    assert second["status"] == first["status"] == "source_supported"


def test_explicit_review_resolves_action_without_accepting_tax_or_erasing_source_limitations():
    source = annual_tax()
    source["accepted"] = False
    state = review([source])
    result = build([source], state=state)["noi"]
    assert result["status"] == "reviewed" and not result["review_needed"]
    assert not result["headline"]["source_accepted"] and not source["accepted"]
    assert "source_quotations_unverified" in result["source_limitations"]
    assert "tax_book_and_non_operating_review" in result["source_limitations"]
    assert "PRIVATE" not in result["status_reason"]
    assert result["headline"]["value"] == 105000


def test_pinned_review_ignores_older_sources_and_nonfinancial_metadata_changes():
    rows = [annual_tax()]
    state = review(rows)
    updated = deepcopy(rows)
    updated[0]["file_name"] = "Renamed business return.pdf"
    updated[0]["key_facts"]["preparer_note"] = "Nonfinancial metadata"
    updated += [annual_tax(2024, ordinary_business_income=900000)]
    current = build(updated, state=state)["noi"]
    assert current["status"] == "reviewed" and not current["review"]["stale"]
    assert current["headline"]["value"] == 105000


@pytest.mark.parametrize("change", [
    {"ordinary_business_income": 99999}, {"interest": 4000}, {"currency": "EUR"},
    {"basis": "accrual"}, {"period_end": "2025-11-30"}, {"entity_name": "Another Entity LLC"},
    {"other_income": 9000},
])
def test_relevant_pinned_source_changes_require_review_without_auto_replacement(change):
    state = review([annual_tax()])
    result = build([annual_tax(**change)], state=state)["noi"]
    assert result["status"] == "stale" and result["review_needed"]
    assert result["headline"] is None and result["scenario"]["current"] is None
    assert result["review"]["current"]["annual_value"] == 105000


def test_removed_selected_source_or_newer_business_period_stales_review():
    state = review([annual_tax()])
    for rows in ([], [annual_tax(), annual_pl(2026, period_end="2026-06-30")]):
        result = build(rows, state=state)["noi"]
        assert result["status"] == "stale" and result["headline"] is None
        assert result["review"]["history"][-1]["annual_value"] == 105000


def test_source_free_manual_review_ignores_new_older_documents_but_not_newer_periods_or_entity():
    state = review([], mode="manual", selected=None, amount=-1000)
    result = build([annual_tax(2024)], state=state)["noi"]
    assert result["status"] == "reviewed" and result["headline"]["value"] == -1000
    assert build([annual_tax(2026, period_end="2026-06-30")], state=state)["noi"]["status"] == "stale"
    changed_entity = financial_noi.build_noi([], entity="Changed Entity LLC", review_state=state)
    assert changed_entity["status"] == "stale"


def test_tax_interest_correction_changes_suggestion_and_stales_pinned_review():
    from app.services.financial_metric_review import change

    source = annual_tax()
    state = review([source])
    current = build([source], state=state)
    target = next(item for item in current["metric_review"]["inputs"] if item["key"].endswith(".interest"))
    state, _ = change(state, current["metric_review"], actor=ACTOR, input_key=target["key"], action="correct",
        value=6000, reason="Corrected the extracted tax interest", expected_revision=1,
        source_stamp=target["source_stamp"], idempotency_key="correct-tax-interest")
    result = build([source], state=state)["noi"]
    assert result["review"]["sources"][0]["proposed_annual_value"] == 106000
    assert result["status"] == "stale" and result["headline"] is None


def test_legacy_receipt_retains_original_catalog_guard_until_explicit_review_upgrade():
    rows = [annual_tax()]
    state = review(rows)
    state["events"][-1].pop("source_dependency")
    assert build(rows, state=state)["noi"]["status"] == "reviewed"
    renamed = deepcopy(rows)
    renamed[0]["key_facts"]["preparer_note"] = "Legacy guard cannot prove this field irrelevant"
    result = build(renamed, state=state)["noi"]
    assert result["status"] == "stale" and "legacy evidence catalog" in result["status_reason"]
    assert result["review"]["current"]["annual_value"] == 105000


def test_identical_reads_have_stable_authority_but_status_changes_change_it():
    rows = [annual_tax()]
    first = build(rows)["noi"]
    assert first["authority_stamp"] == build(rows)["noi"]["authority_stamp"]
    current = build(rows, state=review(rows))["noi"]
    assert current["headline"]["value"] == first["headline"]["value"]
    assert current["authority_stamp"] != first["authority_stamp"]
    assert current["scenario"]["status"] == current["status"]


@pytest.mark.parametrize("legacy", [False, True])
def test_noi_receipt_replay_is_bound_to_the_original_reviewer(legacy):
    rows = [annual_tax()]
    state = review(rows)
    if legacy:
        state["events"][-1].pop("source_dependency")
    first = state["events"][-1]
    payload = {key: first[key] for key in ("mode", "selected_file_id", "annual_value", "year", "currency",
        "period_start", "period_end", "reason", "expected_revision", "source_stamp", "idempotency_key")}
    with pytest.raises(HTTPException) as error:
        financial_noi_review.change(state, build(rows, state=state)["noi"]["review"],
            actor=SimpleNamespace(id=uuid4(), role=ACTOR.role), **payload)
    assert error.value.status_code == 409
    assert state["events"][-1]["actor_id"] == str(ACTOR.id)


def test_legacy_catalog_stamps_match_pre_policy_release_without_customer_rewrites():
    # Golden stamps from the previous production implementation, on these
    # synthetic fixed sources. A policy upgrade must not mass-stale receipts.
    t = tax(tax_form="1120S", currency="USD", basis="cash", period_start="2025-01-01", period_end="2025-12-31")
    p = pnl(period_start="2025-01-01", period_end="2025-12-31", cost_of_goods_sold=10000,
        total_operating_expenses=150000, basis="cash")
    assert build([t])["noi"]["review"]["source_stamp"] == "1da22da685e5a1edb0f54973c560f0e2a9f7cd75bc12ea439e75b239803dbb93"
    assert build([t, p])["noi"]["review"]["source_stamp"] == "b16b1a47314f14cb1cfb68fca7a562f48654eda3c97ad722d9bbf45b6c051eca"


def test_live_projection_reuses_saved_adjustment_and_unavailable_debt_guards():
    rows = [annual_pl()]
    adjustment = {"id": "reviewed-rent", "kind": "rent_to_owner", "label": "Rent reduction",
        "source": "Lease", "note": "Staff hypothesis", "status": "hypothetical_unverified", "amount_annual": 5000}
    items = [adjustment, deepcopy(adjustment), {**adjustment, "id": "missing-note", "note": ""}]
    noi = financial_noi.build_noi(rows, entity="Example LLC", plan_annual_debt_service=12000,
        operating_adjustments=items)
    live = financial_noi.preview_scenario(noi, {"active": True, "debt_service_after_annual": 12000,
        "ebitda": {"adjusted_annual": {"adjustments": items}}})
    assert live["plan"] == noi["scenario"]["plan"] == 45000
    assert live["operating_adjustments"] == noi["scenario"]["operating_adjustments"]
    blocked = financial_noi.preview_scenario(noi, {"active": True, "debt_service_after_annual": 12000,
        "coverage_unavailable_reason": "Unresolved replacement loan payment"})
    assert blocked["annual_debt_service_plan"] is None and blocked["after_debt_service_plan"] is None


@pytest.mark.parametrize("source", [annual_pl(2027), annual_tax(2027), annual_pl(is_forecast=True),
                                   annual_tax(statement_type="projection"), annual_pl(statement_type="Projected profit and loss")])
def test_future_and_forecast_sources_are_reviewable_but_not_automatic_current_noi(source):
    noi = build([source], now=datetime(2026, 11, 10, tzinfo=UTC))["noi"]
    assert noi["headline"] is None and noi["status"] == "unavailable" and noi["review_needed"]
    assert noi["reason_codes"] == ["forecast_or_future_source"]
    assert "completed historical P&L" in noi["status_reason"]
    assert noi["review"]["sources"][0]["eligible"]
    assert not noi["review"]["sources"][0]["automatic_eligible"]


def test_newer_forecasts_do_not_displace_actual_noi_or_stale_current_pinned_review():
    actual = annual_tax()
    state = review([actual])
    rows = [actual, annual_pl(2027), annual_tax(2028)]
    now = datetime(2026, 11, 10, tzinfo=UTC)
    automatic = build(rows, now=now)["noi"]
    assert automatic["headline"]["year"] == "2025"
    pinned = build(rows, state=state, now=now)["noi"]
    assert pinned["status"] == "reviewed" and not pinned["review_needed"]


def test_changed_legacy_catalog_requires_review_without_losing_amount_or_entering_forecast_in_actual_trend():
    actual = annual_pl()
    state = review([actual], selected="pl-2025", amount=40000)
    state["events"][-1].pop("source_dependency")
    rows = [actual, annual_pl(2026, gross_revenue=900000)]
    noi = build(rows, state=state, now=datetime(2026, 11, 10, tzinfo=UTC))["noi"]
    assert noi["status"] == "stale" and noi["review"]["stale"]
    assert noi["review"]["current"]["annual_value"] == 40000
    assert "legacy evidence catalog" in noi["status_reason"]
    assert [row["year"] for row in noi["years"]] == ["2025"]
    assert not noi["trend"]["comparable"]


def test_explicit_future_source_review_keeps_assumption_provenance():
    source = annual_pl(2027)
    state = review([source], selected="pl-2027", year=2027, amount=40000)
    noi = build([source], state=state, now=datetime(2026, 11, 10, tzinfo=UTC))["noi"]
    assert noi["status"] == "reviewed" and not noi["review_needed"]
    assert noi["headline"]["forecast_assumption"] and "assumption" in noi["headline"]["label"]
    assert "forecast_or_future_reporting_assumption" in noi["source_limitations"]
    assert "not historical actual income" in noi["status_reason"]


def test_offsetting_pl_fundamental_changes_stale_pinned_review_even_when_net_noi_is_identical():
    source = annual_pl()
    state = review([source], selected="pl-2025", amount=40000)
    changed = annual_pl(gross_revenue=210000, cost_of_goods_sold=20000)
    assert build([source])["noi"]["headline"]["value"] == build([changed])["noi"]["headline"]["value"] == 40000
    noi = build([changed], state=state)["noi"]
    assert noi["status"] == "stale" and noi["headline"] is None
