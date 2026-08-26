from __future__ import annotations

from datetime import date

from app.dealer_os.services.buckets_link import adapt_analysis_to_extraction
from app.dealer_os.services.extract import apply_extraction


def test_statement_beginning_balance_reaches_normalized_period() -> None:
    plan = apply_extraction(
        {
            "months": [
                {
                    "month": "2026-07",
                    "beginning_balance": 12500.25,
                    "ending_balance": 18750.75,
                    "average_ledger_balance": 14900.50,
                    "total_deposits": 52000,
                    "total_withdrawals": 45749.50,
                }
            ],
            "transactions": [],
        }
    )

    period = plan["period_upserts"][date(2026, 7, 1)]
    assert period["starting_balance"] == 12500.25
    assert period["ending_balance"] == 18750.75
    assert period["avg_daily_balance"] == 14900.50
    assert plan["months"][0]["beginning_balance"] == 12500.25


def test_bucket_analysis_preserves_beginning_balance() -> None:
    extraction = adapt_analysis_to_extraction(
        {
            "key_facts": {
                "months": [
                    {
                        "statement_period": "2026-06-01 to 2026-06-30",
                        "beginning_balance": "$9,250.00",
                        "ending_balance": "$11,100.00",
                        "average_ledger_balance": "$10,400.00",
                    }
                ]
            }
        }
    )

    assert extraction["months"][0]["beginning_balance"] == 9250.0
    assert extraction["months"][0]["ending_balance"] == 11100.0
