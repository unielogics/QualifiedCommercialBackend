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
from app.services.financial_earnings_sources import change
from app.tests.test_financial_metric_review import ACTOR, ENTITY, build, file, pnl


def business(year, revenue, income, **facts):
    return file("tax_return", {"entity": ENTITY, "tax_form": "1120-S", "tax_year": str(year),
        "period_start": f"{year}-01-01", "period_end": f"{year}-12-31", "gross_receipts": revenue,
        "ordinary_business_income": income, "interest_expense": 170, "accounting_basis": "Cash", **facts}, key=f"tax-{year}")


def selection(cockpit):
    return cockpit["ebitda"]["source_selection"]


def save(cockpit, state=None, **extra):
    return change(state, selection(cockpit), mode="manual", selected_file_id="tax-2025", excluded_file_ids=["tax-2024", "personal"],
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
    assert current["ebitda"]["headline"]["provisional"] is True
    assert current["dscr"]["headline"]["numerator"] == -54369
    selected = selection(current)
    assert selected["selected_file_id"] == "tax-2025"
    assert [row["file_id"] for row in selected["sources"] if row["selected"]] == ["tax-2025"]
    assert next(row for row in selected["sources"] if row["file_id"] == "personal")["eligible"] is False
    assert all(ref["file_id"] != "personal" for ref in current["ebitda"]["headline"]["source_refs"])
    assert next(row for row in current["metric_review"]["inputs"] if row["key"].endswith(":2025.depreciation"))["source_refs"][0]["fact_key"] == "amortization_health_record_soft"


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


def test_manual_no_source_removes_baseline_without_deleting_documents_or_dscr():
    c = build(sources())
    state, _ = change(None, selection(c), mode="manual", selected_file_id=None, excluded_file_ids=[], reason="Awaiting reconciled return",
                      expected_revision=0, source_stamp=selection(c)["source_stamp"], idempotency_key="receipt-1", actor=ACTOR)
    current = build(sources(), state=state)
    assert current["ebitda"]["headline"] is None
    assert len(current["ebitda"]["source_selection"]["sources"]) == 3
    assert current["dscr"]["headline"]["value"] == c["dscr"]["headline"]["value"]


def test_current_year_outlook_is_not_invented_from_explosive_growth_or_bank_deposits():
    c = build(sources(), now=datetime(2026, 11, 9, tzinfo=UTC))
    trend = c["ebitda"]["revenue_trend"]
    assert trend["comparison"]["current_revenue"] == 604785
    assert trend["comparison"]["change_amount"] == 563901
    assert trend["comparison"]["change_pct"] == 1379.27
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


def test_explicit_assumptions_replace_current_p_and_l_illustration_consistently():
    original = build([*sources(), pnl(basis="cash")], now=datetime(2026, 11, 9, tzinfo=UTC))
    state, _ = save(original, forecast_assumptions={"revenue_growth_pct": 10, "ebitda_margin_pct": 5})
    current = build([*sources(), pnl(basis="cash")], state=state, now=datetime(2026, 11, 9, tzinfo=UTC))
    forecast = current["ebitda"]["revenue_trend"]["forecast"]
    assert forecast["method"] == "reviewed_assumptions"
    assert forecast["projected_revenue"] == 665263.5
    assert forecast["projected_ebitda"] == 33263.18
    assert forecast["margin"] == 0.05
    assert forecast["ebitda_kind"] == "reviewed_assumption"


def test_excluded_current_p_and_l_never_supplies_projected_ebitda():
    original = build([*sources(), pnl(basis="cash")], now=datetime(2026, 11, 9, tzinfo=UTC))
    state, _ = change(None, selection(original), mode="manual", selected_file_id=None, excluded_file_ids=["source"],
                      reason="P&L earnings awaiting reconciliation", expected_revision=0, source_stamp=selection(original)["source_stamp"],
                      idempotency_key="source-review-1", actor=ACTOR)
    current = build([*sources(), pnl(basis="cash")], state=state, now=datetime(2026, 11, 9, tzinfo=UTC))
    assert current["ebitda"]["revenue_trend"]["forecast"]["projected_revenue"] == 400000
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
    assert selection(c)["selected_file_id"] == "source"


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


def test_reviewed_forecast_uses_selected_period_not_latest_excluded_annual_return():
    original = build(sources(), now=datetime(2026, 11, 9, tzinfo=UTC))
    state, _ = change(None, selection(original), mode="manual", selected_file_id="tax-2024", excluded_file_ids=["tax-2025"],
        forecast_assumptions={"revenue_growth_pct": 10, "ebitda_margin_pct": None}, reason="Reviewed alternative historical baseline",
        expected_revision=0, source_stamp=selection(original)["source_stamp"], idempotency_key="source-review-1", actor=ACTOR)
    current = build(sources(), state=state, now=datetime(2026, 11, 9, tzinfo=UTC))
    forecast = current["ebitda"]["revenue_trend"]["forecast"]
    assert forecast["projected_revenue"] == 44972.4
    assert forecast["projected_ebitda"] == -61391
    assert "2024" in forecast["assumptions"][0]
    assert current["ebitda"]["revenue_trend"]["comparison"]["current_year"] == "2025"


def test_full_annual_p_and_l_and_tax_same_period_are_one_comparison_observation():
    current = build([*sources(), pnl(period_start="2025-01-01", period_end="2025-12-31", basis="cash")])
    trend = current["ebitda"]["revenue_trend"]
    assert [row["year"] for row in trend["periods"]] == ["2024", "2025"]
    assert trend["comparison"]["comparable"] is True


@pytest.mark.parametrize("changes", [{"currency": "CAD"}, {"accounting_basis": None}, {"period_start": None}, {"period_end": "2025-11-30"}])
def test_mismatched_unknown_and_short_periods_do_not_supply_growth(changes):
    rows = sources()
    rows[1]["key_facts"].update(changes)
    trend = build(rows)["ebitda"]["revenue_trend"]
    assert not (trend["comparison"] and trend["comparison"]["comparable"])
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
