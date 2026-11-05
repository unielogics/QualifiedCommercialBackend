"""Native debt terms and their monthly cash-payment equivalent.

The multipliers deliberately match the refinance workbench's business-day
convention. A factor rate is a separate contract term and never an APR.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from app.dealer_os.services.refinance import FREQUENCY_MONTHLY_MULT

PAYMENT_FREQUENCIES = tuple(FREQUENCY_MONTHLY_MULT)
MONEY_QUANTUM = Decimal("0.01")
RATE_QUANTUM = Decimal("0.001")
SOURCE_NATIVE = "native_terms"
SOURCE_STATED = "stated"


class DebtTermsError(ValueError):
    pass


def _get(row: Any, key: str, default=None):
    return row.get(key, default) if isinstance(row, Mapping) else getattr(row, key, default)


def _decimal(value: Any, *, field: str, quantum: Decimal, nonnegative: bool = False) -> Decimal | None:
    if value is None or isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, bool) or isinstance(value, (dict, list)):
        raise DebtTermsError(f"Enter a valid {field}")
    text = re.sub(r"[,$\s]", "", str(value))
    if field == "factor_rate":
        text = text.rstrip("xX×")
    elif field == "rate":
        text = text.rstrip("%")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1].lstrip("-")
    try:
        number = Decimal(text)
        limit = Decimal("1000") if quantum == RATE_QUANTUM else Decimal("1000000000000")
        if not number.is_finite() or abs(number) >= limit or nonnegative and number < 0:
            raise InvalidOperation
        rounded = number.quantize(quantum, rounding=ROUND_HALF_UP)
        if rounded != number:
            raise DebtTermsError(f"{field} supports at most {abs(quantum.as_tuple().exponent)} decimal places")
        if field == "factor_rate" and rounded <= 0:
            raise DebtTermsError("Factor rate must be greater than zero")
        return rounded
    except (InvalidOperation, ValueError) as error:
        if isinstance(error, DebtTermsError):
            raise
        raise DebtTermsError(f"Enter a valid {field}") from error


def payment_frequency(value: Any) -> str | None:
    if value is None or isinstance(value, str) and not value.strip():
        return None
    if not isinstance(value, str) or value not in PAYMENT_FREQUENCIES:
        raise DebtTermsError("Payment frequency must be daily, weekly, biweekly or monthly")
    return value


def monthly_equivalent(amount: Any, frequency: Any) -> Decimal | None:
    amount = _decimal(amount, field="payment_amount", quantum=MONEY_QUANTUM, nonnegative=True)
    frequency = payment_frequency(frequency)
    if amount is None or frequency is None:
        return None
    return (amount * Decimal(str(FREQUENCY_MONTHLY_MULT[frequency]))).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def monthly_source(row: Any) -> str | None:
    """Recover source from a draft marker or canonical evidence metadata."""
    marker = _get(row, "monthly_payment_source")
    if marker not in {SOURCE_NATIVE, SOURCE_STATED}:
        evidence = _get(row, "evidence")
        marker = evidence.get("monthly_payment_source") if isinstance(evidence, dict) else None
    monthly = _decimal(_get(row, "monthly_payment"), field="monthly_payment", quantum=MONEY_QUANTUM, nonnegative=True)
    if monthly is None:
        return None
    if marker in {SOURCE_NATIVE, SOURCE_STATED}:
        return marker
    equivalent = monthly_equivalent(_get(row, "payment_amount"), _get(row, "payment_frequency"))
    return SOURCE_NATIVE if equivalent is not None and equivalent == monthly else SOURCE_STATED


def normalize_terms(row: Mapping[str, Any], previous: Any = None, *, strict: bool = True) -> dict:
    """Normalize a copy, deriving monthly when a known native pair changes.

    Existing stated monthly values are retained when native terms are unchanged.
    Clearing a previously derived native pair clears its stale monthly result;
    an explicitly supplied replacement monthly value is retained as stated.
    Older callers omitting native fields never erase stored contract terms.
    """
    result = dict(row)
    try:
        amount = _decimal(row.get("payment_amount", _get(previous, "payment_amount")), field="payment_amount", quantum=MONEY_QUANTUM, nonnegative=True)
        frequency = payment_frequency(row.get("payment_frequency", _get(previous, "payment_frequency")))
        monthly = _decimal(row.get("monthly_payment", _get(previous, "monthly_payment")), field="monthly_payment", quantum=MONEY_QUANTUM, nonnegative=True)
        for key in ("rate", "factor_rate"):
            if key in row:
                result[key] = _decimal(row[key], field=key, quantum=RATE_QUANTUM, nonnegative=key == "factor_rate")
        for key, value in (("payment_amount", amount), ("payment_frequency", frequency)):
            if key in row:
                result[key] = value
        previous_monthly = _decimal(_get(previous, "monthly_payment"), field="monthly_payment", quantum=MONEY_QUANTUM, nonnegative=True)
        native_changed = previous is not None and (
            "payment_amount" in row and amount != _decimal(_get(previous, "payment_amount"), field="payment_amount", quantum=MONEY_QUANTUM, nonnegative=True)
            or "payment_frequency" in row and frequency != payment_frequency(_get(previous, "payment_frequency"))
        )
        monthly_changed = previous is not None and "monthly_payment" in row and monthly != previous_monthly
        old_source = monthly_source(previous) if previous is not None else row.get("monthly_payment_source")
        equivalent = monthly_equivalent(amount, frequency)
        if previous is None and monthly is not None:
            # Validation has no edit context. An explicit monthly amount must
            # survive even if old native fields have no provenance marker.
            source = SOURCE_NATIVE if equivalent is not None and equivalent == monthly else SOURCE_STATED
        elif equivalent is not None and (
            native_changed or monthly is None
            or old_source == SOURCE_NATIVE and not monthly_changed
        ):
            monthly, source = equivalent, SOURCE_NATIVE
        elif equivalent is None and old_source == SOURCE_NATIVE and native_changed and not monthly_changed:
            monthly, source = None, None
        else:
            source = SOURCE_STATED if monthly is not None else None
        result["monthly_payment"] = monthly
        result["monthly_payment_source"] = source
        return result
    except DebtTermsError:
        if strict:
            raise
        return result
