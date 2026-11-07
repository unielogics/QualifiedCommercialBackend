"""Read-only loan balance reduction, independent of payment or pricing rules."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from math import isfinite
from typing import Any

PAID_DOWN_HINT = (
    "100 × (original amount − current balance) ÷ original amount. "
    "Balance reduction, not cumulative payments or fees. A negative percentage "
    "means the current balance is above the original amount."
)
ORIGINATION_KEYS = ("originated_on", "origination_date", "originated_date", "origin_date")


def finite_amount(value: Any) -> Decimal | None:
    """Read the form's money syntax without accepting partial or nonfinite numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        return None
    text = re.sub(r"[,$\s]", "", str(value))
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1].lstrip("-")
    if re.fullmatch(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", text) is None:
        return None
    try:
        result = Decimal(text)
    except (InvalidOperation, TypeError, ValueError):
        return None
    try:
        return result if result.is_finite() and isfinite(float(result)) else None
    except (ValueError, OverflowError):
        return None


def paid_down_pct(original_amount: Any, current_balance: Any) -> float | None:
    """Percentage points (0–100 normally), preserving balances above original as negative.

    The two inputs must describe the same loan. Payoff quotes, credit limits,
    factor rates and payment history are deliberately not inputs.
    """
    original, balance = finite_amount(original_amount), finite_amount(current_balance)
    if original is None or balance is None or original <= 0 or balance < 0:
        return None
    try:
        with localcontext() as context:
            context.prec = 50
            value = (original - balance) / original * 100
        result = float(value)
        return result if result not in (float("inf"), float("-inf")) else None
    except (InvalidOperation, OverflowError, ZeroDivisionError):
        return None


def format_paid_down_pct(value: Any) -> str:
    """Human display of percentage points, with the same half-up rounding as money."""
    number = finite_amount(value)
    if number is None:
        return "—"
    try:
        rounded = number.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return "—"
    return f"{abs(rounded) if rounded == 0 else rounded:.1f}%"


def originated_on(row: Mapping[str, Any]) -> str | None:
    """Only an explicit recorded origination date, including historical aliases."""
    value = row["originated_on"] if "originated_on" in row else next(
        (row[key] for key in ORIGINATION_KEYS[1:] if row.get(key) not in (None, "")), None
    )
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except (TypeError, ValueError):
        return None


def loan_progress_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    """Derived read model; never changes the recorded original amount or date."""
    original = finite_amount(row.get("original_amount"))
    balance = row.get("balance") if row.get("balance") is not None else row.get("current_balance")
    return {
        "original_amount": float(original) if original is not None and original >= 0 else None,
        "originated_on": originated_on(row),
        "paid_down_pct": paid_down_pct(original, balance),
    }
