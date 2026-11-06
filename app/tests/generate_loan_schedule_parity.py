"""Generate fictional Python-engine expectations for the browser's parity gate."""

import json
import sys
from pathlib import Path

from app.services.loan_schedule import (
    build_schedule,
    effective_rate_pct,
    risk_profile,
    schedule_cash_flows,
)


def balloon(identifier, month, value, kind="amount"):
    return {"id": identifier, "month": month, "type": kind, "value": value}


CASES = {
    "legacy-simple": {"amount": 200000, "rate_pct": 12, "term_months": 24, "interest_only_months": 6},
    "legacy-amortizing": {"amount": 100000, "rate_pct": 10, "term_months": 60},
    "legacy-interest-only": {"amount": 100000, "rate_pct": 12, "term_months": 12, "structure": "interest_only"},
    "legacy-percent-ties": {"amount": 10000, "rate_pct": 0, "term_months": 16, "structure": "simple_interest"},
    "legacy-custom-cadence": {"amount": 10000, "rate_pct": 6, "term_months": 24, "periods_per_year": 32},
    "extended-amortization": {"amount": 100000, "rate_pct": 6, "term_months": 60, "structure": "amortizing", "repayment_config": {"amortization_months": 360}},
    "ordered-same-month": {"amount": 100000, "rate_pct": 12, "term_months": 12, "structure": "interest_only", "repayment_config": {"balloons": [balloon("fixed", 3, 20000), balloon("percent", 3, 50, "percent_balance")]}},
    "recast-after-io": {"amount": 100000, "rate_pct": 6, "term_months": 60, "structure": "amortizing", "interest_only_months": 12, "repayment_config": {"amortization_months": 360, "after_balloon": "recast", "balloons": [balloon("io", 6, 20000), balloon("later", 24, 10, "percent_balance")]}},
    "keep-payment": {"amount": 100000, "rate_pct": 6, "term_months": 60, "structure": "amortizing", "repayment_config": {"amortization_months": 360, "balloons": [balloon("partial", 12, 10000)]}},
    "simple-flat-recast": {"amount": 10000, "rate_pct": 12, "term_months": 24, "structure": "simple_interest", "interest_only_months": 3, "repayment_config": {"amortization_months": 36, "after_balloon": "recast", "balloons": [balloon("partial", 6, 1000)]}},
    "maturity-overlap": {"amount": 100000, "rate_pct": 0, "term_months": 12, "structure": "interest_only", "repayment_config": {"balloons": [balloon("fixed", 12, 20000), balloon("percent", 12, 50, "percent_balance")]}},
    "zero-rate-early-payoff": {"amount": 10000, "rate_pct": 0, "term_months": 12, "structure": "interest_only", "repayment_config": {"balloons": [balloon("all", 2, 100, "percent_balance")]}},
    "small-cents": {"amount": 100.01, "rate_pct": 7.5, "term_months": 12, "structure": "amortizing", "repayment_config": {"amortization_months": 24, "after_balloon": "recast", "balloons": [balloon("cent", 3, .01), balloon("percent", 4, 13.333, "percent_balance")]}},
    "weekly": {"amount": 52000, "rate_pct": 10, "term_months": 24, "structure": "amortizing", "periods_per_year": 52, "repayment_config": {"amortization_months": 120, "balloons": [balloon("first", 1, 500), balloon("second", 12, 10, "percent_balance")]}},
    "fractional-weekly": {"amount": 52000, "rate_pct": 10, "term_months": 24, "structure": "amortizing", "periods_per_year": 51.96, "repayment_config": {"amortization_months": 120, "after_balloon": "recast", "balloons": [balloon("first", 1, 500), balloon("second", 12, 10, "percent_balance")]}},
    "long-weekly": {"amount": 100000, "rate_pct": 8, "term_months": 600, "structure": "amortizing", "periods_per_year": 52, "repayment_config": {"amortization_months": 1200, "balloons": [balloon("year10", 120, 10, "percent_balance")]}},
    "long-daily": {"amount": 100000, "rate_pct": 8, "term_months": 600, "structure": "amortizing", "periods_per_year": 252, "repayment_config": {"amortization_months": 1200, "after_balloon": "recast", "balloons": [balloon("year10", 120, 10, "percent_balance")]}},
}


def fixture():
    output = {}
    for name, inputs in CASES.items():
        summary = build_schedule(**inputs)
        rows = summary["rows"]
        samples = sorted({0, len(rows) // 2, len(rows) - 1, *[max(0, event["period"] - 1) for event in summary["scheduled_balloons"]]})
        risk = risk_profile(summary, income_monthly=15000, existing_service_monthly=1000)
        output[name] = {"input": inputs, "summary": {key: value for key, value in summary.items() if key not in {"rows", "cash_flows"}},
                        "rows": [{"index": index, "row": rows[index]} for index in samples],
                        "risk": {key: value for key, value in risk.items() if key not in {"coverage_by_period", "cash_coverage_by_period", "words"}},
                        "effective_rate_pct": effective_rate_pct(inputs["amount"], schedule_cash_flows(summary), periods_per_year=summary["periods_per_year"])}
    return output


if __name__ == "__main__":
    target = Path(sys.argv[1])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(fixture(), indent=2), encoding="utf-8")
