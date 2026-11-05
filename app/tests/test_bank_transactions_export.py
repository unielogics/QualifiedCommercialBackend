"""A bank transactions export (CSV or workbook) reads into the same months a statement does."""

from __future__ import annotations

from app.services import spreadsheets
from app.services.bank_transactions_export import (
    describe,
    find_columns,
    parse_date,
    parse_money,
    read_export,
)

CHASE = """Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #
DEBIT,05/01/2026,KAPITUS DAILY ACH PMT,-350.00,ACH_DEBIT,12650.00,
CREDIT,05/02/2026,SQUARE INC DEPOSIT,2400.55,ACH_CREDIT,15050.55,
DEBIT,05/02/2026,KAPITUS DAILY ACH PMT,-350.00,ACH_DEBIT,14700.55,
DEBIT,05/03/2026,RENT ACME PROPERTIES,-4000.00,ACH_DEBIT,10700.55,
CREDIT,05/09/2026,SQUARE INC DEPOSIT,3100.00,ACH_CREDIT,13800.55,
DEBIT,05/12/2026,KAPITUS DAILY ACH PMT,-350.00,ACH_DEBIT,13450.55,
DEBIT,05/20/2026,CHECK 1042,-1200.00,CHECK_PAID,12250.55,1042
CREDIT,06/01/2026,SQUARE INC DEPOSIT,2900.00,ACH_CREDIT,15150.55,
DEBIT,06/02/2026,KAPITUS DAILY ACH PMT,-350.00,ACH_DEBIT,14800.55,
DEBIT,06/03/2026,RENT ACME PROPERTIES,-4000.00,ACH_DEBIT,10800.55,
DEBIT,06/15/2026,INSUFFICIENT FUNDS FEE,-34.00,FEE,-120.00,
CREDIT,06/16/2026,SQUARE INC DEPOSIT,5000.00,ACH_CREDIT,4880.00,
DEBIT,07/01/2026,KAPITUS DAILY ACH PMT,-350.00,ACH_DEBIT,4530.00,
CREDIT,07/03/2026,SQUARE INC DEPOSIT,3300.00,ACH_CREDIT,7830.00,
DEBIT,07/03/2026,RENT ACME PROPERTIES,-4000.00,ACH_DEBIT,3830.00,
"""

DEBIT_CREDIT = """Account Number: ****5566
Date,Description,Debit,Credit,Balance
2026-03-02,Payroll run,"2,500.00",,"8,200.00"
2026-03-05,Customer wire,,"4,000.00","12,200.00"
2026-03-09,Payroll run,"2,500.00",,"9,700.00"
2026-03-16,Payroll run,"2,500.00",,"7,200.00"
2026-04-01,Customer wire,,"4,000.00","11,200.00"
2026-04-06,Payroll run,"2,500.00",,"8,700.00"
"""


def _workbook(text: str) -> spreadsheets.Workbook:
    return spreadsheets.read_workbook(text.encode("utf-8"), "csv", file_name="export.csv")


def test_movement_lines_keep_original_type_location_currency_and_do_not_change_totals():
    text = "Date,Description,Amount,Type,Currency,Reference,Account\n" + "\n".join(
        f"2026-05-{day:02d},Terse description,{-100 if day % 2 else 150},Internal Transfer,USD,REF{day},XXXX1111"
        for day in range(1, 6)
    )
    facts = read_export(_workbook(text))
    assert facts["transactions"][0]["kind"] == "Internal Transfer"
    assert facts["transactions"][0]["source_row"] == 2 and facts["transactions"][0]["account_last4"] == "1111"
    assert facts["transactions"][0]["currency"] == facts["currency"] == "USD"
    assert facts["total_deposits_and_credits"] == 300 and facts["total_withdrawals_and_debits"] == 300
    assert not facts["transactions_complete"] and not facts["account_identity_ambiguous"]


def test_mixed_account_export_retains_last_four_per_row_and_flags_origin_ambiguity():
    text = "Date,Description,Amount,Type,Account\n" + "\n".join(
        f"2026-05-{day:02d},Transfer,{(-100 if day % 2 else 100)},Transfer,XXXX{1111 if day < 3 else 2222}"
        for day in range(1, 6)
    )
    facts = read_export(_workbook(text))
    assert facts["account_identity_ambiguous"]
    assert {row["account_last4"] for row in facts["transactions"]} == {"1111", "2222"}


def test_positive_amount_with_ambiguous_type_never_guesses_movement_direction():
    text = "Date,Description,Amount,Type\n" + "\n".join(
        f"2026-05-{day:02d},Transfer,100,Transfer" for day in range(1, 6)
    )
    facts = read_export(_workbook(text))
    assert all(row["direction"] is None and "signed_amount" not in row for row in facts["transactions"])


def test_money_and_dates_read_the_way_banks_print_them() -> None:
    assert (
        parse_money("$1,234.56") == 1234.56
        and parse_money("(45.00)") == -45.0
        and parse_money("45.00 CR") == 45.0
    )
    assert (
        parse_money("12.50 DR") == -12.5 and parse_money("") is None and parse_money("n/a") is None
    )
    assert (
        parse_date("05/01/2026").isoformat() == "2026-05-01"
        and parse_date("2026-05-01T10:00:00").isoformat() == "2026-05-01"
    )
    assert (
        parse_date("May 1, 2026").isoformat() == "2026-05-01" and parse_date("not a date") is None
    )


