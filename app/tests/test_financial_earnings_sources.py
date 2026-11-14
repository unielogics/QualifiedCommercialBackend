import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.enums import Role
from app.routers.financial_metrics import FinancialEarningsSources, FinancialRevenueAssumptions
from app.services import financial_cockpit
from app.services.financial_earnings_sources import align_coverage, attach, change
from app.tests.test_financial_metric_review import ACTOR, ENTITY, build, correction, file, pnl


def business(year, revenue, income, **facts):
    return file("tax_return", {"entity": ENTITY, "tax_form": "1120-S", "tax_year": str(year),
        "period_start": f"{year}-01-01", "period_end": f"{year}-12-31", "gross_receipts": revenue,
        "ordinary_business_income": income, "interest_expense": 170, "accounting_basis": "Cash", **facts}, key=f"tax-{year}")


def selection(cockpit):
    return cockpit["ebitda"]["source_selection"]


def save(cockpit, state=None, **extra):
    return change(state, selection(cockpit), mode="manual", selected_file_id=selection(cockpit)["selected_file_id"], excluded_file_ids=["tax-2024", "personal"],
        reason="Use the latest business return, retain earlier comparison", expected_revision=(state or {}).get("revision", 0),
        source_stamp=selection(cockpit)["source_stamp"], idempotency_key="source-review-1", actor=ACTOR, **extra)


def sources():
    return [business(2024, 40884, -69267, interest_expense=301, amortization=13156),
            business(2025, 604785, -67695, amortization_health_record_soft=13156, accumulated_amortization_eoy=39469),
            file("tax_return", {"filers": "Synthetic individual", "filing_status": "Married Filing Jointly", "agi": 53905,
                                "tax_year": "2025", "body_good_studio_llc_k1_nonpassive_loss": -67695}, key="personal")]


def test_entity_alias_and_current_amortization_fix_latest_business_estimate_without_household_income():
    current = build(sources(), now=datetime(2026, 11, 9, tzinfo=UTC))
    assert len(current["tax"]["years"]) == 2
    assert current["ebitda"]["headline"]["value"] == -54369
    assert current["ebitda"]["headline"]["kind"] == "tax_estimate"
    # An accepted return of this business is a source in its own right.
    assert current["ebitda"]["headline"]["provisional"] is False
    assert current["dscr"]["headline"]["numerator"] == -54369
    selected = selection(current)
    assert selected["selected_file_id"] == "tax-2025"
    assert [row["file_id"] for row in selected["sources"] if row["selected"]] == ["tax-2025"]
    assert next(row for row in selected["sources"] if row["file_id"] == "personal")["eligible"] is False
    assert all(ref["file_id"] != "personal" for ref in current["ebitda"]["headline"]["source_refs"])
    assert next(row for row in current["metric_review"]["inputs"] if row["key"].endswith(":2025.depreciation"))["source_refs"][0]["fact_key"] == "amortization_health_record_soft"


def test_calendar_year_labels_use_normalized_tax_year_for_latest_source_and_coverage():
    rows = sources()
    rows[0]["key_facts"]["tax_year"] = "Calendar year 2024"
    rows[1]["key_facts"]["tax_year"] = "Calendar year 2025"
    current = build(rows)
    assert selection(current)["selected_file_id"] == "tax-2025"
    assert next(row for row in selection(current)["sources"] if row["selected"])["tax_year"] == "2025"
    assert current["dscr"]["headline"]["numerator"] == -54369
    assert current["dscr"]["headline"]["period"] == "2025"


def test_reasoned_manual_sources_preserve_history_and_revenue_comparison_not_add_annual_returns():
    original = build(sources())
    state, changed = save(original)
    assert changed and state["revision"] == 1
    current = build(sources(), state=state)
    assert current["ebitda"]["headline"]["value"] == -54369
    assert selection(current)["excluded_file_ids"] == ["personal", "tax-2024"]
    assert len(selection(current)["history"]) == 1
    assert [row["year"] for row in current["ebitda"]["revenue_trend"]["periods"]] == ["2024", "2025"]


def test_retry_is_idempotent_and_different_payload_rejected():
    original = build(sources())
    payload = dict(mode="manual", selected_file_id="tax-2025", excluded_file_ids=[], reason="Reviewed business source",
                   expected_revision=0, source_stamp=selection(original)["source_stamp"], idempotency_key="receipt-1", actor=ACTOR)
    state, _ = change(None, selection(original), **payload)
    assert change(state, selection(original), **payload) == (state, False)
    with pytest.raises(HTTPException) as error:
        change(state, selection(original), **{**payload, "reason": "Changed reason"})
    assert error.value.status_code == 409


