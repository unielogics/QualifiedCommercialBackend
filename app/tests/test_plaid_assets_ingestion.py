from __future__ import annotations

from app.dealer_os.services.plaid_assets import (
    _merge_document_evidence_month,
    normalize_asset_report,
)


def test_asset_report_normalizes_balances_transactions_and_months():
    payload = {
        "report": {
            "items": [
                {
                    "institution_name": "Example Bank",
                    "accounts": [
                        {
                            "name": "Business Checking",
                            "official_name": "Operating Account",
                            "mask": "4812",
                            "type": "depository",
                            "subtype": "checking",
                            "balances": {"current": 5100.0},
                            "historical_balances": [
                                {"date": "2026-06-01", "current": 1000.0},
                                {"date": "2026-06-15", "current": -25.0},
                                {"date": "2026-06-30", "current": 2500.0},
                                {"date": "2026-07-01", "current": 2500.0},
                                {"date": "2026-07-31", "current": 5100.0},
                            ],
                            "transactions": [
                                {
                                    "date": "2026-06-03",
                                    "name": "Customer deposit",
                                    "amount": -4000.0,
                                },
                                {
                                    "date": "2026-06-10",
                                    "original_description": "RENT PAYMENT",
                                    "amount": 1500.0,
                                },
                                {
                                    "date": "2026-06-16",
                                    "name": "Overdraft fee",
                                    "amount": 35.0,
                                },
                                {
                                    "date": "2026-07-08",
                                    "name": "Card sales",
                                    "amount": -2600.0,
                                },
                            ],
                        }
                    ],
                }
            ]
        }
    }

    rows = normalize_asset_report(payload)

    assert len(rows) == 1
    row = rows[0]
    assert row["account"] == {
        "institution": "Example Bank",
        "name_hint": "Operating Account",
        "mask": "4812",
        "kind_hint": "checking",
    }
    assert [transaction["amount"] for transaction in row["transactions"]] == [
        4000.0,
        -1500.0,
        -35.0,
        2600.0,
    ]

    june, july = row["months"]
    assert june["month"] == "2026-06"
    assert june["total_deposits"] == 4000.0
    assert june["total_withdrawals"] == 1535.0
    assert june["beginning_balance"] == 1000.0
    assert june["ending_balance"] == 2500.0
    assert june["average_ledger_balance"] == 1158.33
    assert june["low_daily_balance"] == -25.0
    assert june["negative_balance_dates"] == ["2026-06-15"]
    assert june["nsf_count"] == 1
    assert july["month"] == "2026-07"
    assert july["total_deposits"] == 2600.0
    assert july["total_withdrawals"] == 0


def test_asset_report_keeps_accounts_separate():
    payload = {
        "items": [
            {
                "institution_name": "Example Bank",
                "accounts": [
                    {
                        "name": "Checking",
                        "mask": "1111",
                        "balances": {"current": 100.0},
                        "transactions": [],
                        "historical_balances": [],
                    },
                    {
                        "name": "Savings",
                        "mask": "2222",
                        "balances": {"current": 900.0},
                        "transactions": [],
                        "historical_balances": [],
                    },
                ],
            }
        ]
    }

    rows = normalize_asset_report(payload)

    assert [row["account"]["mask"] for row in rows] == ["1111", "2222"]
    assert [row["months"][0]["ending_balance"] for row in rows] == [100.0, 900.0]


def test_asset_report_document_month_keeps_risk_evidence_from_every_account():
    first = {
        "month": "2026-07",
        "nsf_count": 1,
        "negative_balance_dates": ["2026-07-08"],
    }
    second = {
        "month": "2026-07",
        "nsf_count": 2,
        "negative_balance_dates": ["2026-07-19", "2026-07-08"],
    }

    merged = _merge_document_evidence_month(first, second)

    assert merged["nsf_count"] == 3
    assert merged["negative_balance_dates"] == ["2026-07-08", "2026-07-19"]
    assert merged["negative_balance_days"] == 2
    assert first["nsf_count"] == 1
