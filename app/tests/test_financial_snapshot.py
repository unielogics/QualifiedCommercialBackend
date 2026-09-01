from __future__ import annotations

from types import SimpleNamespace

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


def test_capacity_uses_modelled_path_not_requested_amount() -> None:
    snapshot = financial_snapshot.add_capacity(
        {"sources": {}},
        {"label": "EZ Term", "funding_typical": "175000.50"},
    )

    assert snapshot["indicative_capacity"] == 175_000.5
    assert snapshot["capacity_path"] == "EZ Term"
    assert snapshot["sources"]["indicative_capacity"]["source"] == "program_sizing"
