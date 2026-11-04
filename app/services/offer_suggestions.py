"""Suggested loan terms for a file that is not funded yet.

When the desk lists an unfunded file for capital partners it is publishing
suggested terms: which products to offer, at what amount, rate and term. This
module derives a starting point from the file itself, without a model: the
income figure the financial cockpit already settled on (tax-return EBITDA,
then the P&L, then bank cash flow as a labelled proxy), the debt service the
file carries today, and each program's published commercial terms. The
suggestion is the largest amount the business covers at a 1.25x DSCR on the
longest term the program allows, capped by the program's maximum and by what
the client asked for, with the coverage at the requested amount shown beside
it. The desk changes anything; nothing here is a decision.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_profile import ApplicationProfile
from app.models.funding_program import FundingProgramCatalog
from app.services import application_programs as programs
from app.services import financial_cockpit
from app.services import funding_programs as program_catalog
from app.services.financial_cockpit import amortized_payment, dscr_status

TARGET_DSCR = 1.25
#: The 10-year MCA refinance program's shape when a program publishes no range.
DEFAULT_TERM_MONTHS = 120
DEFAULT_RATE_PCT = 16.0
MAX_SUGGESTED_PRODUCTS = 4
_ROUND_TO = 500.0


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def capacity_monthly(
    income_annual: float | None,
    existing_monthly_debt_service: float | None,
    target: float = TARGET_DSCR,
) -> float | None:
    """The largest new monthly payment the income covers at the target DSCR."""
    if income_annual is None:
        return None
    room = income_annual / 12 / target - (existing_monthly_debt_service or 0.0)
    return round(max(0.0, room), 2)


def principal_for_payment(payment: float, annual_rate_pct: float, months: int) -> float:
    if payment <= 0 or months <= 0:
        return 0.0
    rate = annual_rate_pct / 100 / 12
    if rate <= 0:
        return round(payment * months, 2)
    return round(payment * (1 - (1 + rate) ** (-months)) / rate, 2)


def _midpoint(low: float | None, high: float | None) -> float | None:
    values = [v for v in (low, high) if v is not None]
    return round(sum(values) / len(values), 2) if values else None


def program_shape(terms: Any | None) -> dict[str, Any]:
    """Term, rate and amount bounds from published commercial terms, with the
    program's own pricing basis; defaults where the program publishes none."""
    if terms is None:
        return {
            "term_months": DEFAULT_TERM_MONTHS,
            "rate_pct": DEFAULT_RATE_PCT,
            "pricing_basis": "interest_rate",
            "min_amount": None,
            "max_amount": None,
            "min_term": None,
            "max_term": None,
            "published": False,
        }
    basis = str(getattr(terms, "pricing_basis", None) or "interest_rate")
    min_term, max_term = (
        getattr(terms, "minimum_term_months", None),
        getattr(terms, "maximum_term_months", None),
    )
    term = max_term or DEFAULT_TERM_MONTHS
    if min_term and term < min_term:
        term = min_term
    pricing = _midpoint(
        _num(getattr(terms, "minimum_pricing", None)), _num(getattr(terms, "maximum_pricing", None))
    )
    rate = (
        pricing
        if basis in {"apr", "interest_rate"} and pricing is not None
        else (None if basis == "factor_rate" else DEFAULT_RATE_PCT)
    )
    return {
        "term_months": int(term),
        "rate_pct": rate,
        "factor_rate": pricing if basis == "factor_rate" else None,
        "pricing_basis": basis,
        "min_amount": _num(getattr(terms, "minimum_amount", None)),
        "max_amount": _num(getattr(terms, "maximum_amount", None)),
        "min_term": min_term,
        "max_term": max_term,
        "published": True,
    }


def payment_for(amount: float, shape: dict[str, Any], interest_only: bool = False) -> float | None:
    term = int(shape.get("term_months") or DEFAULT_TERM_MONTHS)
    if shape.get("factor_rate"):
        return round(amount * float(shape["factor_rate"]) / term, 2)
    if interest_only:
        return round(amount * float(shape.get("rate_pct") or 0.0) / 100 / 12, 2)
    return amortized_payment(amount, float(shape.get("rate_pct") or 0.0), term)