def test_a_chase_style_export_becomes_statement_months() -> None:
    facts = read_export(_workbook(CHASE), file_name="Chase1204_Activity.CSV")
    assert (
        facts is not None
        and facts["source"] == "transactions_export"
        and facts["transactions_read"] == 15
    )
    months = facts["months"]
    assert [m["month"] for m in months] == ["2026-05", "2026-06", "2026-07"]
    may = months[0]
    assert may["statement_period"] == "2026-05-01 to 2026-05-20"
    assert (
        may["total_deposits_and_credits"] == 5500.55
        and may["total_withdrawals_and_debits"] == 6250.0
    )
    assert (
        may["beginning_balance"] == 13000.0 and may["ending_balance"] == 12250.55
    )  # the running balance column
    assert may["number_of_deposits"] == 2 and may["number_of_checks"] == 1
    june = months[1]
    assert june["nsf_or_overdraft_count"] == 1 and june["negative_balance_dates"] == ["2026-06-15"]
    assert {i["type"] for i in june["overdraft_items"]} == {"nsf_fee", "negative_balance"}
    kapitus = next(r for r in facts["recurring_debits"] if "KAPITUS" in r["payee"])
    assert (
        kapitus["occurrences"] == 5
        and kapitus["kind_guess"] == "merchant_cash_advance"
        and kapitus["same_amount"] is True
    )
    rent = next(r for r in facts["recurring_debits"] if "RENT" in r["payee"])
    assert rent["cadence"] == "monthly" and rent["total_amount"] == 12000.0
    square = next(r for r in facts["recurring_credits"] if "SQUARE" in r["payee"])
    assert square["occurrences"] == 5 and square["kind_guess"] == "card_settlement"
    assert facts["account_last4"] == "1204"  # from the file name
    assert facts["total_deposits_and_credits"] == 16700.55 and facts["low_daily_balance"] == -120.0
    assert "15 transactions over 3 months (2026-05 to 2026-07)" in describe(facts)


def test_a_debit_credit_export_with_the_account_above_the_header_is_read() -> None:
    facts = read_export(_workbook(DEBIT_CREDIT), file_name="activity.csv")
    assert facts is not None and facts["account_last4"] == "5566"
    assert [m["month"] for m in facts["months"]] == ["2026-03", "2026-04"]
    march = facts["months"][0]
    assert (
        march["total_withdrawals_and_debits"] == 7500.0
        and march["total_deposits_and_credits"] == 4000.0
    )
    assert march["beginning_balance"] == 10700.0 and march["ending_balance"] == 7200.0
    payroll = next(r for r in facts["recurring_debits"] if "Payroll" in r["payee"])
    assert payroll["cadence"] == "weekly" and payroll["kind_guess"] == "payroll"


def test_a_table_that_is_not_transactions_is_left_to_the_model() -> None:
    assert (
        read_export(_workbook("Lender,Balance,Payment\nOnDeck,12000,350\nKapitus,8000,200\n"))
        is None
    )
    assert find_columns([["Vendor", "Amount"], ["x", "1"]]) is None
    too_short = "Date,Description,Amount\n05/01/2026,a,-1\n05/02/2026,b,2\n"
    assert read_export(_workbook(too_short)) is None
    unsigned = "Date,Description,Amount\n" + "\n".join(
        f"05/0{i}/2026,row {i},{i * 10}" for i in range(1, 8)
    )
    assert (
        read_export(_workbook(unsigned)) is None
    )  # every amount positive and no type column: unreadable safely


def test_the_analysis_row_takes_the_computed_months_and_reads_as_a_statement() -> None:
    from types import SimpleNamespace

    from app.services.bucket_ai import _apply_transactions_export, _transactions_export_facts

    file = SimpleNamespace(id="f1", file_name="Chase1204_Activity.CSV")
    facts = _transactions_export_facts(file, CHASE.encode("utf-8"), "text/csv")
    assert facts and len(facts["months"]) == 3
    row = SimpleNamespace(
        classification="other",
        analysis={
            "key_facts": {"bank": "JPMorgan Chase", "months": [{"statement_period": "wrong"}]},
            "limitations": ["x"],
            "read_warnings": [],
        },
    )
    _apply_transactions_export(row, facts)
    assert row.classification == "bank_statement"
    assert row.analysis["key_facts"]["bank"] == "JPMorgan Chase"  # the model's other facts stay
    assert [m["month"] for m in row.analysis["key_facts"]["months"]] == [
        "2026-05",
        "2026-06",
        "2026-07",
    ]  # the computed months win
    assert row.analysis["transactions_export"]["transactions_read"] == 15 and any(
        "computed from the rows" in note for note in row.analysis["limitations"]
    )
    assert _transactions_export_facts(file, b"%PDF-1.4 not a sheet", "application/pdf") is None
    kept = SimpleNamespace(
        classification="debt_schedule",
        analysis={"key_facts": {}, "limitations": [], "read_warnings": []},
    )
    _apply_transactions_export(kept, facts)
    assert (
        kept.classification == "debt_schedule"
    )  # a model that saw something else keeps its word; the months still land