@pytest.mark.parametrize("selected,excluded", [("personal", []), ("other-profile-file", []), ("tax-2025", ["tax-2025"])])
def test_personal_unowned_and_excluded_cannot_be_selected(selected, excluded):
    c = build(sources())
    with pytest.raises(HTTPException) as error:
        change(None, selection(c), mode="manual", selected_file_id=selected, excluded_file_ids=excluded,
               reason="Reviewed source", expected_revision=0, source_stamp=selection(c)["source_stamp"], idempotency_key="receipt-1", actor=ACTOR)
    assert error.value.status_code == 422


def test_source_replacement_makes_choice_stale_instead_of_silently_reincluding_excluded_return():
    original = build(sources())
    state, _ = save(original)
    replacement = deepcopy(sources())
    replacement[1]["analysis_id"] = "replacement-reading"
    current = build(replacement, state=state)
    assert selection(current)["stale"] is True
    assert current["ebitda"]["headline"] is None
    assert "changed" in current["ebitda"]["reason"]
    with pytest.raises(HTTPException) as error:
        change(None, selection(current), mode="manual", selected_file_id="tax-2025", excluded_file_ids=[], reason="Reviewed source",
               expected_revision=0, source_stamp="0" * 64, idempotency_key="receipt-1", actor=ACTOR)
    assert error.value.status_code == 409


def test_manual_no_source_removes_earnings_and_dscr_baselines_without_deleting_documents():
    c = build(sources())
    state, _ = change(None, selection(c), mode="manual", selected_file_id=None, excluded_file_ids=[], reason="Awaiting reconciled return",
                      expected_revision=0, source_stamp=selection(c)["source_stamp"], idempotency_key="receipt-1", actor=ACTOR)
    current = build(sources(), state=state)
    assert current["ebitda"]["headline"] is None
    assert len(current["ebitda"]["source_selection"]["sources"]) == 3
    assert current["dscr"]["headline"] is None
    assert current["dscr"]["comparative_candidates"][0]["value"] == c["dscr"]["headline"]["value"]


def test_current_year_outlook_is_not_invented_from_explosive_growth_or_bank_deposits():
    c = build(sources(), now=datetime(2026, 11, 9, tzinfo=UTC))
    trend = c["ebitda"]["revenue_trend"]
    assert trend["comparison"]["current_revenue"] == 604785
    assert trend["comparison"]["change_amount"] == 563901
    assert trend["comparison"]["change_pct"] == 1379.27
    assert trend["comparison"]["direction"] == "up"
    assert trend["direction"] == "up"
    assert trend["forecast"]["status"] == "unavailable"
    assert trend["forecast"]["projected_revenue"] is None
    assert "Transfers" in trend["bank_context"]["note"]


def test_explicit_growth_and_negative_margin_forecast_remain_separate_from_historical_earnings():
    original = build(sources(), now=datetime(2026, 11, 9, tzinfo=UTC))
    state, _ = save(original, forecast_assumptions={"revenue_growth_pct": 10, "ebitda_margin_pct": None})
    c = build(sources(), state=state, now=datetime(2026, 11, 9, tzinfo=UTC))
    forecast = c["ebitda"]["revenue_trend"]["forecast"]
    assert forecast["status"] == "illustration" and forecast["method"] == "reviewed_assumptions"
    assert forecast["projected_revenue"] == 665263.5
    assert forecast["projected_ebitda"] == -59805.9
    assert c["ebitda"]["headline"]["value"] == -54369
    assert c["dscr"]["headline"]["numerator"] == -54369
    assert "assumption" in forecast["assumptions"][0]


def test_dated_comparable_current_p_and_l_uses_run_rate_with_explicit_seasonality_caveat():
    pl = pnl(basis="cash")
    c = build([*sources(), pl], now=datetime(2026, 11, 9, tzinfo=UTC))
    forecast = c["ebitda"]["revenue_trend"]["forecast"]
    assert forecast["method"] == "p_and_l_run_rate"
    assert forecast["projected_revenue"] == 400000
    assert forecast["projected_ebitda"] == 52000
    assert "seasonal" in forecast["assumptions"][0]


