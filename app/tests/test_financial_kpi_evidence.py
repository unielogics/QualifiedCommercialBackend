from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services import financial_fact_citations
from app.tests.test_financial_metric_review import build, correction, file, pnl, target, tax


def banks():
    return [file("bank_statement", {"bank": "Example Bank", "account_last4": "1234", "currency": "USD", "business_name": "Example LLC",
                 "statement_period": f"2026-0{month}", "total_deposits_and_credits": 10000 + month * 100,
                 "total_withdrawals_and_debits": 8000, "average_ledger_balance": 1500 + month * 100,
                 "ending_balance": 1800 + month * 100, **({"nsf_or_overdraft_count": 2} if month == 1 else {})}, key=f"bank-{month}") for month in (1, 2, 3)]


def test_every_kpi_has_an_explicit_formula_source_scope_and_actual_operand_keys():
    c = build([*banks(), tax(), pnl()])
    calculations = {row["key"]: row for row in c["metric_review"]["calculations"]}
    expected = {"bank.deposits", "bank.net_cash_flow", "bank.average_balance", "bank.nsf_overdrafts",
                "dscr.tax_return", "profit_and_loss", "net_profit.tax_return"}
    assert expected <= set(calculations)
    for key in expected:
        row = calculations[key]
        assert row["formula"] and row["input_keys"] and row["source_refs"]
    assert calculations["bank.deposits"]["value"] == 10200
    assert calculations["bank.net_cash_flow"]["value"] == 2200
    assert calculations["bank.average_balance"]["value"] == 1700
    nsf = calculations["bank.nsf_overdrafts"]
    assert nsf["value"] == 2 and not nsf["details"]["complete"]
    assert nsf["details"]["reported_months"] == 1 and nsf["details"]["total_months"] == 3
    assert calculations["net_profit.tax_return"]["input_keys"] == ["tax_return:tax:2025.income"]
    assert calculations["bank.average_balance"]["account_scope"]["kind"] == "counted_accounts"


def test_reviewed_bank_aggregate_has_original_raw_mean_and_unmodified_historical_months():
    analyses = banks()
    original = deepcopy(analyses)
    c = build(analyses)
    state = correction(c, "bank.avg_deposits", 12000)
    updated = build(analyses, state)
    assert updated["bank"]["summary"]["avg_deposits"] == 12000
    assert updated["bank"]["summary"]["annualized_deposits"] == 144000
    assert updated["bank"]["source_summary"]["avg_deposits"] == 10200
    assert [month["deposits"] for month in updated["bank"]["months"]] == [10100, 10200, 10300]
    assert target(updated, "bank.avg_deposits")["original_value"] == 10200
    receipt = next(row for row in updated["metric_review"]["calculations"] if row["key"] == "bank.deposits")
    assert receipt["value"] == 12000 and receipt["source_value"] == 10200
    assert receipt["details"]["reviewed_aggregate"]
    assert receipt["formula"].startswith("staff-reviewed aggregate input")
    assert receipt["base_formula"].startswith("sum of known monthly deposits")
    assert analyses == original


def test_reviewed_incident_aggregate_retains_the_incomplete_historical_count_and_file_periods():
    analyses = banks()
    analyses[0]["key_facts"].update(period_start="2026-01-01", period_end="2026-01-31")
    current = build(analyses)
    updated = build(analyses, correction(current, "bank.nsf_total", 5))
    receipt = next(row for row in updated["metric_review"]["calculations"] if row["key"] == "bank.nsf_overdrafts")
    assert receipt["value"] == 5 and receipt["source_value"] == 2
    assert receipt["details"]["reviewed_aggregate"]
    assert not receipt["details"]["complete"] and not receipt["details"]["source_complete"]
    source = next(row for row in receipt["source_refs"] if row["file_id"] == "bank-1")
    assert source["period_start"] == "2026-01-01" and source["period_end"] == "2026-01-31"
    assert source["statement_period"] == "2026-01"
    other = next(row for row in receipt["source_refs"] if row["file_id"] == "bank-2")
    assert other["period_start"] is None and other["statement_period"] == "2026-02"


