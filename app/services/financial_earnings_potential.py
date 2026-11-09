"""Read-only earnings targets, never a replacement for historical results.

The latest selected business period supplies both margin and earnings. A prior
year only describes trend. A negative source margin stays negative; neither
revenue growth nor refinancing creates an operating earnings improvement.
"""

from app.services.financial_metric_review import number


def _rounded(value):
    return round(value, 2) if value is not None else None


def build(ebitda, service, evaluation, *, statement=None):
    headline = ebitda.get("headline") or {}
    selection = ebitda.get("source_selection") or {}
    trend = ebitda.get("revenue_trend") or {}
    comparison = trend.get("comparison") or {}
    file_id = selection.get("selected_file_id")
    period = next((row for row in trend.get("periods", []) if row.get("file_id") == file_id), {})
    revenue = number(period.get("revenue"))
    annual_earnings = number(headline.get("annualized_value"))
    annualized = False
    if not period and statement and str(statement.get("file_id")) == file_id:
        months = number(headline.get("months_covered"))
        reported_revenue = number(statement.get("gross_revenue"))
        if months is not None and 6 <= months <= 12 and reported_revenue is not None:
            revenue = reported_revenue * 12 / months
            annualized = months != 12
    currency = headline.get("currency") or period.get("currency")
    basis = headline.get("basis") or period.get("basis")
    usable = bool(headline and not selection.get("stale") and file_id
                  and annual_earnings is not None and revenue is not None and revenue > 0
                  and currency and basis in {"cash", "accrual"})
    denominator_compatible = usable and currency == "USD" and not service.get("identity_association_conflict")
    notes = [
        "The latest business period is the earnings baseline; the prior year supplies trend only.",
        "Targets are required earnings, not forecasts or evidence of program qualification.",
        "A refinance changes debt service. It does not itself change EBITDA or the source margin.",
        "A negative margin remains negative when revenue grows unless a separate operating improvement is supported.",
        "With negative earnings, lower debt service can make the arithmetic DSCR more negative even while reducing the funding shortfall. Compare earnings less debt service alongside the ratio.",
    ]
    if annualized:
        notes.append("The selected partial P&L is annualized from its reporting months; this is a run-rate illustration, without seasonal adjustment.")
    if headline.get("provisional"):
        notes.append("The source earnings estimate is provisional; its missing-component and reconciliation limitations remain in effect.")
    if evaluation.get("debt_service_override_monthly") is not None:
        notes.append("Saved-plan debt service includes a staff scenario override; current published debt service is unchanged.")
    if evaluation.get("schedule"):
        notes.append("Saved-plan annual debt service uses the coverage window, including scheduled balloons and maturity stress where configured; it is not a regular-payment cash-flow forecast.")
    if currency and currency != "USD":
        notes.append("Source currency differs from USD debt service; no exchange rate is assumed and coverage targets are unavailable.")

    def coverage(annual_service, earnings=annual_earnings):
        annual_service = number(annual_service)
        if annual_service is None or annual_service < 0 or not denominator_compatible:
            return None
        targets = []
        if annual_service > 0:
            for ratio in (1.0, 1.25):
                required = annual_service * ratio
                targets.append({"ratio": ratio, "earnings_required": _rounded(required),
                    "margin_pct": round(required / revenue * 100, 2),
                    "improvement_required": _rounded(max(0, required - earnings)) if earnings is not None else None})
        return {"annual_debt_service": _rounded(annual_service),
                "dscr": round(earnings / annual_service, 2) if earnings is not None and annual_service > 0 else None,
                "earnings_less_debt_service": _rounded(earnings - annual_service) if earnings is not None else None,
                "funding_shortfall": _rounded(max(0, annual_service - earnings)) if earnings is not None else None,
                "no_debt_service": annual_service == 0, "targets": targets}

    current = coverage(service.get("annual"))
    plan = coverage(evaluation.get("debt_service_after_annual")) if evaluation.get("active") and not evaluation.get("coverage_unavailable_reason") else None
    forecast_source = trend.get("forecast") or {}
    forecast = None
    if forecast_source.get("status") == "illustration":
        earnings = number(forecast_source.get("projected_ebitda"))
        # Currency/period validation is inherited from the source-bound revenue
        # illustration. Explicit margins remain assumptions, never accepted NOI.
        forecast = {"status": "illustration", "earnings": earnings,
                    "revenue": number(forecast_source.get("projected_revenue")),
                    "kind": forecast_source.get("ebitda_kind"),
                    "dscr_current": round(earnings / current["annual_debt_service"], 2) if earnings is not None and current and current["annual_debt_service"] > 0 else None,
                    "dscr_plan": round(earnings / plan["annual_debt_service"], 2) if earnings is not None and plan and plan["annual_debt_service"] > 0 else None,
                    "assumptions": forecast_source.get("assumptions", [])}
    return {"available": usable,
        "reason": None if usable else "A latest-year business earnings source with known annual earnings, revenue, reporting period, currency and accounting basis is needed for an earnings potential calculation.",
        "baseline": {"revenue": _rounded(revenue), "earnings": annual_earnings,
                     "kind": headline.get("kind"), "margin_pct": round(annual_earnings / revenue * 100, 4) if usable else None,
                     "period": headline.get("period") or period.get("year"), "currency": currency, "basis": basis,
                     "provisional": bool(headline.get("provisional")), "annualized": annualized},
        "trend": {"direction": comparison.get("direction") if comparison.get("comparable") else "unavailable",
                  "change_pct": comparison.get("change_pct"), "prior_year": comparison.get("prior_year"), "current_year": comparison.get("current_year")},
        "operating_break_even": {"earnings_required": 0, "improvement_required": _rounded(max(0, -annual_earnings)) if usable else None},
        "current": current, "plan": plan, "forecast": forecast, "notes": notes}