def test_filed_return_leads_a_current_p_and_l_and_an_older_return_cannot_be_selected():
    rows = [*sources(), pnl(basis="cash")]
    original = build(rows, now=datetime(2026, 11, 9, tzinfo=UTC))
    assert selection(original)["selected_file_id"] == "tax-2025"
    assert selection(original)["baseline_policy"] == "filed_return_first"
    fallback = next(row for row in selection(original)["sources"] if row["file_id"] == "source")
    assert fallback["fallback_only"] and fallback["baseline_eligible"] and not fallback["selected"]
    with pytest.raises(HTTPException) as error:
        change(None, selection(original), mode="manual", selected_file_id="tax-2024", excluded_file_ids=[],
               reason="Try the prior tax period", expected_revision=0, source_stamp=selection(original)["source_stamp"],
               idempotency_key="source-review-1", actor=ACTOR)
    assert error.value.status_code == 422
    assert "Older periods are historical comparison only" in error.value.detail
    # Staff may still put the current P&L in the return's place.
    state, _ = change(None, selection(original), mode="manual", selected_file_id="source", excluded_file_ids=[],
        reason="Use the current P&L", expected_revision=0, source_stamp=selection(original)["source_stamp"],
        idempotency_key="source-review-2", actor=ACTOR)
    chosen = build(rows, state=state, now=datetime(2026, 11, 9, tzinfo=UTC))
    assert selection(chosen)["selected_file_id"] == "source" and chosen["ebitda"]["headline"]["value"] == 26000
    assert chosen["dscr"]["headline"]["key"] == "profit_and_loss"


def test_explicit_assumptions_replace_current_p_and_l_illustration_consistently():
    original = build([*sources(), pnl(basis="cash")], now=datetime(2026, 11, 9, tzinfo=UTC))
    # Staff put the current P&L in the return's place; assumptions start from it.
    state, _ = change(None, selection(original), mode="manual", selected_file_id="source", excluded_file_ids=[],
        reason="Use the current P&L with reviewed assumptions", expected_revision=0, source_stamp=selection(original)["source_stamp"],
        idempotency_key="source-review-1", actor=ACTOR, forecast_assumptions={"revenue_growth_pct": 10, "ebitda_margin_pct": 5})
    current = build([*sources(), pnl(basis="cash")], state=state, now=datetime(2026, 11, 9, tzinfo=UTC))
    forecast = current["ebitda"]["revenue_trend"]["forecast"]
    assert forecast["method"] == "reviewed_assumptions"
    assert forecast["projected_revenue"] == 440000
    assert forecast["projected_ebitda"] == 22000
    assert forecast["margin"] == 0.05
    assert forecast["ebitda_kind"] == "reviewed_assumption"


def test_excluded_current_p_and_l_never_supplies_projected_ebitda():
    original = build([*sources(), pnl(basis="cash")], now=datetime(2026, 11, 9, tzinfo=UTC))
    state, _ = change(None, selection(original), mode="manual", selected_file_id=None, excluded_file_ids=["source"],
                      reason="P&L earnings awaiting reconciliation", expected_revision=0, source_stamp=selection(original)["source_stamp"],
                      idempotency_key="source-review-1", actor=ACTOR)
    current = build([*sources(), pnl(basis="cash")], state=state, now=datetime(2026, 11, 9, tzinfo=UTC))
    assert current["ebitda"]["revenue_trend"]["forecast"]["projected_revenue"] is None
    assert current["ebitda"]["revenue_trend"]["forecast"]["projected_ebitda"] is None


def test_unrelated_current_p_and_l_never_supplies_current_year_forecast():
    unrelated = pnl(basis="cash", business_name="Other Business LLC")
    forecast = build([*sources(), unrelated], now=datetime(2026, 11, 9, tzinfo=UTC))["ebitda"]["revenue_trend"]["forecast"]
    assert forecast["status"] == "unavailable"
    assert forecast["projected_revenue"] is None


def test_automatic_source_uses_actual_dates_not_human_period_label_order():
    old_pnl = pnl(period_start="2024-01-01", period_end="2024-06-30")
    c = build([*sources(), old_pnl])
    assert selection(c)["selected_file_id"] == "tax-2025"
    current_pnl = pnl(period_start="2026-01-01", period_end="2026-06-30")
    c = build([*sources(), current_pnl])
    # A newer P&L is the labelled fallback; the latest filed return still leads.
    assert selection(c)["selected_file_id"] == "tax-2025"
    assert next(row for row in selection(c)["sources"] if row["file_id"] == "source")["fallback_only"]
    # Without a return, the P&L with the latest actual dates leads.
    older = pnl(period_start="2025-01-01", period_end="2025-06-30")
    older.update(file_id="pl-older", file_name="Later label, earlier period.pdf")
    c = build([older, current_pnl])
    assert selection(c)["selected_file_id"] == "source"


