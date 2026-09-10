"""extract_profit_and_loss / extract_balance_sheet and the lender-packet section
that renders them. These run beside the bank / tax / debt / PFS extractors in
public_underwriting_packet_pdf and are what the desk panel, the key-metrics
fallback and the packet PDF all read."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from app.services import dealer_ai_intelligence_pdf as intel
from app.services import public_underwriting_packet_pdf as pdf


def _doc(classification: str, facts: dict[str, Any], file_id: str = "f1") -> dict[str, Any]:
    return {"file_id": file_id, "classification": classification, "key_facts": facts}


TYPED_PL = {
    "source_form": "qc_pl.v1",
    "period_start": "2026-01-01",
    "period_end": "2026-06-30",
    "gross_revenue": "$600,000.00",
    "cost_of_goods_sold": 200_000,
    "gross_profit": 400_000,
    "total_operating_expenses": 340_000,
    "operating_income": 60_000,
    "other_income": 2_000,
    "income_taxes": 2_000,
    "interest": 5_000,
    "depreciation_and_amortization": 5_000,
    "taxes_and_licenses": 9_000,
    "owner_salaries": 90_000,
    "net_income": 60_000,
}


# ── profit and loss ─────────────────────────────────────────────────────────


def test_no_pl_returns_none():
    assert pdf.extract_profit_and_loss([]) is None
    assert pdf.extract_profit_and_loss([_doc("tax_return", {"gross_receipts": 1})]) is None
    assert pdf.extract_profit_and_loss([_doc("current_p_and_l", "not a dict")]) is None


def test_the_typed_shape_reads_every_figure_and_flags_typed():
    out = pdf.extract_profit_and_loss([_doc("current_p_and_l", TYPED_PL)])
    assert out is not None
    assert out["typed"] is True
    assert out["gross_revenue"] == 600_000.0
    assert out["net_income"] == 60_000.0
    assert out["months_covered"] == 6
    assert out["period_start"] == "2026-01-01" and out["period_end"] == "2026-06-30"
    assert out["taxes_and_licenses"] == 9_000.0
    assert out["owner_salaries"] == 90_000.0
    assert len(out["statements"]) == 1


def test_ebitda_adds_back_interest_income_taxes_and_da_but_never_taxes_and_licenses():
    out = pdf.extract_profit_and_loss([_doc("current_p_and_l", TYPED_PL)])
    assert out["ebitda"] == 60_000 + 5_000 + 2_000 + 5_000
    # 9,000 of taxes and licenses stays an operating cost
    assert out["ebitda"] != 60_000 + 5_000 + 2_000 + 5_000 + 9_000


def test_ebitda_is_none_without_net_income():
    # No net income printed and no lines to derive it from (operating income,
    # total operating expenses and gross profit all absent).
    facts = {**TYPED_PL, "net_income": None, "operating_income": None, "total_operating_expenses": None, "gross_profit": None, "cost_of_goods_sold": None}
    facts.pop("source_form")  # so the three-key shape rule, not the form stamp, decides `typed`
    out = pdf.extract_profit_and_loss([_doc("current_p_and_l", facts)])
    assert out["net_income"] is None
    assert out["gross_profit"] is None and out["operating_income"] is None
    assert out["ebitda"] is None
    assert out["annualized_ebitda"] is None
    assert out["typed"] is False  # net income is part of the typed shape


def test_totals_are_computed_from_lines_only_when_the_document_prints_none():
    facts = {
        "period_start": "2026-01-01",
        "period_end": "2026-06-30",
        "gross_revenue": 600_000,
        "cost_of_goods_sold": 200_000,
        "operating_income": 60_000,
        "other_income": 2_000,
        "income_taxes": 2_000,
    }
    out = pdf.extract_profit_and_loss([_doc("current_p_and_l", facts)])
    assert out["gross_profit"] == 400_000
    assert out["total_operating_expenses"] == 340_000
    assert out["net_income"] == 60_000
    # a printed total is kept even when the lines disagree
    printed = {**facts, "gross_profit": 399_000, "net_income": 55_000}
    out = pdf.extract_profit_and_loss([_doc("current_p_and_l", printed)])
    assert out["gross_profit"] == 399_000 and out["net_income"] == 55_000


def test_annualization_only_between_six_and_eleven_months():
    def _for(start: str, end: str) -> dict[str, Any]:
        return pdf.extract_profit_and_loss(
            [_doc("current_p_and_l", {**TYPED_PL, "period_start": start, "period_end": end})]
        )

    five = _for("2026-02-01", "2026-06-30")
    assert five["months_covered"] == 5 and five["annualized_revenue"] is None and five["annualized_ebitda"] is None
    six = _for("2026-01-01", "2026-06-30")
    assert six["annualized_revenue"] == 1_200_000 and six["annualized_ebitda"] == 144_000
    nine = _for("2025-10-01", "2026-06-30")
    assert nine["months_covered"] == 9 and nine["annualized_revenue"] == 800_000
    twelve = _for("2025-07-01", "2026-06-30")
    assert twelve["months_covered"] == 12 and twelve["annualized_revenue"] == 600_000
    thirteen = _for("2025-06-01", "2026-06-30")
    assert thirteen["months_covered"] == 13 and thirteen["annualized_revenue"] is None
    unknown = _for("", "2026-06-30")
    assert unknown["months_covered"] is None and unknown["annualized_revenue"] is None


def test_typed_is_false_for_ad_hoc_facts_but_the_figures_still_read():
    facts = {"period": "Jan–Jun 2026", "revenue": 600_000, "net_profit": 60_000}
    out = pdf.extract_profit_and_loss([_doc("current_p_and_l", facts)])
    assert out["typed"] is False
    assert out["gross_revenue"] == 600_000 and out["net_income"] == 60_000
    assert out["period_end"] is None and out["months_covered"] is None
    # the three-key shape without source_form is typed
    shaped = {"period_end": "2026-06-30", "gross_revenue": 1, "net_income": 1}
    assert pdf.extract_profit_and_loss([_doc("current_p_and_l", shaped)])["typed"] is True


def test_latest_period_wins_with_the_most_populated_statement_on_a_tie():
    older = {**TYPED_PL, "period_start": "2025-01-01", "period_end": "2025-12-31", "gross_revenue": 1}
    newer_thin = {"period_end": "2026-06-30", "gross_revenue": 2, "net_income": 1}
    newer_full = {**TYPED_PL, "gross_revenue": 3}
    out = pdf.extract_profit_and_loss(
        [_doc("current_p_and_l", older, "a"), _doc("current_p_and_l", newer_thin, "b"), _doc("current_p_and_l", newer_full, "c")]
    )
    assert out["file_id"] == "c" and out["gross_revenue"] == 3
    assert [s["file_id"] for s in out["statements"]] == ["a", "b", "c"]
    # nothing is summed across documents
    assert out["gross_revenue"] == 3


# ── balance sheet ───────────────────────────────────────────────────────────

TYPED_BS = {
    "source_form": "qc_bs.v1",
    "as_of_date": "2026-06-30",
    "cash": 40_000,
    "accounts_receivable": 60_000,
    "inventory": 50_000,
    "total_current_assets": 150_000,
    "total_fixed_assets": 200_000,
    "total_other_assets": 50_000,
    "total_assets": 400_000,
    "accounts_payable": 30_000,
    "current_portion_long_term_debt": 20_000,
    "total_current_liabilities": 50_000,
    "total_long_term_liabilities": 200_000,
    "total_liabilities": 250_000,
    "total_equity": 150_000,
}


def test_no_balance_sheet_returns_none():
    assert pdf.extract_balance_sheet([]) is None
    assert pdf.extract_balance_sheet([_doc("current_p_and_l", TYPED_PL)]) is None


def test_balance_sheet_reads_totals_and_derives_the_ratios():
    out = pdf.extract_balance_sheet([_doc("balance_sheet", TYPED_BS)])
    assert out["as_of_date"] == "2026-06-30"
    assert out["total_assets"] == 400_000 and out["total_liabilities"] == 250_000 and out["total_equity"] == 150_000
    assert out["working_capital"] == 100_000
    assert out["current_ratio"] == 3.0
    assert out["debt_to_equity"] == round(250_000 / 150_000, 2)
    assert out["typed"] is True


def test_balance_sheet_totals_from_subtotals_and_implied_equity_when_absent():
    facts = {
        "as_of_date": "2026-06-30",
        "total_current_assets": 150_000,
        "total_fixed_assets": 200_000,
        "total_current_liabilities": 50_000,
        "total_long_term_liabilities": 200_000,
    }
    out = pdf.extract_balance_sheet([_doc("balance_sheet", facts)])
    assert out["total_assets"] == 350_000
    assert out["total_liabilities"] == 250_000
    assert out["total_equity"] == 100_000


def test_balance_sheet_ratios_never_divide_by_zero_or_negative_equity():
    facts = {"as_of_date": "2026-06-30", "total_current_assets": 10, "total_current_liabilities": 0, "total_assets": 10, "total_liabilities": 50}
    out = pdf.extract_balance_sheet([_doc("balance_sheet", facts)])
    assert out["working_capital"] == 10
    assert out["current_ratio"] is None
    assert out["total_equity"] == -40
    assert out["debt_to_equity"] is None


def test_the_newest_balance_sheet_wins():
    out = pdf.extract_balance_sheet(
        [
            _doc("balance_sheet", {**TYPED_BS, "as_of_date": "2025-12-31", "total_assets": 1}, "old"),
            _doc("balance_sheet", TYPED_BS, "new"),
        ]
    )
    assert out["file_id"] == "new" and out["total_assets"] == 400_000
    assert len(out["statements"]) == 2


def test_a_balance_sheet_misfiled_as_a_pl_is_recovered_by_shape_and_not_read_as_a_pl():
    misfiled = _doc("current_p_and_l", {"as_of_date": "2026-03-31", "total_assets": 500_000, "total_liabilities": 300_000}, "m")
    out = pdf.extract_balance_sheet([misfiled])
    assert out is not None and out["file_id"] == "m" and out["total_equity"] == 200_000
    assert pdf.extract_profit_and_loss([misfiled]) is None
    # both totals AND a revenue line is a P&L with a balance-sheet-looking tail — rejected
    with_revenue = _doc("current_p_and_l", {"total_assets": 1, "total_liabilities": 1, "gross_revenue": 5, "net_income": 1, "period_end": "2026-06-30"})
    assert pdf.extract_balance_sheet([with_revenue]) is None
    assert pdf.extract_profit_and_loss([with_revenue])["gross_revenue"] == 5
    # one total alone is not enough
    assert pdf.extract_balance_sheet([_doc("current_p_and_l", {"total_assets": 1})]) is None


def test_balance_sheet_is_a_non_tax_class():
    assert "balance_sheet" in pdf._NON_TAX_CLASSES
    sheet = _doc("balance_sheet", {**TYPED_BS, "net_income": 5})
    assert pdf.extract_tax_years([sheet]) == []


# ── the lender packet ───────────────────────────────────────────────────────


def test_pl_bs_section_renders_nothing_without_either_statement():
    assert pdf._pl_bs_section(None, None) == ""


def test_pl_bs_section_renders_each_card_and_both_side_by_side():
    p_and_l = pdf.extract_profit_and_loss([_doc("current_p_and_l", TYPED_PL)])
    balance = pdf.extract_balance_sheet([_doc("balance_sheet", TYPED_BS)])
    only_pl = pdf._pl_bs_section(p_and_l, None)
    assert "Profit &amp; loss — 2026-01-01 to 2026-06-30" in only_pl
    assert "$1,200,000" in only_pl and "Annualized revenue" in only_pl
    assert "6 months covered" in only_pl
    assert "Balance sheet" not in only_pl and 'class="cols"' not in only_pl
    only_bs = pdf._pl_bs_section(None, balance)
    assert "Balance sheet — as of 2026-06-30" in only_bs and "3.00x" in only_bs
    assert "Profit" not in only_bs
    both = pdf._pl_bs_section(p_and_l, balance)
    assert 'class="cols"' in both and "Profit &amp; loss" in both and "Balance sheet" in both


def _captured_packet_html(**financials: Any) -> str:
    """Render the packet with the HTML renderer stubbed so the composed document
    can be inspected (WeasyPrint is forced unavailable; PyMuPDF captures)."""
    captured: dict[str, str] = {}

    def _capture(html_doc: str) -> bytes:
        captured["html"] = html_doc
        return b"%PDF-stub"

    intake = SimpleNamespace(variant="main_street_v1", full_name="Ana", business_name="Ana's Bakery", requested_loan_amount=100_000, loan_purpose="Equipment")
    result = {
        "key_metrics": {
            "estimated_ebitda_or_cash_flow": 140_000,
            "ebitda_source": "profit_and_loss",
            "revenue_source": "profit_and_loss",
            "dscr_basis": "profit_and_loss_ebitda",
            "pl_revenue": 600_000,
            "balance_sheet_total_assets": 400_000,
        }
    }
    with (
        patch.dict(sys.modules, {"weasyprint": None}),
        patch.object(pdf, "_render_html_pymupdf", side_effect=_capture),
        patch.object(pdf, "_apply_watermark", side_effect=lambda b: b),
    ):
        pdf.render_underwriting_packet_pdf(intake=intake, files=[], missing_docs=[], result=result, financials=financials)
    return captured["html"]


def test_the_generic_key_metrics_loop_skips_provenance_and_statement_keys():
    html = _captured_packet_html()
    assert "Estimated Ebitda Or Cash Flow" in html
    for stray in ("Ebitda Source", "Revenue Source", "Dscr Basis", "Pl Revenue", "Balance Sheet Total Assets"):
        assert stray not in html, stray
    # no statement → no section
    assert "Profit &amp; loss" not in html and "Balance sheet —" not in html


def test_the_packet_carries_the_statement_section_when_the_loader_supplies_one():
    p_and_l = pdf.extract_profit_and_loss([_doc("current_p_and_l", TYPED_PL)])
    html = _captured_packet_html(p_and_l=p_and_l, balance_sheet=None)
    assert "Profit &amp; loss — 2026-01-01 to 2026-06-30" in html
    assert "Balance sheet —" not in html


def test_the_packet_loader_hands_both_statements_to_the_renderer():
    import inspect

    from app.routers import dealer_ai_intake as intake_router

    source = inspect.getsource(intake_router._collect_packet_financials)
    assert '"p_and_l": extract_profit_and_loss(analyses)' in source
    assert '"balance_sheet": extract_balance_sheet(analyses)' in source
    # the empty-bucket return carries the same keys
    empty = source.split("if not active_ids:")[1].split("rows = (")[0]
    assert '"p_and_l": None' in empty and '"balance_sheet": None' in empty


# ── the intelligence PDF suffix ─────────────────────────────────────────────


def test_source_note_reads_the_provenance_stamps():
    km = {"ebitda_source": "profit_and_loss", "pl_period": "2026-01-01 to 2026-06-30", "dscr_basis": "profit_and_loss_ebitda"}
    assert intel._source_note(km, "ebitda_source") == "from P&L, 2026-01-01 to 2026-06-30"
    assert intel._source_note(km, "dscr_basis") == "from P&L, 2026-01-01 to 2026-06-30"
    assert intel._source_note({"ebitda_source": "tax_return"}, "ebitda_source") == "from tax return"
    assert intel._source_note({"ebitda_source": "tax_return", "pl_period": "x"}, "ebitda_source") == "from tax return"
    assert intel._source_note({}, "ebitda_source") == ""
    assert intel._source_note({"ebitda_source": "something_else"}, "ebitda_source") == ""