def suggest_product(
    *,
    program_key: str | None,
    label: str,
    income_annual: float | None,
    existing_monthly_debt_service: float | None,
    requested_amount: float | None,
    shape: dict[str, Any],
    target: float = TARGET_DSCR,
    eligible: bool | None = None,
    interest_only: bool = False,
) -> dict[str, Any]:
    room = capacity_monthly(income_annual, existing_monthly_debt_service, target)
    term = int(shape.get("term_months") or DEFAULT_TERM_MONTHS)
    rate = shape.get("rate_pct")
    interest_only = bool(interest_only and not shape.get("factor_rate"))
    affordable: float | None = None
    if room is not None:
        if shape.get("factor_rate"):
            affordable = round(room * term / float(shape["factor_rate"]), 2)
        elif interest_only:
            # Interest alone: the room covers a principal of room / (rate / 12).
            affordable = round(room / (float(rate) / 100 / 12), 2) if rate else None
        else:
            affordable = principal_for_payment(room, float(rate or 0.0), term)
    candidates = [
        v for v in (affordable, requested_amount, shape.get("max_amount")) if v is not None
    ]
    amount = min(candidates) if candidates and income_annual is not None else None
    if amount is not None:
        amount = float(int(amount / _ROUND_TO) * _ROUND_TO)
    reason = None
    if income_annual is None:
        reason = "No income figure yet: a tax return, a profit-and-loss statement or three months of statements gives one."
    elif room is not None and room <= 0:
        reason = f"At {target:.2f}x the income on file leaves no room for a new payment after today's debt service."
        amount = None
    elif amount is not None and shape.get("min_amount") and amount < float(shape["min_amount"]):
        reason = f"The amount the income supports ({amount:,.0f}) is below the program minimum ({float(shape['min_amount']):,.0f})."
        amount = None
    payment = payment_for(amount, shape, interest_only) if amount else None
    existing_annual = (existing_monthly_debt_service or 0.0) * 12
    dscr_at = (
        round(income_annual / (existing_annual + payment * 12), 2)
        if income_annual is not None and payment
        else None
    )
    requested_payment = (
        payment_for(requested_amount, shape, interest_only) if requested_amount else None
    )
    dscr_requested = (
        round(income_annual / (existing_annual + requested_payment * 12), 2)
        if income_annual is not None and requested_payment
        else None
    )
    return {
        "program_key": program_key,
        "label": label,
        "eligible": eligible,
        "amount": amount,
        "rate_pct": rate,
        "factor_rate": shape.get("factor_rate"),
        "term_months": term,
        "interest_only": interest_only,
        "payment_frequency": "monthly",
        "monthly_payment": payment,
        "dscr": dscr_at,
        "dscr_status": dscr_status(dscr_at),
        "affordable_amount": affordable,
        "room_monthly": room,
        "requested_amount": requested_amount,
        "requested_monthly_payment": requested_payment,
        "requested_dscr": dscr_requested,
        "requested_dscr_status": dscr_status(dscr_requested),
        "bounds": {
            k: shape.get(k)
            for k in (
                "min_amount",
                "max_amount",
                "min_term",
                "max_term",
                "pricing_basis",
                "published",
            )
        },
        "reason": reason,
        "reading": _reading(
            label,
            amount,
            rate,
            term,
            payment,
            dscr_at,
            requested_amount,
            dscr_requested,
            reason,
            interest_only,
        ),
    }


def _reading(
    label,
    amount,
    rate,
    term,
    payment,
    dscr_at,
    requested_amount,
    dscr_requested,
    reason,
    interest_only=False,
) -> str:
    if reason and amount is None:
        return f"{label}: {reason}"
    if amount is None or payment is None or dscr_at is None:
        return f"{label}: no suggestion yet."
    structure = f"interest-only over {term} months" if interest_only else f"over {term} months"
    parts = [
        f"{label}: {amount:,.0f}"
        + (f" at {rate:g}%" if rate is not None else "")
        + f" {structure} is about {payment:,.0f} a month and leaves coverage at {dscr_at:.2f}x."
    ]
    if (
        requested_amount
        and dscr_requested is not None
        and amount is not None
        and abs(requested_amount - amount) >= _ROUND_TO
    ):
        parts.append(
            f"The {requested_amount:,.0f} the client asked for would put coverage at {dscr_requested:.2f}x."
        )
    return " ".join(parts)


