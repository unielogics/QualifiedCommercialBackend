from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.dealer_os.services import financial_snapshot


def _owner(score: int | None = None, *, tier: str | None = None, band: str | None = None):
    return SimpleNamespace(
        credit_score=score,
        credit_tier=tier,
        credit_summary={"quality_tier": tier, "score_band": band} if tier and band else {},
    )


def _profile(**overrides):
    values = {
        "annual_sales": None,
        "annual_cash_flow_available_for_debt": None,
        "monthly_debt_payments": None,
        "field_confirmations": {},
        "field_provenance": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_confirmed_profile_values_win_and_drive_confirmed_dscr() -> None:
    profile = _profile(
        annual_sales=900_000,
        annual_cash_flow_available_for_debt=240_000,
        monthly_debt_payments=10_000,
        field_confirmations={
            "annual_sales": {},
            "annual_cash_flow_available_for_debt": {},
            "monthly_debt_payments": {},
        },
    )
    snapshot = financial_snapshot.build(
        profile=profile,
        required_owners=[_owner(742)],
        metric_tree={"dscr": {"current": 1.1}, "adb": {"current": 42_000}},
        period_rows=[],
        statement_months=[],
        suggestions={
            "annual_sales": {"value": 1_200_000},
            "annual_cash_flow_available_for_debt": {"value": 360_000},
            "monthly_debt_payments": {"value": 20_000},
        },
        negative_balance_days_90=0,
        nsf_count=0,
    )

    assert snapshot["annual_sales"] == 900_000
    assert snapshot["dscr"] == 2.0
    assert snapshot["sources"]["annual_sales"]["status"] == "confirmed"
    assert snapshot["sources"]["dscr"]["status"] == "confirmed"
    assert snapshot["credit_quality_tier"] == "Good"
    assert snapshot["credit_score_band"] == "720\u2013759"


def test_evidence_suggestions_prefill_blanks_without_becoming_confirmed() -> None:
    periods = [
        {"deposits": 100_000},
        {"deposits": 120_000},
        {"deposits": 110_000},
    ]
    snapshot = financial_snapshot.build(
        profile=_profile(),
        required_owners=[_owner(tier="Average", band="660-699")],
        metric_tree={
            "dscr": {"current": None, "draft": 1.28, "cash_flow": 1.31},
            "adb": {"current": 18_500, "source": "ending_balance_proxy"},
        },
        period_rows=periods,
        statement_months=["2026-06", "2026-07", "2026-08"],
        suggestions={
            "annual_sales": {
                "value": 1_320_000,
                "status": "estimated",
                "source": "verified_bank_evidence",
                "label": "Evidence-backed estimate",
            },
            "annual_cash_flow_available_for_debt": {"value": 180_000},
            "monthly_debt_payments": {"value": 12_000},
        },
        negative_balance_days_90=2,
        nsf_count=1,
    )

    assert snapshot["annual_sales"] == 1_320_000
    assert snapshot["sources"]["annual_sales"]["status"] == "estimated"
    assert snapshot["dscr"] == 1.25
    assert snapshot["sources"]["dscr"]["status"] == "estimated"
    assert snapshot["avg_daily_balance"] == 18_500
    assert snapshot["sources"]["avg_daily_balance"]["source"] == "ending_balance_proxy"
    assert snapshot["sources"]["avg_daily_balance"]["status"] == "estimated"
    assert snapshot["annualized_deposits"] == 1_320_000
    assert snapshot["negative_balance_days_90"] == 2
    assert snapshot["returned_items"] == 1


def test_missing_activity_remains_null_instead_of_displaying_fake_zero() -> None:
    snapshot = financial_snapshot.build(
        profile=None,
        required_owners=[_owner()],
        metric_tree={"dscr": {}, "adb": {}},
        period_rows=[],
        statement_months=[],
        suggestions={},
        negative_balance_days_90=None,
        nsf_count=None,
    )

    assert snapshot["dscr"] is None
    assert snapshot["avg_daily_balance"] is None
    assert snapshot["negative_balance_days_90"] is None
    assert snapshot["returned_items"] is None
    assert snapshot["credit_quality_tier"] is None
    assert snapshot["sources"]["returned_items"]["status"] == "unavailable"


@pytest.mark.parametrize("confirmed", [False, True])
def test_accepted_schedule_overrides_stale_profile_monthly_debt(confirmed: bool) -> None:
    snapshot = financial_snapshot.build(
        profile=_profile(
            annual_cash_flow_available_for_debt=240_000,
            monthly_debt_payments=30_000,
            field_confirmations={"annual_cash_flow_available_for_debt": {}} if confirmed else {},
        ),
        required_owners=[],
        metric_tree={"dscr": {
            "schedule_authoritative": True,
            "monthly_debt_service": 10_000,
            "current": 1.1,
        }},
        period_rows=[],
        statement_months=[],
        suggestions={"monthly_debt_payments": {"value": 50_000}},
        negative_balance_days_90=None,
        nsf_count=None,
    )

    assert snapshot["monthly_debt_payments"] == 10_000
    assert snapshot["annual_cash_flow_available_for_debt"] == 240_000
    assert snapshot["dscr"] == 2.0
    assert snapshot["sources"]["monthly_debt_payments"]["source"] == "accepted_debt_schedule"
    assert snapshot["sources"]["monthly_debt_payments"]["label"] == "Saved debt schedule"
    assert snapshot["sources"]["dscr"]["source"] == "accepted_debt_schedule"
    assert snapshot["sources"]["dscr"]["status"] == ("confirmed" if confirmed else "estimated")


@pytest.mark.parametrize("accepted_monthly", [0, None])
def test_no_positive_accepted_debt_blocks_stale_ratios(accepted_monthly: float | None) -> None:
    snapshot = financial_snapshot.build(
        profile=_profile(
            annual_cash_flow_available_for_debt=240_000,
            monthly_debt_payments=10_000,
            field_confirmations={
                "annual_cash_flow_available_for_debt": {},
                "monthly_debt_payments": {},
            },
        ),
        required_owners=[],
        metric_tree={"dscr": {
            "schedule_authoritative": True,
            "monthly_debt_service": accepted_monthly,
            "current": 2.5,
            "draft": 2.0,
            "cash_flow": 1.5,
        }},
        period_rows=[],
        statement_months=[],
        suggestions={},
        negative_balance_days_90=None,
        nsf_count=None,
    )

    assert snapshot["monthly_debt_payments"] == accepted_monthly
    assert snapshot["dscr"] is None
    assert snapshot["sources"]["dscr"]["status"] == "unavailable"


@pytest.mark.parametrize("engine_current", [1.75, None])
def test_accepted_schedule_without_entered_cash_flow_uses_only_engine_current(
    engine_current: float | None,
) -> None:
    snapshot = financial_snapshot.build(
        profile=_profile(monthly_debt_payments=40_000),
        required_owners=[],
        metric_tree={"dscr": {
            "schedule_authoritative": True,
            "monthly_debt_service": 10_000,
            "current": engine_current,
            "draft": 9,
            "cash_flow": 8,
        }},
        period_rows=[],
        statement_months=[],
        suggestions={"annual_cash_flow_available_for_debt": {"value": 120_000}},
        negative_balance_days_90=None,
        nsf_count=None,
    )

    # Annual cash-flow display retains the established suggestion policy, while
    # DSCR uses the engine's accepted-schedule numerator when none was entered.
    assert snapshot["annual_cash_flow_available_for_debt"] == 120_000
    assert snapshot["monthly_debt_payments"] == 10_000
    assert snapshot["dscr"] == engine_current
    assert snapshot["sources"]["dscr"]["status"] == (
        "verified" if engine_current is not None else "unavailable"
    )


def test_accepted_schedule_preserves_explicit_zero_annual_cash_flow() -> None:
    snapshot = financial_snapshot.build(
        profile=_profile(annual_cash_flow_available_for_debt=0, monthly_debt_payments=40_000),
        required_owners=[],
        metric_tree={"dscr": {
            "schedule_authoritative": True,
            "monthly_debt_service": 10_000,
            "current": 2,
        }},
        period_rows=[],
        statement_months=[],
        suggestions={},
        negative_balance_days_90=None,
        nsf_count=None,
    )

    assert snapshot["dscr"] == 0


def test_capacity_uses_modelled_path_not_requested_amount() -> None:
    snapshot = financial_snapshot.add_capacity(
        {"sources": {}},
        {"label": "EZ Term", "funding_typical": "175000.50"},
    )

    assert snapshot["indicative_capacity"] == 175_000.5
    assert snapshot["capacity_path"] == "EZ Term"
    assert snapshot["sources"]["indicative_capacity"]["source"] == "program_sizing"
