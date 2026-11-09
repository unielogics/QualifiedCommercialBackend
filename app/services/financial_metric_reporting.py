"""Read-only earnings context for AI and profile intelligence.

The cockpit's selected coverage basis stays primary. A separately labelled,
validated annual P&L pair is suitable for fields that specifically mean annual
EBITDA; a tax earnings estimate or bank proxy never fills those fields.
"""

import logging
import math
import re
from copy import deepcopy

SOURCE_KEYS = ("file_id", "file_name", "analysis_id", "content_hash", "accepted", "entity", "period_start", "period_end", "statement_period", "currency", "basis", "version_id", "fact_key", "fact_present", "citations", "pages", "identity_association")
CURRENCY_KEYS = ("numerator_currency", "denominator_currency", "source_currencies", "currency_comparable", "currency_status", "currency_note", "denominator_currency_basis")
RESULT_KEYS = ("key", "label", "kind", "value", "annualized_value", "period", "months_covered", "annualization_factor",
               "formula", "complete", "provisional", "entity_mismatch", "entity_comparable", "missing_inputs", "input_keys",
               "computed_value", "reported_value", "currency", "basis", "numerator", "denominator", "selected", "basis_status", "debt_version_id", "notes",
               "source_value", "reviewed_value", "reviewed_label", "staff_adjusted", "year", "period_start", "period_end", *CURRENCY_KEYS)


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _refs(values):
    rows = []
    for source in values or []:
        if not isinstance(source, dict):
            continue
        row = {key: source[key] for key in SOURCE_KEYS if key in source and key != "identity_association"}
        association = source.get("identity_association")
        if isinstance(association, dict):
            row["identity_association"] = {key: association[key] for key in (
                "status", "automatic_status", "allows_financial_use", "expected_entity", "observed_entity", "observed_entities", "document_accepted"
            ) if key in association}
        rows.append(row)
    return rows


def _result(value):
    if not isinstance(value, dict):
        return None
    return {**{key: value[key] for key in RESULT_KEYS if key in value}, "source_refs": _refs(value.get("source_refs"))}


def operating_earnings_brief(cockpit):
    """One shareable earnings snapshot for AI, marketplace and package PDFs.

    Selection audit reasons, actor identities and concurrency receipts stay
    internal. Historical earnings and illustrative forecasts never occupy the
    same fact field, even when the historical value is unavailable.
    """
    ebitda = (cockpit or {}).get("ebitda") or {}
    if not ebitda:
        return None
    selection = ebitda.get("source_selection") or {}
    trend = ebitda.get("revenue_trend") or {}

    def fields(value, keys):
        return {key: value[key] for key in keys if key in value} if isinstance(value, dict) else {}

    def observation(value):
        return {**fields(value, ("year", "period_start", "period_end", "months_covered", "revenue",
                                "average_monthly_revenue", "ebitda", "ebitda_kind", "currency", "basis",
                                "file_id", "file_name")), "source_refs": _refs(value.get("source_refs"))}

    forecast = trend.get("forecast")
    noi = (cockpit or {}).get("noi") or {}
    noi_scenario = noi.get("scenario") or {}
    noi_historical = _result(noi.get("headline"))
    if noi_historical:
        noi_historical["fundamentals"] = [fields(row, ("key", "label", "value", "signed_value"))
                                         for row in noi["headline"].get("fundamentals", [])]
    return {
        "historical": _result(ebitda.get("headline")),
        "historical_unavailable_reason": ebitda.get("reason"),
        "source_selection": {
            **fields(selection, ("mode", "selected_file_id", "excluded_file_ids", "stale")),
            "sources": [fields(row, ("file_id", "file_name", "entity", "period", "kind", "eligible", "included", "selected", "baseline_eligible", "comparative_only"))
                        for row in selection.get("sources", []) if isinstance(row, dict)],
        },
        "revenue_trend": {
            **fields(trend, ("status", "notes")),
            "periods": [observation(row) for row in trend.get("periods", []) if isinstance(row, dict)],
            "comparison": fields(trend.get("comparison"), ("prior_year", "current_year", "prior_revenue", "current_revenue",
                                    "change_amount", "change_pct", "monthly_change", "comparable", "direction", "reason")) or None,
            "forecast": ({**fields(forecast, ("status", "method", "target_year", "projected_revenue", "change_amount",
                                    "change_pct", "projected_ebitda", "ebitda_kind", "margin", "assumptions", "reason")),
                          "source_refs": _refs(forecast.get("source_refs"))} if isinstance(forecast, dict) else None),
            "bank_context": fields(trend.get("bank_context"), ("average_monthly_deposits", "annualized_deposits", "note")),
        },
        "potential": deepcopy(ebitda.get("potential")),
        "noi": {
            "definition": fields(noi.get("definition"), ("key", "label", "formula", "includes_depreciation_and_amortization")),
            "historical": noi_historical, "reason": noi.get("reason"),
            "years": [_result(row) for row in noi.get("years", [])],
            "trend": fields(noi.get("trend"), ("direction", "comparable", "prior_year", "current_year", "change_amount", "change_pct", "reason")),
            "scenario": fields(noi_scenario, ("current", "plan", "annual_debt_service_current", "annual_debt_service_plan",
                "after_debt_service_current", "after_debt_service_plan", "notes")),
        } if noi else None,
        "qualification_policy": "Historical source earnings and illustrative forecasts are separate. Forecast revenue or EBITDA does not establish current program eligibility; bank deposits are not operating revenue.",
    }


