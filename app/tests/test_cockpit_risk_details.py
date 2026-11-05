"""Risk visuals retain the account and statement behind every located event."""

from app.services.financial_cockpit import bank_months, bank_summary


def statement(file_id: str, last4: str, **facts) -> dict:
    return {
        "file_id": file_id, "file_name": f"{file_id}.pdf", "accepted": True,
        "classification": "bank_statement",
        "key_facts": {
            "bank": "Chase", "account_last4": last4,
            "statement_period": "2026-05-01 to 2026-05-31", **facts,
        },
    }


def test_simultaneous_negative_dates_keep_account_day_counts_and_sources():
    months, accounts = bank_months([
        statement("checking", "1234", negative_balance_dates=["2026-05-12", "2026-05-13"],
                  overdraft_items=[{"date": "2026-05-12", "type": "overdraft_fee", "amount": 34, "page": 3}]),
        statement("savings", "7777", negative_balance_dates=["2026-05-12"]),
    ])
    assert months[0]["negative_dates"] == ["2026-05-12", "2026-05-13"]
    assert months[0]["negative_days"] == 3  # Two accounts can be negative on the same day.
    assert bank_summary(months)["negative_days_total"] == 3
    assert [a["series"][0]["negative_days"] for a in accounts] == [2, 1]
    assert months[0]["risk_sources"][1]["account"] == "Chase ••7777"
    assert months[0]["risk_sources"][1]["negative_dates"] == ["2026-05-12"]
    event = months[0]["overdraft_items"][0]
    assert (event["file_id"], event["file_name"], event["page"]) == ("checking", "checking.pdf", 3)


def test_missing_risk_fields_stay_unknown_and_same_statement_is_not_counted_twice():
    known = statement("known", "1234", negative_balance_dates=["2026-05-12"])
    unknown = statement("unknown", "7777")
    months, accounts = bank_months([known, known, unknown])
    assert months[0]["negative_days"] == 1
    assert len(months[0]["risk_sources"]) == 2
    assert months[0]["risk_sources"][1]["negative_days"] is None
    assert accounts[1]["series"][0]["negative_days"] is None
    assert months[0]["fees"] is None
    narrowed, _ = bank_months([known, unknown], counted=["chase ••7777"])
    assert narrowed[0]["negative_days"] is None
    assert [s["file_id"] for s in narrowed[0]["risk_sources"]] == ["unknown"]


def test_undated_reported_count_is_not_replaced_with_another_accounts_dates():
    months, _ = bank_months([
        statement("dated", "1234", negative_balance_dates=["2026-05-12"]),
        statement("count_only", "7777", negative_balance_dates=4),
    ])
    assert months[0]["negative_days"] == 5
    assert months[0]["negative_dates"] == ["2026-05-12"]
    assert months[0]["risk_sources"][1]["negative_days"] == 4
