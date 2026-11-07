"""Read-only earnings context for AI and profile intelligence.

The cockpit's selected coverage basis stays primary. A separately labelled,
validated annual P&L pair is suitable for fields that specifically mean annual
EBITDA; a tax earnings estimate or bank proxy never fills those fields.
"""

import math
import re
from copy import deepcopy

SOURCE_KEYS = ("file_id", "file_name", "analysis_id", "content_hash", "accepted", "entity", "period_start", "period_end", "currency", "basis", "version_id", "fact_key", "fact_present", "citations", "pages")
CURRENCY_KEYS = ("numerator_currency", "denominator_currency", "source_currencies", "currency_comparable", "currency_status", "currency_note", "denominator_currency_basis")
RESULT_KEYS = ("key", "label", "kind", "value", "annualized_value", "period", "months_covered", "annualization_factor",
               "formula", "complete", "provisional", "entity_mismatch", "entity_comparable", "missing_inputs", "input_keys",
               "computed_value", "reported_value", "currency", "basis", "numerator", "denominator", "selected", "basis_status", "debt_version_id", "notes", *CURRENCY_KEYS)


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _refs(values):
    return [{key: source[key] for key in SOURCE_KEYS if key in source} for source in values or [] if isinstance(source, dict)]


def _result(value):
    if not isinstance(value, dict):
        return None
    return {**{key: value[key] for key in RESULT_KEYS if key in value}, "source_refs": _refs(value.get("source_refs"))}


def validated_annual_pair(cockpit):
    """A current, reconciled P&L annual basis; never a tax or cash substitute."""
    for result in (cockpit.get("ebitda") or {}).get("candidates", []):
        refs = result.get("source_refs") or []
        annual = _number(result.get("annualized_value"))
        currency = str(result.get("currency") or (refs[0].get("currency") if refs else "") or "").upper()
        basis = str(result.get("basis") or (refs[0].get("basis") if refs else "") or "").strip().casefold()
        reported, computed = _number(result.get("reported_value")), _number(result.get("computed_value"))
        if (result.get("kind") not in {"computed_ebitda", "reported_ebitda"}
                or annual is None or not result.get("complete") or result.get("provisional")
                or not result.get("entity_comparable") or result.get("entity_mismatch")
                or result.get("months_covered") not in range(6, 13)
                or not re.fullmatch(r"[A-Z]{3}", currency) or currency == "XXX"
                or basis not in {"cash", "accrual"}
                or not refs or not all(source.get("accepted") and source.get("period_start") and source.get("period_end") for source in refs)
                or reported is not None and computed is not None and abs(reported - computed) > 0.01):
            continue
        candidate = next((row for row in (cockpit.get("dscr") or {}).get("candidates", [])
                          if row.get("key") == "profit_and_loss" and _number(row.get("numerator")) is not None
                          and abs(float(row["numerator"]) - annual) <= 0.01), None)
        service = (cockpit.get("debt") or {}).get("service") or {}
        debt_validated = bool(service.get("schedule_authoritative") or service.get("observed_additions_suppressed"))
        debt_currency = str(service.get("currency") or "USD").upper()  # The system debt templates are denominated in dollars.
        return {"ebitda": _result(result), "ebitda_annual": annual, "currency": currency,
                "basis": "validated_profit_and_loss_annual_ebitda", "period": result.get("period"),
                "dscr": _number(candidate.get("value")) if candidate and debt_validated and currency == debt_currency else None,
                "annual_debt_service": _number((cockpit.get("dscr") or {}).get("annual_debt_service")),
                "debt_service_validated": debt_validated, "debt_service_basis": service.get("source"),
                "dscr_label": "P&L EBITDA-based DSCR", "source_refs": _refs(refs)}
    return None


