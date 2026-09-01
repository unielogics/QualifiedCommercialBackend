"""Canonical evidence-backed financial values for Field Desk surfaces.

The PDF, routing engine, Step 3, and the application sidebar used to choose
their own sources.  This module keeps that precedence deterministic: confirmed
staff values win, then entered values, then verified calculations, then clearly
labelled estimates.  Missing evidence remains null instead of becoming zero.
"""

from __future__ import annotations

from typing import Any

from . import credit_quality


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in {float("inf"), float("-inf")}:
        return None
    return round(result, 2)


def _source(
    *,
    status: str,
    source: str,
    label: str,
    evidence: str | None = None,
) -> dict[str, str | None]:
    return {
        "status": status,
        "source": source,
        "label": label,
        "evidence": evidence,
    }


def _profile_or_suggestion(
    profile: Any | None,
    suggestions: dict[str, dict[str, Any]],
    field: str,
) -> tuple[float | None, dict[str, str | None]]:
    value = _number(getattr(profile, field, None)) if profile is not None else None
    confirmations = dict(getattr(profile, "field_confirmations", None) or {})
    provenance = dict(getattr(profile, "field_provenance", None) or {}).get(field) or {}
    if value is not None:
        confirmed = field in confirmations
        return value, _source(
            status="confirmed" if confirmed else "entered",
            source=str(provenance.get("source") or ("agent_confirmed" if confirmed else "application_profile")),
            label=str(provenance.get("label") or ("Agent confirmed" if confirmed else "Agent entered")),
            evidence=None,
        )

    suggestion = suggestions.get(field) or {}
    suggested = _number(suggestion.get("value"))
    if suggested is not None:
        return suggested, _source(
            status=str(suggestion.get("status") or "estimated"),
            source=str(suggestion.get("source") or "extracted_evidence"),
            label=str(suggestion.get("label") or "Extracted estimate"),
            evidence=str(suggestion.get("evidence") or "") or None,
        )
    return None, _source(
        status="unavailable",
        source="unavailable",
        label="Unavailable",
        evidence="No usable evidence has produced this value yet.",
    )


def _credit_snapshot(required_owners: list[Any]) -> tuple[dict[str, Any], dict[str, str | None]]:
    ranked: list[tuple[int, str, str]] = []
    fallback: list[tuple[int, str, str]] = []
    tier_rank = {
        "Excellent": 6,
        "Good": 5,
        "Average": 4,
        "Below average": 3,
        "Bad": 2,
        "Not fundable": 1,
    }
    for owner in required_owners:
        score = getattr(owner, "credit_score", None)
        quality = credit_quality.summary(score)
        if quality:
            ranked.append((int(score), quality["quality_tier"], quality["score_band"]))
            continue
        summary = dict(getattr(owner, "credit_summary", None) or {})
        tier = str(summary.get("quality_tier") or getattr(owner, "credit_tier", None) or "").strip()
        band = str(summary.get("score_band") or "").strip()
        if tier and band:
            fallback.append((tier_rank.get(tier, 0), tier, band))

    completed = len(ranked) + len(fallback)
    if ranked:
        _, tier, band = min(ranked, key=lambda item: item[0])
    elif fallback:
        _, tier, band = min(fallback, key=lambda item: item[0])
    else:
        tier = band = None
    status = (
        "verified"
        if required_owners and completed == len(required_owners)
        else "partial"
        if completed
        else "unavailable"
    )
    evidence = (
        f"Conservative quality band across {completed} of {len(required_owners)} required owner(s)."
        if completed
        else "No required-owner credit result is available yet."
    )
    return (
        {
            "credit_quality_tier": tier,
            "credit_score_band": band,
            "credit_status": status,
            "credit_completed_owners": completed,
            "credit_required_owners": len(required_owners),
        },
        _source(
            status=status,
            source="isoftpull" if completed else "unavailable",
            label="Verified soft inquiry" if status == "verified" else "Partial soft inquiry" if completed else "Unavailable",
            evidence=evidence,
        ),
    )


