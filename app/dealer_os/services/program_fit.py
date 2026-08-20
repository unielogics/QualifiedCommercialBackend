"""Screening a Capital OS file against the real program catalogue.

Capital OS reasons in seven generic paths (sba, conventional, loc, cre,
equipment, working_capital, floorplan). The lending desk actually places files
into fourteen concrete programs with published thresholds and per-industry
gates, and that catalogue already exists in `main_street_programs`, fully
written and tested. Until now nothing on the Capital OS side could reach it, so
a rep working a file saw generic path readiness and never the program the file
would actually be submitted to.

This is an adapter, not a second catalogue. Every threshold, every gate and
every document rule stays in `main_street_programs`; duplicating any of it here
is how the two would drift into disagreeing about the same business.

**Fallbacks are the point.** A file that misses SBA is not a dead file, it is
usually a line-of-credit file. So the output is ordered easiest-reachable
first: things it qualifies for now, then things it is one or two items away
from, with those items named. That ordering is the whole reason a rep can leave
a visit with a next step instead of a maybe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.main_street_programs import (
    MAIN_STREET_PROGRAM_LABELS,
    compute_main_street_program_fit,
    normalize_industry,
)

__all__ = ["ProgramFit", "INTENT_FOR_PURPOSE", "screen"]

# Capital OS records what the money is for as `funding_purpose`; the catalogue
# calls the same thing `intent`. One mapping, here, rather than each caller
# guessing. Anything unrecognised becomes "not_sure", which screens broadly
# instead of screening nothing.
INTENT_FOR_PURPOSE: dict[str, str] = {
    "working_capital": "working_capital",
    "equipment": "equipment",
    "real_estate": "property",
    "refinance": "refinance_debt",
    "floorplan": "dealership",
    "other": "not_sure",
}


@dataclass
class ProgramFit:
    key: str
    label: str
    eligible: bool
    needs: list[str] = field(default_factory=list)
    """Borrower-safe. What would have to arrive or change for this to open."""
    blocked_by: list[str] = field(default_factory=list)
    """Internal. Why the screen said no, in the desk's own words."""


def _metrics_for_screen(metrics: dict[str, Any], dealer: Any) -> dict[str, Any]:
    """Translate a Capital OS snapshot into the keys the screen reads.

    Only keys we can honestly fill are set. A missing key means "unknown", and
    the screen treats unknown as not-yet-met, which is the safe direction: a
    program that shows as blocked on missing evidence prompts a rep to go and
    get the evidence. Guessing a value to make a program light up would have
    the opposite effect.
    """
    out: dict[str, Any] = {}

    revenue = metrics.get("revenue_annual") or metrics.get("ytd_annualized_revenue")
    if revenue is not None:
        out["ytd_annualized_revenue"] = revenue

    deposits = metrics.get("deposits_annualized") or metrics.get("annualized_adjusted_deposits")
    if deposits is not None:
        out["annualized_adjusted_deposits"] = deposits

    for src, dst in (
        ("ebitda", "estimated_ebitda_or_cash_flow"),
        ("dscr", "estimated_dscr"),
        ("bank_statement_months", "bank_statement_months"),
        ("tax_return_years", "tax_return_years"),
        ("nsf_count", "nsf_or_overdraft_count"),
    ):
        if metrics.get(src) is not None:
            out[dst] = metrics[src]

    if getattr(dealer, "funding_goal", None) is not None:
        out["requested_amount"] = float(dealer.funding_goal)

    return out


def _details_for_screen(metrics: dict[str, Any], dealer: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    fico = metrics.get("fico") or metrics.get("credit_score")
    if fico is not None:
        out["estimated_credit_score"] = fico
    tib = metrics.get("years_in_business")
    if tib is None and getattr(dealer, "started_on", None) is not None:
        from datetime import date

        out["years_in_business"] = (date.today() - dealer.started_on).days / 365.25
    elif tib is not None:
        out["years_in_business"] = tib
    if getattr(dealer, "funding_purpose", None) == "equipment":
        out["financing_equipment_or_vehicle"] = True
    if getattr(dealer, "funding_purpose", None) == "real_estate":
        out["owns_operating_property"] = True
    return out


def screen(
    dealer: Any,
    metrics: dict[str, Any],
    documents: list[dict[str, Any]] | None = None,
) -> list[ProgramFit]:
    """Which programs this file reaches, easiest first.

    Returns [] when there is no lending question to answer, which the catalogue
    decides, not us: a business-systems enquiry is not an unfundable loan, it is
    a different conversation, and showing it an empty program grid would be
    wrong in a way that showing nothing is not.
    """
    intent = INTENT_FOR_PURPOSE.get(
        getattr(dealer, "funding_purpose", None) or "other", "not_sure"
    )
    fit = compute_main_street_program_fit(
        intent=intent,
        industry=normalize_industry(getattr(dealer, "industry", None)),
        key_metrics=_metrics_for_screen(metrics, dealer),
        details=_details_for_screen(metrics, dealer),
        documents=documents or [],
    )

    rows: list[ProgramFit] = []
    for key, row in (fit or {}).items():
        if not isinstance(row, dict):
            continue
        rows.append(
            ProgramFit(
                key=key,
                label=MAIN_STREET_PROGRAM_LABELS.get(key, key),
                eligible=bool(row.get("eligible")),
                needs=[str(n)[:160] for n in (row.get("needs") or [])],
                blocked_by=[str(b)[:160] for b in (row.get("blocked_by") or [])],
            )
        )

    # Eligible first, then whatever is closest to eligible. Sorting by the
    # count of outstanding items is what surfaces the easier fallback ahead of
    # the prestigious program the file cannot reach yet.
    rows.sort(key=lambda r: (not r.eligible, len(r.blocked_by) + len(r.needs), r.label))
    return rows