def validated_annual_pair(cockpit):
    """A current, reconciled P&L annual basis; never a tax or cash substitute."""
    earnings = cockpit.get("ebitda") or {}
    selection = earnings.get("source_selection")
    if isinstance(selection, dict) and (selection.get("stale") or not selection.get("selected_file_id")):
        return None
    for result in earnings.get("candidates", []):
        refs = result.get("source_refs") or []
        if isinstance(selection, dict) and (result.get("excluded_from_ebitda") or not any(
                str(source.get("file_id")) == str(selection["selected_file_id"]) for source in refs)):
            continue
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
        debt_validated = bool(service.get("schedule_authoritative") or service.get("observed_additions_suppressed")) and not service.get("identity_association_conflict")
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
    earnings = operating_earnings_brief(cockpit) or {}
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
    calculation_reviews = []
    for calculation in review.get("calculations", []):
        recipe = calculation.get("recipe") or {}
        if recipe.get("current_event"):
            calculation_reviews.append({"calculation_key": calculation["key"], "status": "staff_adjusted",
                "source_value": recipe.get("source_value"), "reviewed_value": recipe.get("reviewed_value"),
                "period": recipe.get("period"), "annualization_factor": recipe.get("annualization_factor"),
                "components": [{key: component.get(key) for key in ("input_key", "included", "contribution", "role")} for component in recipe.get("components", [])],
                "adjustments": [{key: adjustment.get(key) for key in ("label", "amount")} for adjustment in recipe.get("adjustments", [])],
                "denominator": recipe.get("denominator"),
                "qualification_policy": "Staff-reviewed DSCR recipe; underlying source EBITDA and verified-program earnings remain distinct."})
    headline = dscr.get("headline")
    primary = None
    if headline:
        primary = {key: headline[key] for key in ("key", "label", "value", "numerator", "numerator_label", "period", "confidence", "input_keys", "note", "staff_adjusted", "source_numerator", "source_value", *CURRENCY_KEYS) if key in headline}
        primary["source_refs"] = _refs(headline.get("source_refs"))
        primary["annual_debt_service"] = dscr.get("annual_debt_service")
        primary["role"] = "Primary DSCR, exactly the current financial cockpit headline"
    return {"revision": review.get("revision", 0), "scope": "Current source-bound cockpit calculations; original documents, AI snapshots, saved forms and financial-period records are unchanged.",
            "document_associations": [{key: row.get(key) for key in ("file_id", "content_hash", "analysis_id", "status", "expected_entity", "observed_entity", "association_stamp")}
                                      for row in review.get("document_associations", [])],
            "ebitda": {"headline": _result(ebitda.get("headline")), "candidates": [_result(row) for row in ebitda.get("candidates", [])], "reason": ebitda.get("reason"),
                       "source_selection": earnings.get("source_selection"),
                       "revenue_trend": earnings.get("revenue_trend"), "potential": earnings.get("potential")},
            "noi": earnings.get("noi"),
            "primary_dscr": primary, "validated_annual": validated_annual_pair(cockpit),
            "inputs": inputs, "corrections": corrections, "calculation_reviews": calculation_reviews,
            "excluded_income_sources": [{key: row.get(key) for key in ("file_id", "file_name", "reason")}
                                        for row in review.get("excluded_income_sources", [])],
            "comparative_conflicts": review.get("comparative_conflicts", []),
            "stale_corrections": [{"input_key": row.get("input_key"), "period": row.get("period"), "status": "stale_not_applied"} for row in review.get("stale_corrections", [])],
            "stale_compositions": [{"calculation_key": row.get("calculation_key"), "period": row.get("period"), "status": "stale_not_applied"} for row in review.get("stale_compositions", [])],
            "calculations": [_result(row) for row in review.get("calculations", [])]}


