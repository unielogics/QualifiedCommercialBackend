"""Pure helpers for the Field Desk application workflow.

The router gathers persisted facts; this module turns them into one ordered
readiness contract. UI components must render this contract rather than
recreating progression rules locally.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


FINANCIAL_CONFIRMATION_FIELDS = (
    "annual_sales",
    "annual_cash_flow_available_for_debt",
    "monthly_debt_payments",
)


def debt_source_hash(rows: Iterable[Any]) -> str:
    payload = []
    for row in rows:
        payload.append(
            {
                "id": str(getattr(row, "id", "")),
                "lender": getattr(row, "lender", None),
                "category": getattr(row, "category", None),
                "balance": _number(getattr(row, "balance", None)),
                "payment_amount": _number(getattr(row, "payment_amount", None)),
                "payment_frequency": getattr(row, "payment_frequency", None),
                "monthly_payment": _number(getattr(row, "monthly_payment", None)),
                "rate": _number(getattr(row, "rate", None)),
                "factor_rate": _number(getattr(row, "factor_rate", None)),
                "maturity_on": str(getattr(row, "maturity_on", None) or ""),
                "payoff_amount": _number(getattr(row, "payoff_amount", None)),
                "collateral": getattr(row, "collateral", None),
                "count_in_dscr": bool(getattr(row, "count_in_dscr", False)),
                "status": getattr(row, "status", None),
            }
        )
    payload.sort(key=lambda item: item["id"])
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def financial_confirmation_blockers(profile: Any | None) -> list[str]:
    if profile is None:
        return [
            "Confirm annual sales.",
            "Confirm annual cash flow available for debt.",
            "Confirm monthly debt payments.",
        ]
    confirmations = dict(getattr(profile, "field_confirmations", None) or {})
    labels = {
        "annual_sales": "annual sales",
        "annual_cash_flow_available_for_debt": "annual cash flow available for debt",
        "monthly_debt_payments": "monthly debt payments",
    }
    blockers = []
    for field in FINANCIAL_CONFIRMATION_FIELDS:
        if getattr(profile, field, None) is None or field not in confirmations:
            blockers.append(f"Confirm {labels[field]}.")
    return blockers


def build_workflow(
    *,
    workflow_ungated: bool,
    step_1_blockers: list[str],
    step_2_blockers: list[str],
    step_3_blockers: list[str],
    step_4_blockers: list[str],
    step_2_warnings: list[str] | None = None,
    step_4_warnings: list[str] | None = None,
    program_selection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    step_1_complete = not step_1_blockers
    step_2_complete = step_1_complete and not step_2_blockers
    step_3_complete = step_2_complete and not step_3_blockers
    step_4_complete = step_3_complete and not step_4_blockers

    return {
        "workflow_ungated": workflow_ungated,
        "step_1": {
            "available": True,
            "complete": step_1_complete,
            "blockers": step_1_blockers,
            "warnings": [],
        },
        "step_2": {
            "available": workflow_ungated or step_1_complete,
            "complete": step_2_complete,
            "blockers": step_2_blockers,
            "warnings": list(step_2_warnings or []),
        },
        "step_3": {
            "available": workflow_ungated or step_2_complete,
            "complete": step_3_complete,
            "blockers": step_3_blockers,
            "warnings": [],
        },
        "step_4": {
            "available": workflow_ungated or step_3_complete,
            "complete": step_4_complete,
            "blockers": step_4_blockers,
            "warnings": list(step_4_warnings or []),
        },
        "program_selection": program_selection or {},
    }


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
