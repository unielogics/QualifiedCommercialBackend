from __future__ import annotations

import hashlib

import fitz
import pytest

from app.services.wells_fargo_bank_movements import parse_wells_fargo_pages, parse_wells_fargo_pdf


def header(period="September 1, 2026 - September 30, 2026", account="100000004321"):
    return f"Wells Fargo Bank, N.A.\nOptimize Business Checking SM\nAccount number: {account}\n{period} Page 1 of 3\nFICTIONAL EXAMPLE LLC\n"


SUMMARY = """Account summary
Beginning balance Total credits Total debits Ending balance
100000004321
$100.00
$500.00
-$300.00
$300.00
"""


def codes(result):
    return {error["code"] for error in result["coverage"]["errors"]}


def test_extracts_real_credit_and_debit_rows_and_keeps_audit_separate():
    text = header() + SUMMARY + """Credits
Electronic deposits/bank credits
Effective date Posted date Amount Transaction detail
09/09
700.00 Online Transfer From Fictional Trading Business Checking
xxxxxx8765 Ref #Ib035Rz4W9 on09/09/26
09/10
35.00 Nontransfer deposit
Debits
Electronic debits/bank debits
09/11
40.25 Online Transfer To Fictional Reserve Business Checking
xxxxxx8765 Ref #Aa123456789012 on09/11/26
"""
    result = parse_wells_fargo_pages([text], source_hash="a" * 64)
    assert result["coverage"]["complete"]
    assert result["coverage"]["candidate_count"] == result["coverage"]["parsed_count"] == 2
    assert result["account_last4"] == "4321"
    assert result["currency"] == "USD"
    assert result["currency_basis"] == "inferred_us_statement_context"
    first, second = result["transfer_lines"]
    assert (first["date"], first["amount"], first["direction"], first["transfer_reference"]) == ("2026-09-09", 700.0, "in", "Ib035Rz4W9")
    assert (second["date"], second["amount"], second["direction"], second["transfer_reference"]) == ("2026-09-11", 40.25, "out", "Aa123456789012")
    assert first["counterparty_account_last4"] == "8765"
    assert "100000004321" not in str(result)
    assert "Aa123456789012" in second["quote"]
    assert result["summary_audit"][0]["reconciliation_difference"] == 0
    assert result["summary_audit"][0]["credits"] == 500  # Not replaced by row amount 700.


def test_effective_then_posted_dates_and_cross_year_resolution_use_printed_period():
    text = header("December 15, 2025 - January 14, 2026") + """Credits
12/30
01/02
100.00 Online Transfer From Fictional Reserve
xxxx8765 Ref #DecemberCross on01/02/26
12/31 01/03 200.00 Online Transfer From Fictional Reserve
xxxx8765 Ref #InlineCross on01/03/26
"""
    result = parse_wells_fargo_pages([text])
    assert result["coverage"]["complete"]
    assert [row["date"] for row in result["transfer_lines"]] == ["2026-01-02", "2026-01-03"]
    assert result["currency"] is None  # No printed dollar summary, no currency guess.


@pytest.mark.parametrize(("row", "expected"), [
    ("09/09\n700.00 Online Transfer From Fictional\nxxxx8765", "transfer_reference_missing_or_ambiguous"),
    ("09/09\nOnline Transfer From Fictional xxxx8765 Ref #MissingAmount", "transfer_amount_missing"),
    ("10/01\n700.00 Online Transfer From Fictional xxxx8765 Ref #Outside", "transfer_posted_date_outside_or_unknown_period"),
    ("09/09\n-700.00 Online Transfer From Fictional xxxx8765 Ref #Negative", "transfer_sign_conflict"),
    ("09/09\n700.00 Online Transfer To Fictional xxxx8765 Ref #WrongSection", "transfer_section_conflict"),
    ("09/09\n0.00 Online Transfer From Fictional xxxx8765 Ref #Zero", "transfer_amount_invalid"),
    ("Online Transfer From Fictional xxxx8765 Ref #NoDate 700.00", "unparsed_transfer_candidates"),
])
def test_incomplete_or_contradictory_transfer_candidates_are_not_silently_accepted(row, expected):
    result = parse_wells_fargo_pages([header() + "Credits\n" + row])
    assert not result["coverage"]["complete"]
    assert expected in codes(result)
    assert result["coverage"]["candidate_count"] == 1
    assert result["transfer_lines"] == []