def metric_brief(cockpit):
    """No staff reasons, actor identities, audit history or receipt data."""
    review = cockpit.get("metric_review") or {}
    ebitda = cockpit.get("ebitda") or {}
    dscr = cockpit.get("dscr") or {}
    if not review and not ebitda and not dscr:
        return None
    inputs = []
    corrections = []
    for item in review.get("inputs", []):
        row = {key: item[key] for key in ("key", "label", "original_value", "value", "unit", "period", "missing") if key in item}
        row["source_refs"] = _refs(item.get("source_refs"))
        row["corrected"] = bool(item.get("correction"))
        inputs.append(row)
        if row["corrected"]:
            corrections.append({"input_key": item["key"], "original_value": item.get("original_value"), "effective_value": item.get("value"),
                                "period": item.get("period"), "source_refs": row["source_refs"], "status": "current_applied"})
    headline = dscr.get("headline")
    primary = None
    if headline:
        primary = {key: headline[key] for key in ("key", "label", "value", "numerator", "numerator_label", "period", "confidence", "input_keys", "note", *CURRENCY_KEYS) if key in headline}
        primary["source_refs"] = _refs(headline.get("source_refs"))
        primary["annual_debt_service"] = dscr.get("annual_debt_service")
        primary["role"] = "Primary DSCR, exactly the current financial cockpit headline"
    return {"revision": review.get("revision", 0), "scope": "Current source-bound cockpit calculations; original documents, AI snapshots, saved forms and financial-period records are unchanged.",
            "ebitda": {"headline": _result(ebitda.get("headline")), "candidates": [_result(row) for row in ebitda.get("candidates", [])], "reason": ebitda.get("reason")},
            "primary_dscr": primary, "validated_annual": validated_annual_pair(cockpit),
            "inputs": inputs, "corrections": corrections,
            "excluded_income_sources": [{key: row.get(key) for key in ("file_id", "file_name", "reason")}
                                        for row in review.get("excluded_income_sources", [])],
            "comparative_conflicts": review.get("comparative_conflicts", []),
            "stale_corrections": [{"input_key": row.get("input_key"), "period": row.get("period"), "status": "stale_not_applied"} for row in review.get("stale_corrections", [])],
            "calculations": [_result(row) for row in review.get("calculations", [])]}


def attach_review_context(context, cockpit):
    """The correction context is independent of hypothetical-plan activity."""
    from app.services.financial_cockpit import plan_brief

    brief = metric_brief(cockpit)
    if brief is not None:
        context["financial_calculation_review"] = brief
    else:
        context.pop("financial_calculation_review", None)
    plan = plan_brief(cockpit)
    if plan.get("active"):
        context["desk_financial_plan"] = plan
    else:
        context.pop("desk_financial_plan", None)


def current_review_projection(result, cockpit):
    """Overlay current operands on a response copy; preserve historical AI text.

    No ORM result or immutable review is modified. An unavailable current
    value replaces a cached number instead of letting a personal-return or
    obsolete debt basis remain the visible financial position.
    """
    projected = deepcopy(result) if isinstance(result, dict) else {}
    metrics = projected.get("key_metrics")
    metrics = dict(metrics) if isinstance(metrics, dict) else {}
    brief = metric_brief(cockpit) if cockpit is not None else None
    primary = (brief or {}).get("primary_dscr") or {}
    annual = (brief or {}).get("validated_annual") or {}
    service = ((cockpit or {}).get("debt") or {}).get("service") or {}
    strict_ebitda = annual.get("ebitda_annual") if annual.get("currency") == "USD" else None
    metrics.update({"estimated_dscr": primary.get("value"), "dscr": primary.get("value"),
                    "annual_debt_service": service.get("annual"), "monthly_debt_service": service.get("monthly"),
                    "debt_service_monthly": service.get("monthly"),
                    "annual_ebitda": strict_ebitda, "bankable_ebitda": strict_ebitda,
                    "dscr_basis": primary.get("key"), "dscr_earnings": primary.get("numerator"),
                    "dscr_earnings_label": primary.get("numerator_label"),
                    "dscr_source_refs": primary.get("source_refs") or [],
                    "financial_metrics_source": "current_financial_cockpit" if brief is not None else "current_financial_evidence_unavailable"})
    business_years = ((cockpit or {}).get("tax") or {}).get("years") or []
    metrics["tax_net_income"] = business_years[-1].get("income") if business_years else None
    metrics["tax_earnings_estimate"] = primary.get("numerator") if primary.get("key") == "tax_return" else None
    projected["key_metrics"] = metrics
    projected["financial_calculation_review"] = brief
    return projected
