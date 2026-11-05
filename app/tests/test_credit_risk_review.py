from datetime import date

import pytest

from app.services.credit_risk_review import analyze_report


def review(**fields):
    return analyze_report({"trade_accounts": [{"fields": fields}]}, as_of=date(2026, 11, 5))


def codes(result):
    return {f["code"] for f in result["flags"]}


def test_current_delinquency_has_traceable_evidence():
    result = review(company="Example creditor", past_due="$125.00", account_status="30 days late")
    flag = next(f for f in result["flags"] if f["code"] == "past_due")
    assert flag["source"] == "trade_accounts[0]"
    assert flag["evidence"]["past_due"] == "$125.00"
    assert result["high_count"] == 1


@pytest.mark.parametrize("when,recent", [("10/01/2026", True), ("01/01/2020", False), ("01/01/2027", False)])
def test_recency_uses_delinquency_date_not_last_update(when, recent):
    result = review(max_delinquency_date=when, account_rating="Previously late", date_reported="11/05/2026")
    assert ("recent_late" in codes(result)) == recent


def test_recent_account_update_never_implies_recent_late_payment():
    result = review(date_reported="11/05/2026", account_status="Current", account_rating="Paid as agreed")
    assert not result["flags"]
    assert result["limitations"]


def test_negated_and_zero_balances_are_not_bad_flags():
    result = review(past_due="$0", account_status="Not delinquent", account_rating="No late payments")
    assert not result["flags"]


def test_paid_collection_is_not_described_as_outstanding():
    result = analyze_report({"collections": [{"balance": "0", "status": "Paid"}]})
    assert result["flags"][0]["severity"] == "warning"
    assert "does not establish" in result["flags"][0]["detail"]


def test_bankruptcy_does_not_trigger_from_a_creditor_name_or_negation():
    result = analyze_report({"public_records": [{"name": "Bankruptcy Services", "type": "No bankruptcy"}]})
    assert not result["flags"]
    result = analyze_report({"public_records": [{"type": "Chapter 7 Bankruptcy", "status": "Discharged", "filing_date": "01/01/2020"}]})
    assert result["flags"][0]["code"] == "bankruptcy"
    assert "does not mean an active" in result["flags"][0]["detail"]
    assert "bankruptcy" in codes(analyze_report({"public_records": [{"chapter": "13", "status": "Discharged"}]}))


def test_high_utilization_is_explicitly_advisory():
    result = review(balance="800", credit_limit="1000")
    flag = result["flags"][0]
    assert flag["code"] == "utilization" and flag["evidence"]["utilization_pct"] == 80
    assert "not an approval or denial" in flag["detail"]


def test_unavailable_report_is_limited_not_a_clean_bill_of_health():
    result = analyze_report({})
    assert result["level"] == "limited" and result["limitations"]