def test_same_amount_same_date_distinct_references_and_physical_pages_are_retained():
    first = header() + "Credits\n09/09\n25.00 Online Transfer From Fictional xxxx8765 Ref #First\n09/09\n25.00 Online Transfer From Fictional xxxx8765 Ref #Second\n"
    second = header() + "Credits\n09/09\n25.00 Online Transfer From Fictional xxxx8765 Ref #Third\n"
    result = parse_wells_fargo_pages([first, second])
    assert result["coverage"]["complete"]
    assert [row["transfer_reference"] for row in result["transfer_lines"]] == ["First", "Second", "Third"]
    assert [row["page"] for row in result["transfer_lines"]] == [1, 1, 2]
    assert len({(row["page"], row["source_text_line"]) for row in result["transfer_lines"]}) == 3


def test_footer_and_next_page_account_header_never_enter_quote_or_counterparty():
    first = header() + "Credits\n09/09\n25.00 Online Transfer From Fictional 666666661234 Ref #123456789012\nPage 1 of 2\nAccount number: 100000004321\n"
    result = parse_wells_fargo_pages([first, header()])
    assert result["coverage"]["complete"]
    row = result["transfer_lines"][0]
    assert row["counterparty_account_last4"] is None
    assert "666666661234" not in row["quote"]
    assert "100000004321" not in row["quote"]
    assert row["transfer_reference"] == "123456789012"
    assert "123456789012" in row["quote"]  # An explicit reference is not an account.


def test_conflicting_header_accounts_and_periods_are_coverage_errors():
    result = parse_wells_fargo_pages([header(), header("August 1, 2026 - August 31, 2026", "100000008765")])
    assert not result["coverage"]["complete"]
    assert result["account_last4"] is None and result["period_start"] is None
    assert {"account_header_conflict", "statement_period_missing_or_conflicting"} <= codes(result)


def test_summary_totals_and_recurring_summary_cannot_create_transfer_rows():
    result = parse_wells_fargo_pages([header() + SUMMARY + "Credits\nRecurring payment summary: 6 transfers total 500.00\n"])
    assert result["coverage"]["complete"]
    assert result["transfer_lines"] == []
    assert result["coverage"]["candidate_count"] == 0


def test_summary_discrepancy_stays_audit_warning_and_cannot_infer_currency():
    result = parse_wells_fargo_pages([header() + SUMMARY.replace("$300.00\n", "$301.00\n", 1)])
    assert not result["summary_audit"][0]["valid"]
    assert result["currency"] is None


def test_no_arbitrary_one_hundred_row_crop():
    rows = "\n".join(f"09/09\n1.00 Online Transfer From Fictional xxxx8765 Ref #Ref{i}" for i in range(120))
    result = parse_wells_fargo_pages([header() + "Credits\n" + rows])
    assert result["coverage"]["complete"]
    assert result["coverage"]["parsed_count"] == 120


def test_pdf_entrypoint_uses_physical_pages_and_hashes_exact_bytes():
    document = fitz.open()
    page = document.new_page()
    page.insert_text((30, 30), header() + "Credits\n09/09\n25.00 Online Transfer From Fictional xxxx8765 Ref #PdfExample", fontsize=8)
    raw = document.tobytes()
    document.close()
    result = parse_wells_fargo_pdf(raw)
    assert result["source_hash"] == hashlib.sha256(raw).hexdigest()
    assert result["coverage"]["complete"]
    assert result["transfer_lines"][0]["page"] == 1


def test_prior_account_number_is_not_the_statement_primary_account():
    text = header() + "Prior account number: 100000009999\nCredits\n09/09\n25.00 Online Transfer From Fictional xxxx8765 Ref #PriorExample"
    result = parse_wells_fargo_pages([text])
    assert result["account_last4"] == "4321"
    assert result["coverage"]["complete"]


def test_large_bare_row_amount_and_blank_separator_are_preserved_in_source_quote():
    text = header() + "Credits\n09/09\n\n100000.00 Online Transfer From Fictional xxxx8765 Ref #LargeExample"
    result = parse_wells_fargo_pages([text])
    assert result["coverage"]["complete"]
    assert result["transfer_lines"][0]["amount"] == 100000
    assert "100000.00" in result["transfer_lines"][0]["quote"]


def test_explicit_foreign_currency_prevents_us_dollar_inference():
    result = parse_wells_fargo_pages([header() + SUMMARY + "Currency: CAD\n"])
    assert result["currency"] is None
    assert "statement_currency_conflict" in codes(result)