def income_figure(cockpit: dict[str, Any]) -> tuple[float | None, str | None, str | None]:
    """(annual income figure, basis key, label) the cockpit's DSCR stands on."""
    headline = cockpit["dscr"].get("headline") or cockpit["dscr"].get("pro_forma_headline")
    if headline is None:
        candidates = cockpit["dscr"].get("candidates") or []
        headline = candidates[0] if candidates else None
    if headline is None:
        return None, None, None
    return headline.get("numerator"), headline.get("key"), headline.get("numerator_label")


async def suggest_offers(
    db: AsyncSession, profile: ApplicationProfile, *, program_keys: list[str] | None = None
) -> dict[str, Any]:
    cockpit = await financial_cockpit.load_cockpit(db, profile)
    income, basis_key, basis_label = income_figure(cockpit)
    evaluation = (cockpit.get("plan") or {}).get("evaluation") or {}
    plan_record = (cockpit.get("plan") or {}).get("record") or {}
    # The plan's add-backs, exclusions and overrides change the income the
    # offer stands on; its loan structure sets whether the terms are interest-only.
    if evaluation.get("active") and evaluation.get("income_after_annual") is not None:
        income = evaluation["income_after_annual"]
        basis_label = f"{basis_label or 'Income'} with the desk's plan"
    interest_only = bool(plan_record.get("new_loan_interest_only"))
    # The plan's retirements leave the file: their payments stop, and the new
    # loan has to cover their payoffs on top of what the client asked for.
    existing_monthly = evaluation.get("debt_service_before_new_payment_monthly")
    if existing_monthly is None:
        existing_monthly = cockpit["debt"]["service"].get("monthly") or 0.0
    requested = evaluation.get("loan_amount_needed") or cockpit.get("requested_amount")
    selections = await programs.active_selections(db, profile.id)
    candidates = {c.program_key: c for c in await programs.published_candidates(db, profile)}
    keys: list[str] = list(program_keys or [])
    if not keys:
        keys = [sel.program_key for sel in selections]
        keys += [
            c.program_key for c in candidates.values() if c.eligible and c.program_key not in keys
        ]
        keys = keys[:MAX_SUGGESTED_PRODUCTS]
    rows = (
        list(
            (
                await db.execute(
                    select(FundingProgramCatalog).where(FundingProgramCatalog.program_key.in_(keys))
                )
            )
            .scalars()
            .all()
        )
        if keys
        else []
    )
    by_key = {row.program_key: row for row in rows}
    terms_by_program = (
        await program_catalog.commercial_terms_by_program(
            db, [row.id for row in rows], include_drafts=False
        )
        if rows
        else {}
    )
    products = []
    for key in keys:
        row = by_key.get(key)
        candidate = candidates.get(key)
        label = (row.name if row else None) or (candidate.program_name if candidate else key)
        published = (terms_by_program.get(row.id) or [None])[0] if row else None
        products.append(
            suggest_product(
                program_key=key,
                label=label,
                income_annual=income,
                existing_monthly_debt_service=existing_monthly,
                requested_amount=requested,
                shape=program_shape(published),
                eligible=candidate.eligible if candidate else None,
                interest_only=interest_only,
            )
        )
    impact = (cockpit.get("plan") or {}).get("impact") or {}
    return {
        "plan_loan": impact.get("loan"),
        "interest_only": interest_only,
        "profile_id": str(profile.id),
        "income_annual": income,
        "income_basis": basis_key,
        "income_label": basis_label,
        "existing_debt_service_monthly": existing_monthly,
        "debt_service_source": cockpit["debt"]["service"].get("source"),
        "plan_active": bool(evaluation.get("active")),
        "payoff_total": evaluation.get("payoff_total"),
        "target_dscr": TARGET_DSCR,
        "requested_amount": requested,
        "products": products,
    }