@pytest.mark.parametrize("key,value", [("bank.avg_deposits", -1), ("bank.avg_withdrawals", -1), ("bank.nsf_total", -1), ("bank.nsf_total", 1.5)])
def test_deposit_and_incident_review_ranges_remain_valid(key, value):
    c = build(banks())
    with pytest.raises(HTTPException):
        correction(c, key, value)


def test_unknown_incident_count_is_not_an_invented_zero_and_negative_balance_is_valid():
    sources = banks()
    for source in sources:
        source["key_facts"].pop("nsf_or_overdraft_count", None)
    c = build(sources)
    assert target(c, "bank.nsf_total")["value"] is None
    assert c["bank"]["summary"]["nsf_total"] is None
    updated = build(sources, correction(c, "bank.average_balance", -100))
    assert updated["bank"]["summary"]["average_balance"] == -100
    assert updated["bank"]["source_summary"]["average_balance"] == 1700


def test_validated_bank_pdf_citations_reach_aggregate_input_and_calculation_receipt():
    facts = {"business_name": "Example LLC", "bank": "Example Bank", "account_last4": "1234", "currency": "USD",
             "statement_period": "2026-01", "total_deposits_and_credits": 10000, "total_withdrawals_and_debits": 8000,
             "nsf_or_overdraft_count": 2}
    pages = {1: "Total deposits and credits $10,000. Total withdrawals and debits $8,000. NSF and overdraft items 2."}
    facts["fact_citations"] = {key: [{"page": 1, "line_label": label, "quote": quote, "value": value, "measure": key}]
        for key, label, quote, value in [
            ("total_deposits_and_credits", "Total deposits and credits", "Total deposits and credits $10,000", 10000),
            ("total_withdrawals_and_debits", "Total withdrawals and debits", "Total withdrawals and debits $8,000", 8000),
            ("nsf_or_overdraft_count", "NSF and overdraft items", "NSF and overdraft items 2", 2)]}
    row = SimpleNamespace(id=uuid4(), bucket_file_id=uuid4(), content_hash="a" * 64, status="completed", analysis={"key_facts": facts})
    manifest = financial_fact_citations.build_financial_citation_manifest(row, source_pages=pages, source_kind="pdf_text", source_hash=row.content_hash)
    source = {"file_id": str(row.bucket_file_id), "analysis_id": str(row.id), "content_hash": row.content_hash,
              "file_name": "exact-bank.pdf", "classification": "bank_statement", "accepted": True,
              "key_facts": facts, "financial_fact_citations": manifest}
    c = build([source])
    ref = target(c, "bank.avg_net")["source_refs"][0]
    assert ref["pages"] == [1]
    assert {citation["field"] for citation in ref["citations"]} == {"total_deposits_and_credits", "total_withdrawals_and_debits"}
    nsf = next(calculation for calculation in c["metric_review"]["calculations"] if calculation["key"] == "bank.nsf_overdrafts")
    assert nsf["source_refs"][0]["citations"][0]["quote"] == "NSF and overdraft items 2"


def test_account_scope_change_stales_bank_aggregate_review():
    analyses = [*banks(), file("bank_statement", {"bank": "Other Bank", "account_last4": "9999", "currency": "USD", "statement_period": "2026-01",
                                             "total_deposits_and_credits": 2000, "total_withdrawals_and_debits": 1000}, key="other")]
    c = build(analyses)
    state = correction(c, "bank.avg_net", 9000)
    account = next(row["key"] for row in c["bank"]["accounts"] if "1234" in row["label"])
    updated = build(analyses, state, counted_accounts=[account])
    assert updated["bank"]["summary"]["avg_net"] != 9000
    assert target(updated, "bank.avg_net")["correction"] is None
    assert updated["metric_review"]["stale_corrections"]