def build(
    *,
    profile: Any | None,
    required_owners: list[Any],
    metric_tree: dict[str, Any],
    period_rows: list[dict[str, Any]],
    statement_months: list[str],
    suggestions: dict[str, dict[str, Any]],
    negative_balance_days_90: int | None,
    nsf_count: int | None,
) -> dict[str, Any]:
    """Return the one financial snapshot used by every application surface."""

    sources: dict[str, dict[str, str | None]] = {}
    annual_sales, sources["annual_sales"] = _profile_or_suggestion(
        profile, suggestions, "annual_sales"
    )
    annual_cash_flow, sources["annual_cash_flow_available_for_debt"] = (
        _profile_or_suggestion(
            profile, suggestions, "annual_cash_flow_available_for_debt"
        )
    )
    monthly_debt, sources["monthly_debt_payments"] = _profile_or_suggestion(
        profile, suggestions, "monthly_debt_payments"
    )

    confirmations = dict(getattr(profile, "field_confirmations", None) or {})
    dscr_metrics = dict(metric_tree.get("dscr") or {})
    dscr = None
    if annual_cash_flow is not None and monthly_debt is not None and monthly_debt > 0:
        dscr = round(annual_cash_flow / (monthly_debt * 12), 3)
        both_confirmed = {
            "annual_cash_flow_available_for_debt",
            "monthly_debt_payments",
        }.issubset(confirmations)
        sources["dscr"] = _source(
            status="confirmed" if both_confirmed else "estimated",
            source="confirmed_financial_profile" if both_confirmed else "financial_profile",
            label="Confirmed calculation" if both_confirmed else "Estimated from financial profile",
            evidence="Annual cash flow divided by twelve months of scheduled debt service.",
        )
    else:
        candidates = (
            (dscr_metrics.get("current"), "verified", "metric_engine", "Verified calculation"),
            (dscr_metrics.get("draft"), "estimated", "identified_bank_activity", "Estimated from bank activity"),
            (dscr_metrics.get("cash_flow"), "estimated", "bank_cash_flow", "Cash-flow estimate"),
        )
        for raw, status, source, label in candidates:
            value = _number(raw)
            if value is not None:
                dscr = round(value, 3)
                sources["dscr"] = _source(
                    status=status,
                    source=source,
                    label=label,
                    evidence="Calculated by the underwriting metric engine from extracted evidence.",
                )
                break
        else:
            sources["dscr"] = _source(
                status="unavailable",
                source="unavailable",
                label="Unavailable",
                evidence="Cash flow and positive monthly debt service are both required.",
            )

    adb_metrics = dict(metric_tree.get("adb") or {})
    avg_daily_balance = _number(adb_metrics.get("current"))
    adb_source = str(adb_metrics.get("source") or "average_daily_balance")
    adb_status = (
        "verified"
        if avg_daily_balance is not None and adb_source == "average_daily_balance"
        else "estimated"
        if avg_daily_balance is not None
        else "unavailable"
    )
    sources["avg_daily_balance"] = _source(
        status=adb_status,
        source=adb_source if avg_daily_balance is not None else "unavailable",
        label=(
            "Verified average daily balance"
            if adb_source == "average_daily_balance"
            else "Estimated from ending balances"
            if avg_daily_balance is not None
            else "Unavailable"
        ),
        evidence=(
            f"Calculated across {len(period_rows)} qualifying month(s)."
            if avg_daily_balance is not None
            else "No readable daily or ending balances are available."
        ),
    )

    deposits = [
        value
        for row in period_rows
        if (value := _number(row.get("deposits"))) is not None
    ]
    average_monthly_deposits = round(sum(deposits) / len(deposits), 2) if deposits else None
    annualized_deposits = (
        round(average_monthly_deposits * 12, 2)
        if average_monthly_deposits is not None
        else None
    )
    deposit_status = "verified" if len(deposits) >= 6 else "estimated" if deposits else "unavailable"
    deposit_evidence = (
        f"Annualized from {len(deposits)} qualifying bank-evidence month(s)."
        if deposits
        else "No readable monthly deposit totals are available."
    )
    for key in ("average_monthly_deposits", "annualized_deposits"):
        sources[key] = _source(
            status=deposit_status,
            source="verified_bank_evidence" if deposits else "unavailable",
            label="Verified bank evidence" if deposit_status == "verified" else "Evidence-backed estimate" if deposits else "Unavailable",
            evidence=deposit_evidence,
        )

    sources["negative_balance_days_90"] = _source(
        status="verified" if negative_balance_days_90 is not None else "unavailable",
        source="daily_bank_balances" if negative_balance_days_90 is not None else "unavailable",
        label="Verified daily balances" if negative_balance_days_90 is not None else "Unavailable",
        evidence=(
            "Unique negative end-of-day balance dates in the latest 90-day window."
            if negative_balance_days_90 is not None
            else "The uploaded evidence does not contain readable daily balance history."
        ),
    )
    sources["returned_items"] = _source(
        status="verified" if nsf_count is not None else "unavailable",
        source="bank_statement_activity" if nsf_count is not None else "unavailable",
        label="Verified statement activity" if nsf_count is not None else "Unavailable",
        evidence=(
            f"Returned-item activity across {len(period_rows)} qualifying month(s)."
            if nsf_count is not None
            else "The evidence does not establish returned-item activity."
        ),
    )

    credit, credit_source = _credit_snapshot(required_owners)
    sources["credit_quality"] = credit_source
    return {
        **credit,
        "annual_sales": annual_sales,
        "annual_cash_flow_available_for_debt": annual_cash_flow,
        "monthly_debt_payments": monthly_debt,
        "dscr": dscr,
        "avg_daily_balance": avg_daily_balance,
        "negative_balance_days_90": negative_balance_days_90,
        "returned_items": nsf_count,
        "average_monthly_deposits": average_monthly_deposits,
        "annualized_deposits": annualized_deposits,
        "indicative_capacity": None,
        "capacity_path": None,
        "periods_used": len(period_rows),
        "statement_months": list(statement_months),
        "sources": sources,
    }


def add_capacity(snapshot: dict[str, Any], best_path: dict[str, Any] | None) -> dict[str, Any]:
    """Attach the modelled typical capacity without mutating the base snapshot."""

    result = {**snapshot, "sources": dict(snapshot.get("sources") or {})}
    capacity = _number((best_path or {}).get("funding_typical"))
    path = str((best_path or {}).get("label") or "").strip() or None
    result["indicative_capacity"] = capacity
    result["capacity_path"] = path
    result["sources"]["indicative_capacity"] = _source(
        status="estimated" if capacity is not None else "unavailable",
        source="program_sizing" if capacity is not None else "unavailable",
        label="Modeled typical capacity" if capacity is not None else "Unavailable",
        evidence=(
            f"Typical modeled capacity on {path}." if capacity is not None and path
            else "No path has enough financial evidence to size yet."
        ),
    )
    return result


__all__ = ["add_capacity", "build"]