def test_legacy_manual_prior_year_receipt_reads_latest_without_reusing_older_forecast():
    original = build(sources(), now=datetime(2026, 11, 9, tzinfo=UTC))
    state, _ = save(original, forecast_assumptions={"revenue_growth_pct": 10, "ebitda_margin_pct": 5})
    # Simulate an existing receipt created before the latest-period policy.
    state["events"][0].update(selected_file_id="tax-2024", excluded_file_ids=["personal"])
    before = deepcopy(state)
    current = build(sources(), state=state, now=datetime(2026, 11, 9, tzinfo=UTC))
    assert selection(current)["selected_file_id"] == "tax-2025"
    assert selection(current)["legacy_selection_replaced"]
    assert selection(current)["forecast_assumptions"] is None
    assert current["ebitda"]["headline"]["value"] == -54369
    assert current["ebitda"]["revenue_trend"]["forecast"]["status"] == "unavailable"
    assert state == before  # Projection does not rewrite the immutable receipt.


def test_newer_business_period_invalidates_old_source_receipt_until_current_review():
    original = build(sources())
    state, _ = save(original)
    rows = [*sources(), pnl(basis="cash")]
    pending = build(rows, state=state)
    assert selection(pending)["latest_file_id"] == "tax-2025"
    assert selection(pending)["stale"] and pending["ebitda"]["headline"] is None
    assert pending["dscr"]["headline"] is None
    with pytest.raises(HTTPException) as error:
        change(state, selection(pending), mode="automatic", selected_file_id=None, excluded_file_ids=[],
            reason="Refresh current reporting period", expected_revision=0, source_stamp=selection(pending)["source_stamp"],
            idempotency_key="source-review-2", actor=ACTOR)
    assert error.value.status_code == 409
    accepted, _ = change(state, selection(pending), mode="automatic", selected_file_id=None, excluded_file_ids=[],
        reason="Reviewed the new current P&L", expected_revision=1, source_stamp=selection(pending)["source_stamp"],
        idempotency_key="source-review-2", actor=ACTOR)
    current = build(rows, state=accepted)
    assert not selection(current)["stale"]
    assert current["ebitda"]["headline"]["value"] == -54369
    assert current["dscr"]["headline"]["key"] == "tax_return"


def test_filed_return_baseline_keeps_the_current_partial_pl_as_the_outlook_beside_it():
    current = build([*sources(), pnl(basis="cash")], now=datetime(2026, 11, 9, tzinfo=UTC))
    trend = current["ebitda"]["revenue_trend"]
    assert selection(current)["selected_file_id"] == "tax-2025"
    assert current["ebitda"]["headline"]["value"] == -54369
    assert current["dscr"]["headline"]["numerator"] == -54369
    assert [row["year"] for row in trend["periods"]] == ["2024", "2025"]
    assert trend["comparison"]["prior_year"] == "2024" and trend["comparison"]["current_year"] == "2025"
    assert trend["direction"] == "up"
    assert trend["forecast"]["projected_revenue"] == 400000
    assert trend["forecast"]["projected_ebitda"] == 52000
    assert {ref["file_id"] for ref in trend["forecast"]["source_refs"]} == {"source"}


def test_two_full_annual_pls_provide_revenue_trend_without_adding_old_earnings():
    prior = pnl(period_start="2024-01-01", period_end="2024-12-31", gross_revenue=100000)
    prior.update(file_id="pl-2024", file_name="2024 P&L.pdf")
    latest = pnl(period_start="2025-01-01", period_end="2025-12-31", gross_revenue=150000)
    current = build([prior, latest])
    trend = current["ebitda"]["revenue_trend"]
    assert trend["comparison"]["prior_revenue"] == 100000
    assert trend["comparison"]["current_revenue"] == 150000
    assert trend["comparison"]["change_pct"] == 50 and trend["direction"] == "up"
    assert trend["periods"][0]["source_refs"][0]["file_id"] == "pl-2024"
    assert current["ebitda"]["headline"]["value"] == 26000
    assert {ref["file_id"] for ref in current["ebitda"]["headline"]["source_refs"]} == {"source"}


def test_prior_annual_pl_keeps_its_own_ebitda_as_comparison_while_latest_is_baseline():
    prior = pnl(period_start="2024-01-01", period_end="2024-12-31", net_income=29000)
    prior.update(file_id="pl-2024", file_name="2024 P&L.pdf")
    latest = pnl(period_start="2025-01-01", period_end="2025-12-31", net_income=44000)
    current = build([prior, latest])
    periods = current["ebitda"]["revenue_trend"]["periods"]
    assert [(row["year"], row["ebitda"], row["ebitda_kind"]) for row in periods] == [
        ("2024", 35000, "computed_ebitda"), ("2025", 50000, "computed_ebitda")]
    assert current["ebitda"]["headline"]["value"] == 50000
    state = correction(current, value=None, mark_unknown=True)
    unknown = build([prior, latest], state=state)
    assert unknown["ebitda"]["headline"] is None
    assert [row["ebitda"] for row in unknown["ebitda"]["revenue_trend"]["periods"]] == [35000, None]
    assert unknown["dscr"]["headline"] is None