def attach_review_context(context, cockpit):
    """The correction context is independent of hypothetical-plan activity."""
    from app.services.financial_cockpit import plan_brief

    brief = metric_brief(cockpit)
    from app.services.financial_calculation_policy import financial_authority

    context["financial_calculation_authority"] = financial_authority(cockpit)
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
    from app.services.financial_calculation_policy import (
        CALCULATION_POLICY_VERSION,
        financial_authority,
    )

    authority = financial_authority(cockpit) if cockpit is not None else None
    primary = (brief or {}).get("primary_dscr") or {}
    annual = (brief or {}).get("validated_annual") or {}
    service = ((cockpit or {}).get("debt") or {}).get("service") or {}
    strict_ebitda = annual.get("ebitda_annual") if annual.get("currency") == "USD" else None
    metrics.update({"estimated_dscr": primary.get("value"), "dscr": primary.get("value"),
                    "annual_debt_service": service.get("annual"), "monthly_debt_service": service.get("monthly"),
                    "debt_service_monthly": service.get("monthly"),
                    "annual_ebitda": strict_ebitda, "bankable_ebitda": strict_ebitda,
                    "estimated_ebitda_or_cash_flow": strict_ebitda,
                    "ebitda_source": "profit_and_loss" if strict_ebitda is not None else None,
                    "dscr_basis": primary.get("key"), "dscr_earnings": primary.get("numerator"),
                    "dscr_earnings_label": primary.get("numerator_label"),
                    "dscr_source_refs": primary.get("source_refs") or [],
                    "financial_calculation_policy_version": CALCULATION_POLICY_VERSION,
                    "financial_calculation_signature": authority.get("signature") if authority else None,
                    "financial_metrics_source": "current_financial_cockpit" if brief is not None else "current_financial_evidence_unavailable"})
    business_years = ((cockpit or {}).get("tax") or {}).get("years") or []
    metrics["tax_net_income"] = business_years[-1].get("income") if business_years else None
    metrics["tax_earnings_estimate"] = primary.get("numerator") if primary.get("key") == "tax_return" else None
    projected["key_metrics"] = metrics
    projected["financial_calculation_review"] = brief
    projected["financial_calculation_authority"] = authority
    projected["operating_earnings"] = operating_earnings_brief(cockpit)
    return projected


async def current_intake_report(db, intake, result, *, external=False):
    """Refresh business intelligence exports from the same current cockpit.

    Real-estate rent/PITIA reports keep their separate calculation pathway.
    Failure to load business evidence must clear a cached earnings number.
    """
    if getattr(intake, "variant", None) == "real_estate_dscr_v1":
        return deepcopy(result) if isinstance(result, dict) else {}
    from app.services import application_profiles, financial_cockpit

    cockpit = None
    try:
        profile = await application_profiles.find_profile(db, intake_id=intake.id)
        if profile is not None:
            cockpit = await financial_cockpit.load_cockpit(db, profile)
    except Exception:  # noqa: BLE001 - failed current evidence never revives cached metrics
        logging.getLogger(__name__).exception("Current report financial evidence unavailable intake=%s", intake.id)
    projected = current_review_projection(result, cockpit)
    if external:
        from app.capital_partners.services.listings import partner_operating_earnings

        projected["operating_earnings"] = partner_operating_earnings(projected.get("operating_earnings"))
        projected.pop("financial_calculation_review", None)
        projected.pop("financial_calculation_authority", None)
        projected["key_metrics"].pop("dscr_source_refs", None)
    return projected
