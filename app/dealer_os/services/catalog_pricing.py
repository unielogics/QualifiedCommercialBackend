"""Normalize catalog pricing into lender-safe term illustrations.

Catalog rows are versioned and may still contain the original localized
string shape.  This helper accepts both shapes, only calculates payments when
the catalog has explicit rate endpoints, and never manufactures an index.
"""

from __future__ import annotations

from math import isfinite
from typing import Any


DEFAULT_ILLUSTRATION_AMOUNT = 100_000.0


def _money(value: float) -> float:
    return round(value + 1e-9, 2)


def _amortized_payment(principal: float, annual_rate: float, months: int) -> dict[str, float]:
    monthly_rate = annual_rate / 12
    if monthly_rate == 0:
        payment = principal / months
    else:
        factor = (1 + monthly_rate) ** months
        payment = principal * monthly_rate * factor / (factor - 1)
    total = payment * months
    return {
        "monthly_payment": _money(payment),
        "total_payments": _money(total),
        "total_interest": _money(total - principal),
    }


def _localized(value: Any, locale: str) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        selected = value.get(locale) or value.get("en")
        return str(selected) if selected is not None else None
    return None


def normalize_catalog_pricing(
    pricing: dict[str, Any] | None,
    *,
    locale: str,
    requested_amount: float | int | None,
) -> dict[str, Any]:
    """Return display copy and vertically renderable term scenarios.

    Indexed scenarios stay intentionally uncalculated until both an index
    value and effective date are stored in the versioned catalog record.
    """

    source = pricing or {}
    display = _localized(source.get("display"), locale) if "display" in source else _localized(source, locale)
    amount = float(requested_amount or DEFAULT_ILLUSTRATION_AMOUNT)
    if not isfinite(amount) or amount <= 0:
        amount = DEFAULT_ILLUSTRATION_AMOUNT

    normalized: list[dict[str, Any]] = []
    raw_scenarios = source.get("scenarios") if isinstance(source, dict) else None
    for raw in raw_scenarios if isinstance(raw_scenarios, list) else []:
        if not isinstance(raw, dict):
            continue
        months = int(raw.get("term_months") or 0)
        if months <= 0:
            continue
        rate_type = str(raw.get("rate_type") or "fixed")
        row: dict[str, Any] = {
            "term_months": months,
            "rate_type": rate_type,
            "illustration_amount": _money(amount),
            "amount_source": "requested" if requested_amount else "default_illustration",
            "source": raw.get("source"),
            "effective_date": raw.get("effective_date"),
            "best": None,
            "highest_cost": None,
            "calculation_available": False,
            "unavailable_reason": None,
        }
        if rate_type == "indexed":
            row.update({
                "index_name": raw.get("index_name"),
                "spread": raw.get("spread"),
                "index_value": raw.get("index_value"),
            })
            index_value = raw.get("index_value")
            effective_date = raw.get("effective_date")
            if index_value is None or not effective_date:
                row["unavailable_reason"] = (
                    "Se requiere un valor del indice y una fecha efectiva."
                    if locale == "es"
                    else "Index value and effective date are required."
                )
                normalized.append(row)
                continue
            best_rate = float(index_value) + float(raw.get("spread") or 0)
            highest_rate = float(index_value) + float(raw.get("highest_spread") or raw.get("spread") or 0)
        else:
            best_rate = float(raw.get("best_rate")) if raw.get("best_rate") is not None else None
            highest_rate = float(raw.get("highest_rate")) if raw.get("highest_rate") is not None else None
            if best_rate is None or highest_rate is None:
                row["unavailable_reason"] = (
                    "Los extremos de tasa no estan configurados."
                    if locale == "es"
                    else "Rate endpoints are not configured."
                )
                normalized.append(row)
                continue

        row["best"] = {"annual_rate": best_rate, **_amortized_payment(amount, best_rate, months)}
        row["highest_cost"] = {
            "annual_rate": highest_rate,
            **_amortized_payment(amount, highest_rate, months),
        }
        row["calculation_available"] = True
        normalized.append(row)

    return {
        "display": display,
        "term_scenarios": normalized,
        "illustration_amount": _money(amount),
        "amount_source": "requested" if requested_amount else "default_illustration",
    }