def test_other_business_prior_annual_pl_never_supplies_revenue_comparison():
    prior = pnl(period_start="2024-01-01", period_end="2024-12-31", business_name="Unrelated LLC", gross_revenue=1)
    prior.update(file_id="other-pl-2024", file_name="Other business P&L.pdf")
    latest = pnl(period_start="2025-01-01", period_end="2025-12-31", gross_revenue=150000)
    current = build([prior, latest])
    trend = current["ebitda"]["revenue_trend"]
    assert trend["comparison"] is None and trend["direction"] is None
    assert [row["file_id"] for row in trend["periods"]] == ["source"]
    assert selection(current)["selected_file_id"] == "source"


def test_matching_annual_pl_history_takes_one_observation_per_period_when_tax_also_exists():
    prior = pnl(period_start="2024-01-01", period_end="2024-12-31", basis="cash", gross_revenue=100000)
    prior.update(file_id="pl-2024", file_name="2024 P&L.pdf")
    latest = pnl(period_start="2025-01-01", period_end="2025-12-31", basis="cash", gross_revenue=200000)
    current = build([*sources(), prior, latest])
    trend = current["ebitda"]["revenue_trend"]
    # The returns lead, so each year is observed once, from its return.
    assert selection(current)["selected_file_id"] == "tax-2025"
    assert [row["file_id"] for row in trend["periods"]] == ["tax-2024", "tax-2025"]
    assert trend["comparison"]["prior_revenue"] == 40884
    assert trend["comparison"]["current_revenue"] == 604785


def test_excluding_latest_period_never_falls_back_to_previous_year():
    original = build(sources())
    state, _ = change(None, selection(original), mode="automatic", selected_file_id=None,
        excluded_file_ids=["tax-2025"], reason="Latest return needs reconciliation", expected_revision=0,
        source_stamp=selection(original)["source_stamp"], idempotency_key="source-review-1", actor=ACTOR)
    current = build(sources(), state=state)
    assert current["ebitda"]["headline"] is None
    assert selection(current)["selected_file_id"] is None
    assert selection(current)["latest_file_id"] == "tax-2025"
    assert "older year is not substituted" in current["ebitda"]["reason"]
    older = next(row for row in selection(current)["sources"] if row["file_id"] == "tax-2024")
    assert older["eligible"] and older["comparative_only"] and not older["baseline_eligible"]
    assert current["ebitda"]["revenue_trend"]["comparison"]["direction"] == "up"


def test_latest_missing_income_is_unavailable_instead_of_using_previous_year():
    rows = sources()
    rows[1]["key_facts"]["ordinary_business_income"] = None
    current = build(rows)
    assert current["ebitda"]["headline"] is None
    assert selection(current)["latest_file_id"] == "tax-2025"
    assert not next(row for row in selection(current)["sources"] if row["file_id"] == "tax-2024")["selected"]
    assert "older" in current["ebitda"]["reason"]


def test_unread_newer_p_and_l_does_not_displace_the_filed_return_but_blocks_without_one():
    unread = file("current_p_and_l", {"business_name": ENTITY, "currency": "USD", "basis": "cash",
        "period_start": "2026-01-01", "period_end": "2026-06-30"}, key="unread-current")
    current = build(sources())
    attach(current["ebitda"], current["metric_review"], [*sources(), unread], current["tax"]["years"], None, {}, entity=ENTITY)
    assert current["ebitda"]["headline"]["value"] == -54369
    assert selection(current)["selected_file_id"] == selection(current)["latest_file_id"] == "tax-2025"
    # With no return, an unread latest P&L still never revives an older one.
    older = pnl(period_start="2025-01-01", period_end="2025-06-30", basis="cash")
    alone = build([older])
    attach(alone["ebitda"], alone["metric_review"], [older, unread], [], alone["profit_and_loss"], {}, entity=ENTITY)
    assert alone["ebitda"]["headline"] is None and selection(alone)["selected_file_id"] is None
    assert selection(alone)["latest_file_id"] == "unread-current"


@pytest.mark.parametrize("stale", [False, True])
def test_denied_or_stale_latest_same_business_return_never_restores_older_earnings(stale):
    from app.services import document_associations

    rows = sources()
    rows[1]["content_hash"] = "a" * 64
    latest = rows[1]
    association = document_associations.record_for_source(None, ENTITY, latest, latest)
    state, _ = document_associations.change(None, {latest["file_id"]: association}, file_id=latest["file_id"],
        action="deny", reason="Review the exact current return", expected_revision=0, source_stamp=association["source_stamp"],
        idempotency_key="deny-current-business", actor=ACTOR)
    if stale:
        latest["analysis_id"] = "updated-current-reading"
    current = build(rows, state=state)
    assert current["ebitda"]["headline"] is None
    assert current["dscr"]["headline"] is None
    assert selection(current)["latest_file_id"] == "tax-2025"
    assert selection(current)["selected_file_id"] is None
    assert not next(row for row in selection(current)["sources"] if row["file_id"] == "tax-2024")["baseline_eligible"]


