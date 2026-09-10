"""bucket_ai._compute_key_metrics_from_cache — first coverage.

The function fills key_metrics the synthesis left blank from the durable
per-file facts. Revenue and earnings come from tax returns; with this change a
profit-and-loss statement stands in ONLY as a labelled fallback, and only when
it was read through the typed block (or filed from our own form), covers at
least six months, ended recently and shows revenue. The informational pl_* /
balance_sheet_* keys are written only when a statement exists and never as None.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.services import bucket_ai as ai


def _recent_period(months: int) -> tuple[str, str]:
    """An ISO period of `months` whole calendar months ending last month."""
    today = date.today()
    end = today.replace(day=1) - timedelta(days=1)
    start_month, start_year = end.month - (months - 1), end.year
    while start_month <= 0:
        start_month += 12
        start_year -= 1
    return date(start_year, start_month, 1).isoformat(), end.isoformat()


def _analysis(classification: str, facts: dict[str, Any], file_id: str = "f1") -> dict[str, Any]:
    return {"file_id": file_id, "ai_classification": classification, "key_facts": facts}


def _typed_pl(months: int = 6, **overrides: Any) -> dict[str, Any]:
    start, end = _recent_period(months)
    facts: dict[str, Any] = {
        "source_form": "qc_pl.v1",
        "period_start": start,
        "period_end": end,
        "gross_revenue": 600_000,
        "net_income": 60_000,
        "interest": 5_000,
        "income_taxes": 0,
        "depreciation_and_amortization": 5_000,
        "taxes_and_licenses": 9_000,
    }
    facts.update(overrides)
    return _analysis("current_p_and_l", facts)


TAX = _analysis("tax_return", {"tax_year": "2025", "gross_receipts": 900_000, "net_income": 80_000}, "t1")
DEBTS = _analysis(
    "debt_schedule",
    {"total_monthly_debt_service": 5_000, "total_outstanding_balance": 200_000, "debts": []},
    "d1",
)
BALANCE_SHEET = _analysis(
    "balance_sheet",
    {
        "as_of_date": "2026-06-30",
        "total_current_assets": 150_000,
        "total_current_liabilities": 50_000,
        "total_assets": 400_000,
        "total_liabilities": 250_000,
        "total_equity": 150_000,
    },
    "b1",
)


def _run(analyses: list[dict[str, Any]], key_metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"key_metrics": key_metrics or {}}
    ai._compute_key_metrics_from_cache(result, analyses)
    return result["key_metrics"]


# ── the frozen keys ──────────────────────────────────────────────────────────


def test_a_tax_return_wins_outright_and_is_stamped_as_the_source():
    km = _run([TAX, _typed_pl()])
    assert km["ytd_annualized_revenue"] == 900_000
    assert km["revenue_source"] == "tax_return"
    assert km["estimated_ebitda_or_cash_flow"] == 80_000
    assert km["ebitda_source"] == "tax_return"
    # the statement still feeds the informational keys
    assert km["pl_revenue"] == 600_000
    assert km["pl_annualized_revenue"] == 1_200_000


def test_a_typed_six_month_pl_fills_the_frozen_keys_only_without_a_tax_return():
    km = _run([_typed_pl(months=6)])
    assert km["ytd_annualized_revenue"] == 1_200_000
    assert km["revenue_source"] == "profit_and_loss"
    # EBITDA = 60,000 + 5,000 interest + 0 income taxes + 5,000 D&A = 70,000 → ×12/6
    assert km["estimated_ebitda_or_cash_flow"] == 140_000
    assert km["ebitda_source"] == "profit_and_loss"
    assert km["pl_ebitda"] == 70_000
    assert km["pl_months"] == 6
    assert km["pl_period"].endswith(_recent_period(6)[1])


def test_a_twelve_month_typed_pl_is_taken_as_is():
    km = _run([_typed_pl(months=12)])
    assert km["ytd_annualized_revenue"] == 600_000
    assert km["estimated_ebitda_or_cash_flow"] == 70_000


def test_model_invented_pl_facts_never_touch_the_frozen_keys():
    start, end = _recent_period(6)
    ad_hoc = _analysis(
        "current_p_and_l",
        {"period_start": start, "period_end": end, "revenue": 600_000, "net_profit": 60_000},
    )
    km = _run([ad_hoc])
    assert "ytd_annualized_revenue" not in km
    assert "estimated_ebitda_or_cash_flow" not in km
    assert "revenue_source" not in km and "ebitda_source" not in km
    # …but the informational keys still show what the document says
    assert km["pl_revenue"] == 600_000
    assert km["pl_net_income"] == 60_000


def test_a_pl_under_six_months_stays_informational():
    km = _run([_typed_pl(months=5)])
    assert "ytd_annualized_revenue" not in km
    assert "estimated_ebitda_or_cash_flow" not in km
    assert km["pl_revenue"] == 600_000
    assert "pl_annualized_revenue" not in km  # never annualized, never written as None


def test_a_stale_or_unknown_period_never_fills_the_frozen_keys():
    stale = _typed_pl(period_start="2023-01-01", period_end="2023-12-31")
    km = _run([stale])
    assert "ytd_annualized_revenue" not in km
    unknown = _typed_pl(period_end=None)
    km = _run([unknown])
    assert "ytd_annualized_revenue" not in km
    assert "pl_months" not in km


def test_zero_revenue_never_fills_the_frozen_keys():
    km = _run([_typed_pl(gross_revenue=0)])
    assert "ytd_annualized_revenue" not in km
    assert "estimated_ebitda_or_cash_flow" not in km


def test_an_ai_supplied_value_is_never_overwritten_or_relabelled():
    km = _run([TAX, _typed_pl()], {"ytd_annualized_revenue": 5, "estimated_ebitda_or_cash_flow": "7"})
    assert km["ytd_annualized_revenue"] == 5
    assert km["estimated_ebitda_or_cash_flow"] == "7"
    assert "revenue_source" not in km and "ebitda_source" not in km


def test_tax_revenue_and_pl_earnings_can_mix_when_the_return_lacks_net_income():
    tax = _analysis("tax_return", {"tax_year": "2025", "gross_receipts": 900_000}, "t1")
    km = _run([tax, _typed_pl()])
    assert km["ytd_annualized_revenue"] == 900_000 and km["revenue_source"] == "tax_return"
    assert km["estimated_ebitda_or_cash_flow"] == 140_000 and km["ebitda_source"] == "profit_and_loss"


# ── DSCR ─────────────────────────────────────────────────────────────────────


def test_dscr_uses_tax_net_income_when_a_return_exists_and_says_so():
    km = _run([TAX, DEBTS, _typed_pl()])
    assert km["estimated_debt_burden"] == 60_000
    assert km["estimated_dscr"] == round(80_000 / 60_000, 2)
    assert km["dscr_basis"] == "tax_return"


def test_dscr_falls_back_to_the_annualized_pl_ebitda_without_a_return():
    km = _run([DEBTS, _typed_pl()])
    assert km["estimated_dscr"] == round(140_000 / 60_000, 2)
    assert km["dscr_basis"] == "profit_and_loss_ebitda"


def test_dscr_stays_blank_for_an_untyped_pl():
    start, end = _recent_period(6)
    ad_hoc = _analysis("current_p_and_l", {"period_start": start, "period_end": end, "revenue": 1, "net_profit": 1})
    km = _run([DEBTS, ad_hoc])
    assert km["estimated_debt_burden"] == 60_000
    assert "estimated_dscr" not in km and "dscr_basis" not in km


# ── informational keys ──────────────────────────────────────────────────────


def test_balance_sheet_keys_are_written_from_the_statement():
    km = _run([BALANCE_SHEET])
    assert km["balance_sheet_as_of"] == "2026-06-30"
    assert km["balance_sheet_total_assets"] == 400_000
    assert km["balance_sheet_total_liabilities"] == 250_000
    assert km["balance_sheet_equity"] == 150_000
    assert km["balance_sheet_current_ratio"] == 3.0
    # a balance sheet is never revenue
    assert "ytd_annualized_revenue" not in km


def test_a_figure_the_statement_lacks_is_never_written_as_none():
    sheet = _analysis("balance_sheet", {"as_of_date": "2026-06-30", "total_assets": 1_000, "total_liabilities": 400}, "b2")
    km = _run([sheet])
    assert "balance_sheet_current_ratio" not in km
    assert km["balance_sheet_equity"] == 600  # implied when the sheet prints none
    assert all(v is not None for v in km.values())


def test_no_statement_means_no_statement_keys_and_no_none_values():
    km = _run([TAX, DEBTS])
    assert not [k for k in km if k.startswith(("pl_", "balance_sheet_"))]
    assert all(v is not None for v in km.values())
    assert km["revenue_source"] == "tax_return"


def test_an_empty_file_produces_no_keys_at_all():
    assert _run([]) == {}


# ── the recency gate ────────────────────────────────────────────────────────


def test_period_end_recency_window():
    today = date.today()
    assert ai._period_end_is_recent(today.isoformat())
    assert ai._period_end_is_recent((today.replace(day=1) - timedelta(days=1)).isoformat())
    assert not ai._period_end_is_recent("2020-12-31")
    assert not ai._period_end_is_recent(None)
    assert not ai._period_end_is_recent("June 2026")
    # a statement dated next year is a projection, not actuals
    assert not ai._period_end_is_recent(date(today.year + 1, 12, 31).isoformat())