def test_identity_unresolved_latest_business_return_blocks_older_but_known_other_business_does_not():
    # A fiscal year that has already ended; a period still running is a projection.
    fiscal = {"period_start": "2025-07-01", "period_end": "2026-06-30"}
    unresolved = business(2026, 700000, 100000, entity=None, **fiscal)
    current = build([*sources(), unresolved])
    assert current["ebitda"]["headline"] is None and current["dscr"]["headline"] is None
    assert selection(current)["latest_file_id"] == "tax-2026"
    unrelated = business(2026, 700000, 100000, entity="Unrelated Business LLC", **fiscal)
    current = build([*sources(), unrelated])
    assert current["ebitda"]["headline"]["value"] == -54369
    assert selection(current)["latest_file_id"] == "tax-2025"
    assert current["dscr"]["headline"]["numerator"] == -54369


@pytest.mark.parametrize("revenue,direction", [(40884, "neutral"), (20000, "down"), (604785, "up")])
def test_comparable_prior_year_only_supplies_trend_direction(revenue, direction):
    rows = sources()
    rows[1]["key_facts"]["gross_receipts"] = revenue
    current = build(rows)
    assert current["ebitda"]["revenue_trend"]["direction"] == direction
    assert selection(current)["selected_file_id"] == "tax-2025"
    assert {ref["file_id"] for ref in current["ebitda"]["headline"]["source_refs"]} == {"tax-2025"}


def test_same_latest_period_tax_and_p_and_l_remain_alternatives_without_summing():
    rows = [*sources(), pnl(period_start="2025-01-01", period_end="2025-12-31", basis="cash")]
    original = build(rows)
    assert {row["file_id"] for row in selection(original)["sources"] if row["baseline_eligible"]} == {"source", "tax-2025"}
    state, _ = change(None, selection(original), mode="manual", selected_file_id="tax-2025", excluded_file_ids=[],
        reason="Use current filed tax earnings basis", expected_revision=0, source_stamp=selection(original)["source_stamp"],
        idempotency_key="source-review-1", actor=ACTOR)
    current = build(rows, state=state)
    assert current["ebitda"]["headline"]["value"] == -54369
    assert {ref["file_id"] for ref in current["ebitda"]["headline"]["source_refs"]} == {"tax-2025"}


def test_filed_return_is_also_the_coverage_basis_and_the_newer_p_and_l_is_comparative():
    current = build([*sources(), pnl(basis="cash")])
    align_coverage(current["ebitda"], current["dscr"], current["metric_review"])
    assert current["dscr"]["headline"]["key"] == "tax_return"
    assert current["dscr"]["headline"]["numerator"] == -54369
    assert {row["key"] for row in current["dscr"]["comparative_candidates"]} == {"profit_and_loss"}
    selected = [row["key"] for row in current["metric_review"]["calculations"] if row.get("selected")]
    assert "dscr.tax_return" in selected and "dscr.profit_and_loss" not in selected


def test_older_p_and_l_cannot_replace_the_newer_tax_coverage_basis():
    current = build([*sources(), pnl(period_start="2024-01-01", period_end="2024-06-30")])
    align_coverage(current["ebitda"], current["dscr"])
    assert current["dscr"]["headline"]["key"] == "tax_return"
    assert current["dscr"]["headline"]["period"] == "2025"
    assert {row["key"] for row in current["dscr"]["candidates"]} == {"tax_return"}


def test_incomplete_latest_period_never_exposes_previous_income_to_the_plan():
    rows = sources()
    rows[1]["key_facts"]["ordinary_business_income"] = None
    current = build(rows)
    align_coverage(current["ebitda"], current["dscr"])
    assert current["dscr"]["headline"] is None
    assert current["dscr"]["candidates"] == []
    assert current["dscr"]["comparative_candidates"][0]["period"] == "2024"
    assert "older" in current["dscr"]["reason"]


def test_bank_proxy_remains_distinct_when_latest_business_period_is_unavailable():
    current = build(sources())
    dscr = {"candidates": [{"key": "bank_cash_flow", "label": "Bank cash flow", "value": 2.5,
                            "numerator": 30000, "source_refs": [], "pro_forma_value": 2.0}]}
    align_coverage(current["ebitda"], dscr)
    assert dscr["headline"] is None and dscr["candidates"] == []
    assert dscr["bank_proxy"]["value"] == 2.5
    assert dscr["bank_proxy"]["comparative_only"]
    no_business = {"source_selection": {"sources": [], "selected_file_id": None, "latest_file_id": None}}
    bank_only = {"candidates": [deepcopy(dscr["bank_proxy"])]}
    align_coverage(no_business, bank_only)
    assert bank_only["headline"]["label"] == "Bank cash flow · provisional proxy"
    assert "not EBITDA" in bank_only["headline"]["note"]


def test_full_annual_p_and_l_alone_supplies_reviewed_revenue_and_ebitda_illustration():
    pl = pnl(period_start="2025-01-01", period_end="2025-12-31")
    original = build([pl], now=datetime(2026, 11, 9, tzinfo=UTC))
    state, _ = change(None, selection(original), mode="manual", selected_file_id="source", excluded_file_ids=[],
        forecast_assumptions={"revenue_growth_pct": 10, "ebitda_margin_pct": None}, reason="Reviewed forward outlook",
        expected_revision=0, source_stamp=selection(original)["source_stamp"], idempotency_key="source-review-1", actor=ACTOR)
    current = build([pl], state=state, now=datetime(2026, 11, 9, tzinfo=UTC))
    forecast = current["ebitda"]["revenue_trend"]["forecast"]
    assert forecast["projected_revenue"] == 220000
    assert forecast["projected_ebitda"] == 28600
    assert forecast["target_year"] == 2026
    assert current["ebitda"]["headline"]["value"] == 26000


def test_current_partial_p_and_l_alone_has_run_rate_without_inventing_prior_year_change():
    current = build([pnl()], now=datetime(2026, 11, 9, tzinfo=UTC))
    forecast = current["ebitda"]["revenue_trend"]["forecast"]
    assert forecast["projected_revenue"] == 400000 and forecast["projected_ebitda"] == 52000
    assert forecast["change_amount"] is None and forecast["change_pct"] is None


def test_prior_year_cannot_replace_the_latest_excluded_annual_return():
    original = build(sources(), now=datetime(2026, 11, 9, tzinfo=UTC))
    with pytest.raises(HTTPException) as error:
        change(None, selection(original), mode="manual", selected_file_id="tax-2024", excluded_file_ids=["tax-2025"],
            forecast_assumptions={"revenue_growth_pct": 10, "ebitda_margin_pct": None}, reason="Try an older historical baseline",
            expected_revision=0, source_stamp=selection(original)["source_stamp"], idempotency_key="source-review-1", actor=ACTOR)
    assert error.value.status_code == 422
    assert "historical comparison only" in error.value.detail


def test_full_annual_p_and_l_and_tax_same_period_are_one_comparison_observation():
    current = build([*sources(), pnl(period_start="2025-01-01", period_end="2025-12-31", basis="cash")])
    trend = current["ebitda"]["revenue_trend"]
    assert [row["year"] for row in trend["periods"]] == ["2024", "2025"]
    assert trend["comparison"]["comparable"] is True


def test_unread_accounting_method_or_dates_do_not_block_the_year_over_year_comparison():
    rows = sources()
    rows[1]["key_facts"].update(accounting_basis=None)
    assert build(rows)["ebitda"]["revenue_trend"]["comparison"]["comparable"] is True
    # A return that prints only its tax year is read as that full year.
    for row in rows[:2]:
        row["key_facts"].update(period_start=None, period_end=None)
    trend = build(rows)["ebitda"]["revenue_trend"]
    assert trend["comparison"]["comparable"] is True and trend["direction"] == "up"
    assert all(row["period_assumed"] and row["months_covered"] == 12 for row in trend["periods"])


@pytest.mark.parametrize("changes", [{"currency": "CAD"}, {"accounting_basis": "Accrual"}, {"period_start": None}, {"period_end": "2025-11-30"}])
def test_mismatched_unknown_and_short_periods_do_not_supply_growth(changes):
    rows = sources()
    rows[1]["key_facts"].update(changes)
    trend = build(rows)["ebitda"]["revenue_trend"]
    assert not (trend["comparison"] and trend["comparison"]["comparable"])
    assert trend["direction"] is None
    assert trend["forecast"]["status"] == "unavailable"


def test_source_review_role_and_schema_controls():
    c = build(sources())
    with pytest.raises(HTTPException) as error:
        change(None, selection(c), mode="manual", selected_file_id="tax-2025", excluded_file_ids=[], reason="Review",
               expected_revision=0, source_stamp=selection(c)["source_stamp"], idempotency_key="receipt-1", actor=SimpleNamespace(role=Role.CLIENT))
    assert error.value.status_code == 403
    for growth in [True, float("nan"), 1001, -101]:
        with pytest.raises(ValidationError):
            FinancialRevenueAssumptions(revenue_growth_pct=growth)
    with pytest.raises(ValidationError):
        FinancialEarningsSources(mode="manual", selected_file_id=uuid4(), reason="review", expected_revision=0, source_stamp="bad", idempotency_key="receipt-1")


def test_personal_association_override_cannot_turn_household_packet_into_business_income():
    personal = file("tax_return", {"entity": ENTITY, "filers": "Synthetic individual", "filing_status": "Married Filing Jointly",
                                  "agi": 100000, "ordinary_business_income": 9999999, "tax_year": "2025"}, key="personal")
    c = financial_cockpit.build_cockpit([personal], [], business_entity=ENTITY)
    assert c["tax"]["years"] == []
    assert c["ebitda"]["headline"] is None
    assert selection(c)["sources"][0]["eligible"] is False


def test_external_source_read_does_not_expose_private_review_history():
    from app.services.financial_metric_review import redact_for_external

    original = build(sources())
    state, _ = save(original)
    state["events"][0].update(reason="PRIVATE REASON", actor_name="PRIVATE ACTOR")
    current = build(sources(), state=state)
    external = redact_for_external(current, SimpleNamespace(role=Role.CLIENT))
    assert selection(external)["history"] == []
    assert "PRIVATE" not in json.dumps(external)
    assert selection(current)["history"]


def test_source_router_checks_role_then_profile_scope_before_mutation():
    from app.routers.financial_metrics import review_earnings_sources

    payload = FinancialEarningsSources(mode="manual", selected_file_id=None, reason="Reviewed source", expected_revision=0,
                                      source_stamp="a" * 64, idempotency_key="receipt-1")
    db = SimpleNamespace(commit=AsyncMock())
    with patch("app.routers.financial_metrics.profiles.load_profile", AsyncMock()) as load:
        with pytest.raises(HTTPException) as error:
            asyncio.run(review_earnings_sources(uuid4(), payload, SimpleNamespace(role=Role.BROKER), db))
        assert error.value.status_code == 403
        load.assert_not_awaited()
    with (patch("app.routers.financial_metrics.profiles.load_profile", AsyncMock(side_effect=HTTPException(404, "Profile not found"))),
          patch("app.services.financial_metric_review.save_earnings_sources", AsyncMock()) as mutate):
        with pytest.raises(HTTPException) as error:
            asyncio.run(review_earnings_sources(uuid4(), payload, ACTOR, db))
        assert error.value.status_code == 404
        mutate.assert_not_awaited()
    db.commit.assert_not_awaited()


def test_source_service_locks_refreshes_audits_invalidates_and_refreshes_once():
    from app.services import financial_metric_review

    @asynccontextmanager
    async def savepoint():
        yield

    profile = SimpleNamespace(id=uuid4(), financial_metric_review={})
    stale = SimpleNamespace(id=profile.id, financial_metric_review={"revision": 88})
    original = build(sources())
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: profile)), flush=AsyncMock(),
                         refresh=AsyncMock(), begin_nested=savepoint)
    payload = dict(mode="manual", selected_file_id="tax-2025", excluded_file_ids=["tax-2024"], reason="Latest business period reviewed",
                   expected_revision=0, source_stamp=selection(original)["source_stamp"], idempotency_key="receipt-1")
    with (patch("app.services.financial_workspace.lock_financial_profile", AsyncMock()) as lock,
          patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=original)),
          patch("app.services.application_profiles.log_profile_action", AsyncMock()) as audit,
          patch("app.services.financial_publication.invalidate_financial_review_caches", AsyncMock()) as invalidate,
          patch("app.services.financial_publication.refresh_advisory_projections", AsyncMock()) as refresh):
        asyncio.run(financial_metric_review.save_earnings_sources(db, stale, ACTOR, **payload))
        lock.assert_awaited_once_with(db, stale)
        assert db.execute.call_args.args[0].get_execution_options()["populate_existing"] is True
        assert profile.financial_metric_review["revision"] == 1
        assert profile.financial_metric_review["events"][-1]["source_metric"] == "ebitda"
        audit.assert_awaited_once()
        invalidate.assert_awaited_once_with(db, profile, correction_revision=1)
        refresh.assert_awaited_once()
        # Exact delivery retry reuses its receipt and does not generate work.
        audit.reset_mock()
        invalidate.reset_mock()
        refresh.reset_mock()
        asyncio.run(financial_metric_review.save_earnings_sources(db, stale, ACTOR, **payload))
        audit.assert_not_awaited()
        invalidate.assert_not_awaited()
        refresh.assert_not_awaited()
