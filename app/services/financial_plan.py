"""The financial plan: one model the coverage, the simulator, the debt
schedule and the marketplace offer all read.

The plan says which obligations the new loan pays off (their payments stop
and their payoff is money the loan must cover), which statement payees are
not operating spend (transfers to the owner's own accounts, which the bank
proxy otherwise counts as money gone), and the payment the new loan adds.
Everything derives from the cockpit's figures plus that record: the debt
service after the plan, each coverage basis after the plan, the month-by-
month replay of the statements with the retired payments taken out, and the
levers that would raise coverage. No model call; every lever is arithmetic
the desk can check.

Obligations join the debt schedule rows to the payees the statements show:
a row linked by its ledger identity (vendor_key, set when the desk records an
advance from a statement payee) or by name, and the observed payees no row
explains. Retiring an obligation retires the row and its payee together.
"""

from __future__ import annotations

from typing import Any

from app.services.financial_cockpit import amortized_payment, dscr_status, month_label
from app.services.loan_progress import loan_progress_fields
from app.services.payee_classification import match_names

TARGET_DSCR = 1.25
#: What the desk may add back to the income available for debt service.
ADD_BACK_KINDS: dict[str, str] = {
    "owner_salary": "Owner salary / officer compensation",
    "interest_refinanced": "Interest on the debt the loan refinances",
    "depreciation": "Depreciation and amortization",
    "one_time_expense": "One-time or non-recurring expense",
    "rent_to_owner": "Rent paid to the owner",
    "other": "Other add-back",
}
#: Payees worth suggesting as "not operating spend" start here.
EXCLUDE_MIN_MONTHLY = 500.0
#: Kinds a desk may plausibly mark as not operating spend.
EXCLUDABLE_KINDS = frozenset({"transfer", "owner_draw"})
DEFAULT_RATE_PCT = 16.0
DEFAULT_TERM_MONTHS = 120
MAX_LEVERS = 6
DOUBLE_COUNTED_ADD_BACK_KINDS = frozenset({"interest_refinanced", "depreciation"})
DOUBLE_COUNTED_ADD_BACK_REASON = (
    "Interest and depreciation/amortization are already excluded from EBITDA. "
    "The bank cash-flow basis already adds back scheduled debt paid, while noncash depreciation never reduces bank cash."
)
UNKNOWN_DEBT_SERVICE_REASON = (
    "Existing debt service is unknown; complete the debt schedule or set an explicit debt-service assumption."
)
UNKNOWN_NEW_LOAN_PAYMENT_REASON = (
    "The new loan payment is unknown; enter its rate and terms or a reviewed monthly payment."
)


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _round(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def normalize_add_backs(raw: Any) -> list[dict[str, Any]]:
    """The desk's add-backs: each a kind, a label, a monthly amount and why."""
    out: list[dict[str, Any]] = []
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "other")
        kind = kind if kind in ADD_BACK_KINDS else "other"
        amount = _num(item.get("amount_monthly"))
        if amount is None and _num(item.get("amount_annual")) is not None:
            amount = float(item["amount_annual"]) / 12
        if amount is None or amount <= 0:
            continue
        out.append(
            {
                "id": str(item.get("id") or f"{kind}:{index}")[:60],
                "kind": kind,
                "label": str(item.get("label") or ADD_BACK_KINDS[kind])[:120],
                "amount_monthly": round(amount, 2),
                "note": str(item.get("note") or "")[:300] or None,
                "source": str(item.get("source") or "desk")[:40],
            }
        )
    return out[:20]


def normalize_overrides(raw: Any) -> dict[str, Any]:
    """Figures the desk sets outright: the income available for debt service
    and the existing debt service, each with a note that says why."""
    raw = raw if isinstance(raw, dict) else {}
    income = _num(raw.get("income_annual"))
    service = _num(raw.get("debt_service_monthly"))
    return {
        "income_annual": income if income is not None and income >= 0 else None,
        "income_note": str(raw.get("income_note") or "")[:300] or None,
        "debt_service_monthly": service if service is not None and service >= 0 else None,
        "debt_service_note": str(raw.get("debt_service_note") or "")[:300] or None,
    }


def loan_payment(
    amount: float | None,
    rate_pct: float | None,
    term_months: int | None,
    interest_only: bool = False,
    *,
    structure: str | None = None,
    interest_only_months: int | None = None,
    repayment_config: dict | None = None,
) -> float | None:
    """The payment coverage stands on: interest alone for an interest-only
    loan, else the payment after any interest-only months, from the one loan
    schedule (simple interest under three years by default, amortizing from
    three years)."""
    from app.services.loan_schedule import build_schedule

    if not amount or amount <= 0 or rate_pct is None:
        return None
    kind = "interest_only" if interest_only else structure
    if kind != "interest_only" and not term_months:
        return None
    summary = build_schedule(
        float(amount),
        float(rate_pct or 0.0),
        int(term_months or 0),
        structure=kind,
        interest_only_months=interest_only_months or 0,
        repayment_config=repayment_config,
    )
    return summary["coverage_payment_monthly"] if summary else None


def _structure_of(raw: dict[str, Any]) -> str | None:
    from app.services.loan_schedule import STRUCTURES, default_structure

    term = int(raw["new_loan_term_months"]) if _num(raw.get("new_loan_term_months")) else None
    if raw.get("new_loan_interest_only"):
        return "interest_only"
    text = str(raw.get("new_loan_structure") or "").strip().lower()
    if text in STRUCTURES:
        return text
    return default_structure(term) if raw.get("new_loan_amount") else None


def _io_months_of(raw: dict[str, Any]) -> int:
    months = _num(raw.get("new_loan_interest_only_months"))
    term = _num(raw.get("new_loan_term_months"))
    if months is None or months < 0:
        return 0
    value = int(months)
    return min(value, int(term)) if term else value


def normalize_plan(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    from app.services.loan_schedule import normalize_repayment_config

    repayment_config = normalize_repayment_config(raw.get("new_loan_repayment_config"), raw.get("new_loan_term_months"),
        structure=_structure_of(raw), interest_only_months=_io_months_of(raw), periods_per_year=_num(raw.get("new_loan_payments_per_year")) or 12)
    retire = [str(v) for v in raw.get("retire") or [] if v]
    exclude = [str(v) for v in raw.get("exclude") or [] if v]
    payment = _num(raw.get("new_payment_monthly"))
    return {
        "retire": list(dict.fromkeys(retire)),
        "exclude": list(dict.fromkeys(exclude)),
        "new_payment_monthly": payment if payment is not None and payment >= 0 else None,
        "new_payment_source": str(raw.get("new_payment_source") or "") or None,
        "new_loan_amount": _num(raw.get("new_loan_amount")),
        "new_loan_rate_pct": _num(raw.get("new_loan_rate_pct")),
        "new_loan_term_months": int(raw["new_loan_term_months"])
        if _num(raw.get("new_loan_term_months"))
        else None,
        "new_loan_interest_only": bool(raw.get("new_loan_interest_only")),
        # Structure: simple interest under three years, amortizing from three
        # years, or interest-only; and how many of the term's months are
        # interest-only before principal starts.
        "new_loan_structure": _structure_of(raw),
        "new_loan_interest_only_months": _io_months_of(raw),
        "new_loan_repayment_config": repayment_config,
        "new_loan_payments_per_year": _num(raw.get("new_loan_payments_per_year")),
        "add_backs": normalize_add_backs(raw.get("add_backs")),
        "overrides": normalize_overrides(raw.get("overrides")),
        "note": str(raw.get("note") or "")[:600] or None,
        # Which recorded terms the loan fields were adopted from, if any.
        "terms_ref": dict(raw["terms_ref"])
        if isinstance(raw.get("terms_ref"), dict) and raw["terms_ref"].get("id")
        else None,
        "updated_at": raw.get("updated_at"),
        "updated_by": raw.get("updated_by"),
    }


def validate_plan_write(current: dict | None, payload) -> None:
    """Protect a configured full-write from old clients and stale editors."""
    from datetime import datetime

    from fastapi import HTTPException

    current = current or {}
    advanced = current.get("new_loan_repayment_config") is not None
    if advanced and "new_loan_repayment_config" not in payload.model_fields_set:
        raise HTTPException(409, "This plan contains a flexible repayment schedule. Reload and include the schedule, or explicitly send null to clear it.")
    if advanced or "expected_updated_at" in payload.model_fields_set:
        actual = current.get("updated_at")
        try:
            actual = datetime.fromisoformat(actual.replace("Z", "+00:00")) if isinstance(actual, str) else actual
        except ValueError:
            raise HTTPException(409, "The current plan timestamp requires review before replacing its flexible terms.") from None
        if payload.expected_updated_at != actual or advanced and payload.expected_updated_at is None:
            raise HTTPException(409, "This financial plan changed in another session. Reload before saving or clearing its repayment schedule.")


# ---------------------------------------------------------------------------
# Obligations: rows joined to payees
# ---------------------------------------------------------------------------


def _number(value: Any) -> float | None:
    """A float from what the reader printed, or None."""
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def obligations(debt: dict[str, Any], recurring: dict[str, Any]) -> list[dict[str, Any]]:
    """Explicit schedule membership, with statement observations beside each row.

    A recurring debit, an uploaded contract, or an extracted schedule line is
    evidence to review, not another confirmed obligation. The cockpit exposes
    those candidates separately; its provisional debt-service calculation can
    still use bank observations when there is no curated schedule.
    """
    service = debt.get("service") or {}
    authoritative = bool(service.get("schedule_authoritative"))
    rows = [row for row in debt.get("rows") or []
            if authoritative or row.get("origin") != "ai_draft"]
    payees = [p for p in recurring.get("payees") or [] if p.get("is_debt") or p.get("is_card")]
    by_key = {p["key"]: p for p in payees}
    used: set[str] = set()
    out: list[dict[str, Any]] = []
    source = service.get("source") or "none"
    rows_in_service = source in {"debt_schedule_rows", "debt_schedule_rows+bank_observed", "accepted_debt_schedule"}
    for row in rows:
        vendor_key = row.get("vendor_key")
        linked = by_key.get(vendor_key) if vendor_key not in used else None
        if linked is None:
            linked = next((p for p in payees if p["key"] not in used
                           and match_names(row["lender"], p["payee"])), None)
        if linked is not None:
            used.add(linked["key"])
        out.append({
            "id": f"row:{row.get('id')}",
            "kind": "row",
            "lender": row["lender"],
            "category": "merchant_cash_advance" if row.get("is_mca") else row.get("category") or "loan",
            "is_mca": bool(row.get("is_mca")),
            "monthly": row.get("monthly_payment"),
            "balance": row.get("balance"),
            **loan_progress_fields(row),
            "payoff": None if row.get("payoff_requires_review") else row.get("payoff_amount") if row.get("payoff_amount") is not None else row.get("balance"),
            "payoff_requires_review": bool(row.get("payoff_requires_review")),
            "payoff_review_reason": row.get("payoff_review_reason"),
            "in_debt_service": bool(row.get("count_in_dscr")) and rows_in_service,
            "count_in_dscr": bool(row.get("count_in_dscr")),
            "linked_payee_key": linked["key"] if linked else None,
            "linked_payee": linked["payee"] if linked else None,
            "observed_monthly": linked["monthly_average"] if linked else None,
            "cadence": row.get("payment_frequency") or (linked["cadence"] if linked else None),
            "source": "debt_schedule_row",
            "origin": row.get("origin"),
        })
    return out


def excludable_payees(recurring: dict[str, Any]) -> list[dict[str, Any]]:
    """Statement debits a desk may mark as not operating spend: transfers and
    owner draws large enough to matter."""
    return [
        p
        for p in recurring.get("payees") or []
        if p.get("kind") in EXCLUDABLE_KINDS
        and (p.get("monthly_average") or 0) >= EXCLUDE_MIN_MONTHLY
    ]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _payee_amount_in_month(
    payee: dict[str, Any] | None, month: str, months_read: list[str], fallback: float | None
) -> float:
    if payee is None:
        return float(fallback or 0.0)
    seen = (payee.get("monthly") or {}).get(month)
    if seen is not None:
        return float(seen)
    if month in months_read:
        return 0.0
    return float(payee.get("monthly_average") or fallback or 0.0)


def replay_months(
    months: list[dict[str, Any]],
    retired: list[dict[str, Any]],
    payees_by_key: dict[str, dict[str, Any]],
    months_read: list[str],
    new_payment: float | None,
) -> list[dict[str, Any]]:
    """The statements replayed with the retired payments taken out and the
    new payment put in, cash kept carried into every later ending balance.
    Lowest balances are estimated: prior months' carry plus half of the
    current month's change."""
    added = float(new_payment or 0.0)
    cumulative = 0.0
    rows: list[dict[str, Any]] = []
    for month in months:
        removed = 0.0
        for obligation in retired:
            payee = payees_by_key.get(obligation.get("linked_payee_key") or "")
            removed += _payee_amount_in_month(
                payee, month["month"], months_read, obligation.get("monthly")
            )
        removed = round(removed, 2)
        change = removed - added
        before = cumulative
        cumulative = round(cumulative + change, 2)
        withdrawals = month.get("withdrawals")
        deposits = month.get("deposits")
        withdrawals_after = (
            round(withdrawals - removed + added, 2) if withdrawals is not None else None
        )
        rows.append(
            {
                "month": month["month"],
                "label": month.get("label") or month_label(month["month"]),
                "deposits": deposits,
                "withdrawals": withdrawals,
                "withdrawals_after": withdrawals_after,
                "net": month.get("net"),
                "net_after": round(deposits - withdrawals_after, 2)
                if deposits is not None and withdrawals_after is not None
                else None,
                "ending": month.get("ending_balance"),
                "ending_after": round(month["ending_balance"] + cumulative, 2)
                if month.get("ending_balance") is not None
                else None,
                "lowest": month.get("low_balance"),
                "lowest_after": round(month["low_balance"] + before + change / 2, 2)
                if month.get("low_balance") is not None
                else None,
                "removed": removed,
                "added": added,
                "cumulative": cumulative,
            }
        )
    return rows


def evaluate_plan(
    plan: dict[str, Any],
    *,
    obligations_list: list[dict[str, Any]],
    recurring: dict[str, Any],
    months: list[dict[str, Any]],
    dscr: dict[str, Any],
    service: dict[str, Any],
    proposed: dict[str, Any] | None,
    requested_amount: float | None,
    ebitda: dict[str, Any] | None = None,
) -> dict[str, Any]:
    retire = set(plan.get("retire") or [])
    exclude = set(plan.get("exclude") or [])
    add_backs = plan.get("add_backs") or []
    overrides = plan.get("overrides") or {}
    payees_by_key = {p["key"]: p for p in recurring.get("payees") or []}
    retired = [o for o in obligations_list if o["id"] in retire]
    retired_in_service = [o for o in retired if o.get("in_debt_service")]
    retired_monthly = round(sum(o.get("monthly") or 0 for o in retired_in_service), 2)
    retirement_payment_unknown = any(_num(o.get("monthly")) is None for o in retired_in_service)
    payoff_known = [o.get("payoff") for o in retired if o.get("payoff") is not None]
    payoff_total = round(sum(payoff_known), 2) if payoff_known else None
    payoff_unknown = sum(1 for o in retired if o.get("payoff") is None)
    excluded = [
        payees_by_key[k.split(":", 1)[1]]
        for k in exclude
        if k.startswith("payee:") and k.split(":", 1)[1] in payees_by_key
    ]
    excluded_monthly = round(sum(p.get("monthly_average") or 0 for p in excluded), 2)
    excluded_add_backs = [{**item, "reason": DOUBLE_COUNTED_ADD_BACK_REASON}
                         for item in add_backs if item.get("kind") in DOUBLE_COUNTED_ADD_BACK_KINDS]
    add_backs_monthly = round(sum(a.get("amount_monthly") or 0 for a in add_backs
                                 if a.get("kind") not in DOUBLE_COUNTED_ADD_BACK_KINDS), 2)
    add_backs_annual = round(add_backs_monthly * 12, 2)
    income_override = overrides.get("income_annual")
    service_override = overrides.get("debt_service_monthly")
    interest_only = (
        bool(plan.get("new_loan_interest_only"))
        or plan.get("new_loan_structure") == "interest_only"
    )

    new_payment = plan.get("new_payment_monthly")
    source = plan.get("new_payment_source")
    loan_terms: dict[str, Any] | None = None
    schedule: dict[str, Any] | None = None
    if plan.get("new_loan_amount") and plan.get("new_loan_rate_pct") is not None and (plan.get("new_loan_term_months") or interest_only):
        from app.services.loan_schedule import build_schedule, describe

        schedule = build_schedule(
            float(plan["new_loan_amount"]),
            float(plan["new_loan_rate_pct"]),
            int(plan["new_loan_term_months"]) if plan.get("new_loan_term_months") else 0,
            structure="interest_only" if interest_only else plan.get("new_loan_structure"),
            interest_only_months=int(plan.get("new_loan_interest_only_months") or 0),
            repayment_config=plan.get("new_loan_repayment_config"),
            periods_per_year=plan.get("new_loan_payments_per_year") or 12,
        )
        loan_terms = {
            "amount": float(plan["new_loan_amount"]),
            "rate_pct": float(plan["new_loan_rate_pct"]),
            "term_months": int(plan["new_loan_term_months"])
            if plan.get("new_loan_term_months")
            else None,
            "interest_only": interest_only,
            "structure": schedule["structure"]
            if schedule
            else ("interest_only" if interest_only else plan.get("new_loan_structure")),
            "interest_only_months": schedule["interest_only_months"]
            if schedule
            else int(plan.get("new_loan_interest_only_months") or 0),
            "repayment_config": schedule.get("repayment_config") if schedule else plan.get("new_loan_repayment_config"),
            "payments_per_year": schedule.get("periods_per_year") if schedule else plan.get("new_loan_payments_per_year") or 12,
            "payment_io": schedule["payment_io_monthly"] if schedule else None,
            "payment_after": schedule["payment_after_monthly"] if schedule else None,
            "annual_debt_service": (schedule["coverage_annual_debt_service"] if plan.get("new_loan_repayment_config") is not None else schedule["annual_debt_service"]) if schedule else None,
            "total_interest": schedule["total_interest"] if schedule else None,
            "total_paid": schedule["total_paid"] if schedule else None,
            "balloon": schedule["balloon"] if schedule else None,
            "weighted_average_life_months": schedule["weighted_average_life_months"] if schedule else None,
            "describe": describe(schedule),
        }
        if schedule and schedule.get("repayment_config") is not None:
            loan_terms.update({key: schedule.get(key) for key in (
                "scheduled_balloons", "first_year_actual_debt_service", "peak_rolling_12_month_debt_service",
                "coverage_basis", "actual_payoff_month", "maturity_balloon",
            )})
            loan_terms["actual_regular_payment_monthly"] = schedule.get("regular_coverage_payment_monthly")
    if new_payment is None and loan_terms and schedule:
        new_payment = schedule["coverage_payment_monthly"]
        source = source or "manual_terms"
    elif schedule and plan.get("new_loan_repayment_config") is not None:
        # A manually entered regular payment cannot erase contractual balloon
        # cash flows from a configured schedule's coverage denominator.
        new_payment = max(float(new_payment or 0), schedule["coverage_payment_monthly"])
    # The recorded loan terms join the plan only when the desk adopts them:
    # a term sheet on file is not a decision, and adding its payment on its
    # own made "with the plan" worse than today with nothing planned.
    if (
        new_payment is None
        and source == "term_sheet"
        and not plan.get("new_loan_amount")
        and proposed
        and proposed.get("monthly_payment") is not None
    ):
        new_payment = proposed.get("monthly_payment")
        loan_terms = {
            "amount": _num(proposed.get("amount")),
            "rate_pct": _num(proposed.get("rate_pct")),
            "term_months": int(proposed["term_months"])
            if _num(proposed.get("term_months"))
            else None,
            "interest_only": False,
            "structure": proposed.get("structure"),
            "interest_only_months": proposed.get("interest_only_months"),
            "repayment_config": proposed.get("repayment_config"),
            "payments_per_year": proposed.get("payments_per_year") or 12,
            "source": "term_sheet",
            "label": proposed.get("label"),
        }
        if loan_terms.get("repayment_config") is not None and loan_terms["rate_pct"] is not None:
            from app.services.loan_schedule import build_schedule, describe

            schedule = build_schedule(loan_terms["amount"], loan_terms["rate_pct"], loan_terms["term_months"],
                structure=loan_terms["structure"], interest_only_months=loan_terms["interest_only_months"] or 0,
                periods_per_year=loan_terms["payments_per_year"], repayment_config=loan_terms["repayment_config"])
            if schedule:
                new_payment = max(float(new_payment or 0), schedule["coverage_payment_monthly"])
                loan_terms.update({key: schedule.get(key) for key in (
                    "scheduled_balloons", "first_year_actual_debt_service", "peak_rolling_12_month_debt_service",
                    "coverage_basis", "actual_payoff_month", "maturity_balloon", "total_paid", "total_interest", "balloon",
                )})
                loan_terms.update(interest_only=schedule["structure"] == "interest_only", structure=schedule["structure"],
                    payment_io=schedule["payment_io_monthly"], payment_after=schedule["payment_after_monthly"],
                    actual_regular_payment_monthly=schedule["regular_coverage_payment_monthly"],
                    annual_debt_service=schedule["coverage_annual_debt_service"], describe=describe(schedule))
    configured_schedule_unpriced = bool(((plan.get("new_loan_amount") and plan.get("new_loan_repayment_config") is not None)
        or ((loan_terms or {}).get("amount") and (loan_terms or {}).get("repayment_config") is not None)) and schedule is None)
    new_loan_payment_unknown = bool(plan.get("new_loan_amount") and new_payment is None) or configured_schedule_unpriced
    # The existing debt service: what the file shows, unless the desk set it.
    service_monthly = (
        float(service_override)
        if service_override is not None
        else _num(service.get("monthly"))
    )
    ref = plan.get("terms_ref") or {}
    retained = _num(ref.get("retained_annual_debt_service"))
    recorded_refinance_basis = bool(source == "recorded_terms" and ref.get("treatment") == "refinance" and retained is not None
        and (overrides.get("debt_service_note") or "").startswith("Refinance per ")
        and service_override is not None and abs(float(service_override) - round(retained / 12, 2)) < .005)
    configured_refinance = bool(schedule and schedule.get("repayment_config") is not None and recorded_refinance_basis)
    # Canonical recorded refinance terms already state the debt remaining
    # AFTER these payoffs. Preserve the payoff selections and their evidence,
    # without subtracting their payments from retained debt a second time.
    # Missing existing debt service is not a zero-debt declaration. Even a
    # fully priced new loan cannot establish the missing retained payments.
    before_new = (round(max(0.0, service_monthly if recorded_refinance_basis else service_monthly - retired_monthly), 2)
                  if service_monthly is not None and (recorded_refinance_basis or not retirement_payment_unknown) else None)
    after_monthly = round(max(0.0, before_new + float(new_payment or 0.0)), 2) if before_new is not None and not new_loan_payment_unknown else None
    after_annual = after_monthly * 12 if after_monthly is not None else None
    if before_new is not None and schedule and schedule.get("repayment_config") is not None:
        exact_new_annual = schedule["coverage_annual_debt_service"] if abs(float(new_payment or 0) - schedule["coverage_payment_monthly"]) < .005 else float(new_payment or 0) * 12
        exact_existing_annual = before_new * 12
        if configured_refinance:
            exact_existing_annual = retained
        after_annual = round(exact_existing_annual + exact_new_annual, 2)

    candidates_after = []
    for candidate in dscr.get("candidates") or []:
        numerator = candidate.get("numerator")
        if numerator is None:
            continue
        # Only additional operating adjustments raise earnings. Interest and
        # D&A have already been reversed in EBITDA (or are absent from bank
        # cash retained). Reversing them again would inflate coverage.
        adjusted = (
            numerator
            + add_backs_annual
            + (excluded_monthly * 12 if candidate.get("key") == "bank_cash_flow" else 0.0)
        )
        numerator_after = float(income_override) if income_override is not None else adjusted
        value = round(numerator_after / after_annual, 2) if after_annual is not None and after_annual > 0 else None
        candidates_after.append(
            {
                "key": candidate.get("key"),
                "label": candidate.get("label"),
                "numerator_before": numerator,
                "numerator_after": round(numerator_after, 2),
                "value_before": candidate.get("value"),
                "value_after": value,
                "status_after": dscr_status(value),
                "no_debt_left": after_monthly is not None and after_monthly <= 0 and numerator_after > 0,
                "overridden": income_override is not None,
            }
        )
    if income_override is not None and not candidates_after:
        # No document gives an income figure, but the desk set one: coverage
        # stands on the override alone and says so.
        value = (
            round(float(income_override) / after_annual, 2) if after_annual is not None and after_annual > 0 else None
        )
        candidates_after.append(
            {
                "key": "desk_override",
                "label": "Desk override",
                "numerator_before": None,
                "numerator_after": round(float(income_override), 2),
                "value_before": None,
                "value_after": value,
                "status_after": dscr_status(value),
                "no_debt_left": after_monthly is not None and after_monthly <= 0 and float(income_override) > 0,
                "overridden": True,
            }
        )
    headline = dscr.get("headline") or {}
    headline_key = headline.get("key")
    headline_after = next(
        (c for c in candidates_after if c["key"] == headline_key),
        candidates_after[0] if candidates_after else None,
    )
    rows = replay_months(
        months, retired, payees_by_key, recurring.get("months_read") or [], new_payment
    )
    net_before = [r["net"] for r in rows if r["net"] is not None]
    net_after = [r["net_after"] for r in rows if r["net_after"] is not None]
    active = bool(
        retire
        or exclude
        or add_backs
        or income_override is not None
        or service_override is not None
        or plan.get("new_payment_monthly") is not None
        or plan.get("new_loan_amount")
        or (plan.get("new_payment_source") == "term_sheet" and new_payment is not None)
    )
    risk = None
    if schedule and before_new is not None:
        from app.services.loan_schedule import risk_profile

        income_after = headline_after["numerator_after"] if headline_after else None
        risk_basis = next((candidate for candidate in dscr.get("candidates") or []
                           if headline_after and candidate.get("key") == headline_after["key"]), headline)
        risk = risk_profile(
            schedule,
            income_monthly=(float(income_after) / 12.0) if income_after is not None else None,
            existing_service_monthly=before_new,
            income_currency_comparable=income_override is not None or bool(risk_basis.get("currency_comparable")),
        )
    result = {
        "active": active,
        "risk": risk,
        "retired_ids": sorted(retire),
        "excluded_ids": sorted(exclude),
        "retired_monthly": retired_monthly,
        "retired_count": len(retired),
        "payoff_total": payoff_total,
        "payoff_unknown": payoff_unknown,
        "excluded_monthly": excluded_monthly,
        "add_backs_monthly": add_backs_monthly,
        "add_backs_annual": add_backs_annual,
        "excluded_add_backs": excluded_add_backs,
        "coverage_unavailable_reason": UNKNOWN_DEBT_SERVICE_REASON if before_new is None else UNKNOWN_NEW_LOAN_PAYMENT_REASON if new_loan_payment_unknown else None,
        "income_override_annual": income_override,
        "debt_service_override_monthly": service_override,
        "interest_only": interest_only,
        "loan_terms": loan_terms,
        "schedule": {k: v for k, v in schedule.items() if k not in {"rows", "cash_flows"}} if schedule else None,
        "schedule_rows": schedule["rows"] if schedule else [],
        "new_payment_monthly": _round(float(new_payment)) if new_payment is not None else None,
        "new_payment_source": source if new_payment is not None else None,
        "loan_amount_needed": _round(max(payoff_total or 0.0, requested_amount or 0.0)) or None,
        "debt_service_before_monthly": _round(service_monthly),
        "debt_service_before_new_payment_monthly": before_new,
        "debt_service_after_monthly": after_monthly,
        "debt_service_after_annual": _round(after_annual),
        "candidates": candidates_after,
        "income_before_annual": headline_after["numerator_before"] if headline_after else None,
        "income_after_annual": headline_after["numerator_after"] if headline_after else None,
        "headline_before": headline.get("value"),
        "headline_before_status": headline.get("status") or dscr_status(headline.get("value")),
        "headline_after": headline_after["value_after"] if headline_after else None,
        "headline_after_status": headline_after["status_after"]
        if headline_after
        else "unavailable",
        "headline_key": headline_key or (headline_after["key"] if headline_after else None),
        "no_debt_left": bool(headline_after and headline_after["no_debt_left"]),
        "replay": rows,
        "replay_basis": "coverage_payment_proxy" if schedule and schedule.get("repayment_config") is not None else "constant_monthly_payment",
        "replay_note": "Projected bank balances use a coverage-normalized payment proxy, not the actual monthly cash due. No loan commencement date is aligned to the historical statements; use the loan-month schedule for scheduled balloons and maturity cash flows." if schedule and schedule.get("repayment_config") is not None else None,
        "actual_regular_payment_monthly": schedule.get("regular_coverage_payment_monthly") if schedule else None,
        "net_before_avg": _round(sum(net_before) / len(net_before)) if net_before else None,
        "net_after_avg": _round(sum(net_after) / len(net_after)) if net_after else None,
        "ending_before_last": rows[-1]["ending"] if rows else None,
        "ending_after_last": rows[-1]["ending_after"] if rows else None,
        "months_dipping_negative_before": sum(
            1 for r in rows if r["lowest"] is not None and r["lowest"] < 0
        ),
        "months_dipping_negative_after": sum(
            1 for r in rows if r["lowest_after"] is not None and r["lowest_after"] < 0
        ),
        "cash_kept": rows[-1]["cumulative"] if rows else 0.0,
    }
    from app.services.financial_plan_earnings import scenario_ebitda

    result["ebitda"] = scenario_ebitda(ebitda, plan, result, dscr, service)
    return result


def describe_modifications(
    plan: dict[str, Any],
    evaluation: dict[str, Any],
    obligations_list: list[dict[str, Any]],
    recurring: dict[str, Any],
    accounts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The plan in plain words, one line per change, for the package, the
    listing and the review: what the desk changed and what it does to the
    monthly figures. The order is the order a reader needs: what stops, what
    is counted back, what is set outright, what is added, which accounts."""
    lines: list[dict[str, Any]] = []
    by_id = {o["id"]: o for o in obligations_list}
    for oid in evaluation.get("retired_ids") or []:
        o = by_id.get(oid)
        if o is None:
            continue
        monthly = float(o.get("monthly") or 0)
        payoff = o.get("payoff")
        lines.append(
            {
                "kind": "retire",
                "label": f"Pay off {o['lender']} with the new loan",
                "detail": f"Its {monthly:,.0f} a month stops"
                + (
                    f"; payoff about {float(payoff):,.0f}."
                    if payoff
                    else "; payoff balance not recorded."
                ),
                "monthly_effect": _round(-monthly),
                "annual_effect": _round(-monthly * 12),
            }
        )
    payees = {p["key"]: p for p in recurring.get("payees") or []}
    for pid in evaluation.get("excluded_ids") or []:
        payee = payees.get(pid.split(":", 1)[1] if ":" in pid else pid)
        if payee is None:
            continue
        monthly = float(payee.get("monthly_average") or 0)
        lines.append(
            {
                "kind": "exclude",
                "label": f"{payee['payee']} is not operating spend",
                "detail": f"{monthly:,.0f} a month of {payee.get('kind_label') or 'transfers'} counted back as income on the bank basis.",
                "monthly_effect": _round(monthly),
                "annual_effect": _round(monthly * 12),
            }
        )
    for item in plan.get("add_backs") or []:
        monthly = float(item.get("amount_monthly") or 0)
        excluded = item.get("kind") in DOUBLE_COUNTED_ADD_BACK_KINDS
        lines.append(
            {
                "kind": "add_back",
                "label": f"{'Not applied: ' if excluded else 'Add back '}{item.get('label') or ADD_BACK_KINDS.get(item.get('kind') or 'other')}",
                "detail": DOUBLE_COUNTED_ADD_BACK_REASON if excluded else item.get("note")
                or ADD_BACK_KINDS.get(
                    item.get("kind") or "other",
                    "Added back to the income available for debt service.",
                ),
                "monthly_effect": 0.0 if excluded else _round(monthly),
                "annual_effect": 0.0 if excluded else _round(monthly * 12),
            }
        )
    overrides = plan.get("overrides") or {}
    if overrides.get("income_annual") is not None:
        lines.append(
            {
                "kind": "override_income",
                "label": f"Income available for debt service set to {float(overrides['income_annual']):,.0f} a year",
                "detail": overrides.get("income_note")
                or "Set by the desk in place of the documents' figure.",
                "monthly_effect": None,
                "annual_effect": None,
            }
        )
    if overrides.get("debt_service_monthly") is not None:
        lines.append(
            {
                "kind": "override_debt_service",
                "label": f"Existing debt service set to {float(overrides['debt_service_monthly']):,.0f} a month",
                "detail": overrides.get("debt_service_note")
                or "Set by the desk in place of the schedule's figure.",
                "monthly_effect": None,
                "annual_effect": None,
            }
        )
    payment = evaluation.get("new_payment_monthly")
    loan = evaluation.get("loan_terms")
    if payment:
        ref = plan.get("terms_ref") if isinstance(plan.get("terms_ref"), dict) else None
        if ref and plan.get("new_payment_source") == "recorded_terms":
            lines.append(
                {
                    "kind": "loan",
                    "label": f"New payment {payment:,.0f} a month from {ref.get('label') or 'the recorded terms'}",
                    "detail": (
                        f"{loan['amount']:,.0f} at {loan['rate_pct']:g}%"
                        + (
                            " interest-only"
                            if loan.get("interest_only")
                            else (
                                f" over {loan['term_months']} months"
                                if loan.get("term_months")
                                else ""
                            )
                        )
                        + ", as recorded on the Underwriting tab."
                        if loan and loan.get("amount")
                        else "As recorded on the Underwriting tab."
                    ),
                    "monthly_effect": _round(-float(payment)),
                    "annual_effect": _round(-float(payment) * 12),
                }
            )
        elif loan and loan.get("source") == "term_sheet":
            lines.append(
                {
                    "kind": "loan",
                    "label": f"New payment {payment:,.0f} a month from the recorded loan terms",
                    "detail": (loan.get("label") or "Term sheet")
                    + (
                        f": {loan['amount']:,.0f} at {loan['rate_pct']:g}%"
                        if loan.get("amount")
                        else ""
                    )
                    + (f" over {loan['term_months']} months" if loan.get("term_months") else "")
                    + ".",
                    "monthly_effect": _round(-float(payment)),
                    "annual_effect": _round(-float(payment) * 12),
                }
            )
        elif loan:
            structure = loan.get("describe") or (
                "interest-only"
                if loan.get("interest_only")
                else f"over {loan.get('term_months')} months"
            )
            if loan.get("payment_io") is not None and loan.get("payment_after") is not None:
                detail = f"{loan['payment_io']:,.0f} a month for {loan.get('interest_only_months')} months, then {loan['payment_after']:,.0f} a month."
            elif loan.get("interest_only"):
                detail = f"{payment:,.0f} a month of interest; the principal is due at maturity."
            else:
                detail = f"{payment:,.0f} a month."
            lines.append(
                {
                    "kind": "loan",
                    "label": f"New loan {loan['amount']:,.0f} at {loan['rate_pct']:g}% {structure}",
                    "detail": detail,
                    "monthly_effect": _round(-float(payment)),
                    "annual_effect": _round(-float(payment) * 12),
                }
            )
        else:
            source = {
                "term_sheet": "from the recorded loan terms",
                "manual": "typed by the desk",
                "offer": "from the offer",
            }.get(evaluation.get("new_payment_source") or "", "typed by the desk")
            lines.append(
                {
                    "kind": "loan",
                    "label": f"New payment {payment:,.0f} a month",
                    "detail": f"The new loan's payment, {source}.",
                    "monthly_effect": _round(-float(payment)),
                    "annual_effect": _round(-float(payment) * 12),
                }
            )
    if accounts:
        counted = [a["label"] for a in accounts if a.get("counted")]
        left_out = [a["label"] for a in accounts if not a.get("counted")]
        if left_out:
            lines.append(
                {
                    "kind": "accounts",
                    "label": "Accounts counted: " + ", ".join(counted),
                    "detail": "Not counted: " + ", ".join(left_out) + ".",
                    "monthly_effect": None,
                    "annual_effect": None,
                }
            )
    return lines


def plan_impact(
    evaluation: dict[str, Any], dscr: dict[str, Any], accounts: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Original, with the plan, and the difference: the figures a package or
    a listing prints side by side."""
    headline = dscr.get("headline") or {}
    loan = evaluation.get("loan_terms")
    payment = evaluation.get("new_payment_monthly")

    def pair(before: Any, after: Any) -> dict[str, Any]:
        change = (
            _round(float(after) - float(before))
            if before is not None and after is not None
            else None
        )
        return {"before": before, "after": after, "change": change}

    return {
        "active": bool(evaluation.get("active")),
        "dscr": {
            **pair(headline.get("value"), evaluation.get("headline_after")),
            "basis": evaluation.get("headline_key"),
            "status_before": headline.get("status") or dscr_status(headline.get("value")),
            "status_after": evaluation.get("headline_after_status"),
            "no_debt_left": bool(evaluation.get("no_debt_left")),
        },
        "income_annual": pair(
            evaluation.get("income_before_annual"), evaluation.get("income_after_annual")
        ),
        "ebitda": evaluation.get("ebitda"),
        "debt_service_monthly": pair(
            evaluation.get("debt_service_before_monthly"),
            evaluation.get("debt_service_after_monthly"),
        ),
        "net_cash_flow_monthly": pair(
            evaluation.get("net_before_avg"), evaluation.get("net_after_avg")
        ),
        "ending_balance": pair(
            evaluation.get("ending_before_last"), evaluation.get("ending_after_last")
        ),
        "months_negative": pair(
            evaluation.get("months_dipping_negative_before"),
            evaluation.get("months_dipping_negative_after"),
        ),
        "cash_kept": evaluation.get("cash_kept"),
        "add_backs_monthly": evaluation.get("add_backs_monthly"),
        "retired_monthly": evaluation.get("retired_monthly"),
        "excluded_monthly": evaluation.get("excluded_monthly"),
        "loan": {
            **(loan or {}),
            "payment_monthly": payment,
            "source": evaluation.get("new_payment_source"),
        }
        if payment
        else None,
        "accounts": {
            "counted": [a["label"] for a in accounts if a.get("counted")],
            "not_counted": [a["label"] for a in accounts if not a.get("counted")],
        }
        if accounts
        else None,
    }


# ---------------------------------------------------------------------------
# Levers: what would raise coverage
# ---------------------------------------------------------------------------


def _dscr_for(numerator: float | None, service_monthly: float) -> float | None:
    if numerator is None or service_monthly <= 0:
        return None
    return round(numerator / (service_monthly * 12), 2)


def coverage_levers(
    plan: dict[str, Any],
    evaluation: dict[str, Any],
    *,
    obligations_list: list[dict[str, Any]],
    recurring: dict[str, Any],
    dscr: dict[str, Any],
    requested_amount: float | None,
    terms: dict[str, Any] | None,
    tax_years: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Actions, each with the coverage it would leave, ranked best first.
    Every lever is a change to the plan the desk can apply with one click,
    or a document the file is missing."""
    if evaluation.get("debt_service_after_monthly") is None:
        return [{"key": "debt_schedule", "label": "Complete the debt schedule",
                 "detail": UNKNOWN_DEBT_SERVICE_REASON, "dscr_after": None,
                 "status_after": "unavailable", "uncertain": True, "improves": False, "apply": None}]
    headline = dscr.get("headline") or {}
    numerator = headline.get("numerator")
    if numerator is None:
        candidates = dscr.get("candidates") or []
        numerator = candidates[0].get("numerator") if candidates else None
    # Levers stack on the plan as it stands: the income after its add-backs
    # and exclusions, the debt service after its retirements.
    if evaluation.get("income_after_annual") is not None:
        numerator = evaluation["income_after_annual"]
    overridden = evaluation.get("income_override_annual") is not None
    key = evaluation.get("headline_key") or headline.get("key") or ((dscr.get("candidates") or [{}])[0].get("key"))
    earnings_basis = next((candidate for candidate in dscr.get("candidates") or [] if candidate.get("key") == key), headline)
    base_service = evaluation["debt_service_after_monthly"]
    base_value = evaluation["headline_after"]
    recorded_rate = _num((terms or {}).get("rate_pct"))
    rate = recorded_rate if recorded_rate is not None else DEFAULT_RATE_PCT
    term = int((terms or {}).get("term_months") or DEFAULT_TERM_MONTHS)
    retire = set(plan.get("retire") or [])
    exclude = set(plan.get("exclude") or [])
    levers: list[dict[str, Any]] = []

    advances = [
        o
        for o in obligations_list
        if o.get("is_mca") and o.get("in_debt_service") and o["id"] not in retire
    ]
    if advances and numerator is not None:
        payoff = sum(o.get("payoff") or 0 for o in advances)
        unknown = [o for o in advances if o.get("payoff") is None]
        amount = max(payoff, requested_amount or 0.0) or None
        payment = amortized_payment(amount, rate, term) if amount else None
        after = round(
            max(0.0, base_service - sum(o.get("monthly") or 0 for o in advances) + (payment or 0)),
            2,
        )
        value = _dscr_for(numerator, after)
        sizing = (
            f"The loan covers {payoff:,.0f} of payoffs"
            + (
                f" plus the {requested_amount:,.0f} requested"
                if requested_amount and requested_amount > payoff
                else ""
            )
            + f"; at {rate:g}% over {term} months that is {payment:,.0f} a month."
            if payment
            else "Record the payoff balances to size the loan."
        )
        if unknown and payment:
            sizing += (
                f" Payoff balances are not recorded for {len(unknown)} of {len(advances)} advances, so the loan is sized at the amount requested"
                " and this coverage is optimistic until they are."
            )
        levers.append(
            {
                "key": "refinance_advances",
                "label": f"Refinance every advance into the new loan ({len(advances)} funder{'s' if len(advances) != 1 else ''})",
                "detail": f"Payments stop: {sum(o.get('monthly') or 0 for o in advances):,.0f} a month. "
                + sizing,
                "dscr_after": value,
                "income_after_annual": numerator,
                "debt_service_after_monthly": after,
                "status_after": dscr_status(value),
                "uncertain": bool(unknown),
                "apply": {
                    "retire": sorted(retire | {o["id"] for o in advances}),
                    "new_loan_amount": amount,
                    "new_loan_rate_pct": rate,
                    "new_loan_term_months": term,
                },
            }
        )
        for o in sorted(advances, key=lambda x: -(x.get("monthly") or 0)):
            after_one = round(
                max(
                    0.0,
                    base_service
                    - (o.get("monthly") or 0)
                    + (evaluation.get("new_payment_monthly") or 0) * 0,
                ),
                2,
            )
            value_one = _dscr_for(numerator, after_one)
            levers.append(
                {
                    "key": f"payoff:{o['id']}",
                    "label": f"Pay off {o['lender']} alone",
                    "detail": f"Removes {o.get('monthly') or 0:,.0f} a month"
                    + (
                        f"; payoff about {o['payoff']:,.0f}."
                        if o.get("payoff")
                        else "; payoff balance not recorded yet."
                    ),
                    "dscr_after": value_one,
                    "income_after_annual": numerator,
                    "debt_service_after_monthly": after_one,
                    "status_after": dscr_status(value_one),
                    "apply": {"retire": sorted(retire | {o["id"]})},
                }
            )
    # The recorded loan terms, offered rather than assumed: the desk sees what
    # adding them does and, when the sheet refinances the schedule, what
    # stopping the existing payments does with them.
    sheet_payment = _num((terms or {}).get("monthly_payment")) if terms else None
    if (
        sheet_payment is None
        and terms
        and _num(terms.get("amount"))
        and _num(terms.get("term_months"))
        and _num(terms.get("rate_pct")) is not None
    ):
        sheet_payment = amortized_payment(
            float(terms["amount"]), float(terms["rate_pct"]), int(terms["term_months"])
        )
    adopted = (
        plan.get("new_payment_source") == "term_sheet"
        or plan.get("new_payment_monthly") is not None
        or plan.get("new_loan_amount")
    )
    if sheet_payment and numerator is not None and not adopted:
        sheet_label = (terms or {}).get("label") or "the recorded loan terms"
        sheet_terms = (
            f"{float(terms['amount']):,.0f} at {float(terms.get('rate_pct') or 0):g}% over {int(terms['term_months'])} months"
            if terms and _num(terms.get("amount")) and _num(terms.get("term_months")) and _num(terms.get("rate_pct")) is not None
            else f"{sheet_payment:,.0f} a month"
        )
        adopt = {
            "new_payment_source": "term_sheet",
            "new_payment_monthly": None,
            "new_loan_amount": None,
            "new_loan_rate_pct": None,
            "new_loan_term_months": None,
            "new_loan_interest_only": False,
            "new_loan_repayment_config": None,
            "new_loan_payments_per_year": None,
        }
        after_add = round(
            max(0.0, evaluation["debt_service_before_new_payment_monthly"] + sheet_payment), 2
        )
        value_add = _dscr_for(numerator, after_add)
        treatment = str((terms or {}).get("treatment") or "additive")
        levers.append(
            {
                "key": "term_sheet:add",
                "label": "Add the recorded loan terms as the new payment",
                "detail": f"{sheet_label}: {sheet_terms} is {sheet_payment:,.0f} a month"
                + (
                    f", on top of today's {evaluation['debt_service_before_new_payment_monthly']:,.0f}: the sheet treats it as additional debt. Tick what it pays off if it refinances the schedule."
                    if treatment != "refinance"
                    else "."
                ),
                "dscr_after": value_add,
                "income_after_annual": numerator,
                "debt_service_after_monthly": after_add,
                "status_after": dscr_status(value_add),
                "apply": adopt,
            }
        )
        in_service = [
            o for o in obligations_list if o.get("in_debt_service") and o["id"] not in retire
        ]
        retained = _num((terms or {}).get("retained_annual_debt_service"))
        if treatment == "refinance" and in_service and retained is not None:
            after_refi = round(max(0.0, retained / 12 + sheet_payment), 2)
            value_refi = _dscr_for(numerator, after_refi)
            levers.append(
                {
                    "key": "term_sheet:refinance",
                    "label": "Refinance the schedule into the recorded loan terms",
                    "detail": f"{sheet_label} replaces the existing payments: {sum(o.get('monthly') or 0 for o in in_service):,.0f} a month stops"
                    + (
                        f", {retained / 12:,.0f} a month is retained per the sheet"
                        if retained
                        else ""
                    )
                    + f", and {sheet_payment:,.0f} a month begins.",
                    "dscr_after": value_refi,
                    "income_after_annual": numerator,
                    "debt_service_after_monthly": after_refi,
                    "status_after": dscr_status(value_refi),
                    "apply": {**adopt, "retire": sorted(retire | {o["id"] for o in in_service})},
                }
            )
    existing_kinds = {a.get("kind") for a in plan.get("add_backs") or []}
    latest_comp = next(
        (y for y in reversed(tax_years or []) if (y.get("officer_compensation") or 0) > 0), None
    )
    if (
        latest_comp
        and numerator is not None
        and "owner_salary" not in existing_kinds
        and not overridden
    ):
        comp = float(latest_comp["officer_compensation"])
        value_add = _dscr_for(numerator + comp, base_service)
        levers.append(
            {
                "key": "addback:officer_compensation",
                "label": "Add back officer compensation",
                "detail": f"The {latest_comp.get('year')} return deducts {comp:,.0f} of officer compensation. When the owner's pay is discretionary a lender adds it back to the income available for debt service; confirm with the client that it is.",
                "dscr_after": value_add,
                "income_after_annual": numerator + comp,
                "debt_service_after_monthly": base_service,
                "status_after": dscr_status(value_add),
                "apply": {
                    "add_backs": [
                        *(plan.get("add_backs") or []),
                        {
                            "kind": "owner_salary",
                            "label": f"Officer compensation (tax return {latest_comp.get('year')})",
                            "amount_monthly": round(comp / 12, 2),
                            "source": "tax_return",
                        },
                    ]
                },
            }
        )
    loan = evaluation.get("loan_terms")
    if (
        loan
        and loan.get("repayment_config") is None
        and not loan.get("interest_only")
        and loan.get("rate_pct")
        and numerator is not None
        and evaluation.get("new_payment_monthly")
    ):
        io_payment = loan_payment(loan["amount"], loan["rate_pct"], None, True) or 0.0
        after_io = round(
            max(0.0, evaluation["debt_service_before_new_payment_monthly"] + io_payment), 2
        )
        value_io = _dscr_for(numerator, after_io)
        levers.append(
            {
                "key": "interest_only",
                "label": "Structure the new loan interest-only",
                "detail": f"{loan['amount']:,.0f} at {loan['rate_pct']:g}% costs {io_payment:,.0f} a month of interest instead of {evaluation['new_payment_monthly']:,.0f}; the principal is due at maturity, so the coverage holds only through the interest-only period.",
                "dscr_after": value_io,
                "income_after_annual": numerator,
                "debt_service_after_monthly": after_io,
                "status_after": dscr_status(value_io),
                "apply": {
                    "new_loan_interest_only": True,
                    "new_loan_structure": "interest_only",
                    "new_loan_interest_only_months": None,
                    "new_payment_monthly": None,
                },
            }
        )
    if key == "bank_cash_flow" and numerator is not None and not overridden:
        for payee in excludable_payees(recurring):
            pid = f"payee:{payee['key']}"
            if pid in exclude:
                continue
            value_ex = _dscr_for(numerator + (payee.get("monthly_average") or 0) * 12, base_service)
            levers.append(
                {
                    "key": f"exclude:{payee['key']}",
                    "label": f"Treat {payee['payee']} as a transfer, not spend",
                    "detail": f"{payee.get('monthly_average') or 0:,.0f} a month of {payee.get('kind_label') or 'transfers'}: if this is money moved to the owner's own accounts it is not operating spend and the bank proxy understates income by that much. Confirm with the client.",
                    "dscr_after": value_ex,
                    "income_after_annual": numerator + (payee.get("monthly_average") or 0) * 12,
                    "debt_service_after_monthly": base_service,
                    "status_after": dscr_status(value_ex),
                    "apply": {"exclude": sorted(exclude | {pid})},
                }
            )
    new_payment = evaluation.get("new_payment_monthly")
    if new_payment and evaluation.get("loan_amount_needed") and numerator is not None:
        amount = float(evaluation["loan_amount_needed"])
        for longer in (120, 180):
            if longer <= term:
                continue
            payment = amortized_payment(amount, rate, longer)
            if payment is None:
                continue
            after_long = round(
                max(0.0, evaluation["debt_service_before_new_payment_monthly"] + payment), 2
            )
            value_long = _dscr_for(numerator, after_long)
            levers.append(
                {
                    "key": f"term:{longer}",
                    "label": f"Stretch the new loan to {longer} months",
                    "detail": f"{amount:,.0f} at {rate:g}% over {longer} months is {payment:,.0f} a month instead of {new_payment:,.0f}.",
                    "dscr_after": value_long,
                    "income_after_annual": numerator,
                    "debt_service_after_monthly": after_long,
                    "status_after": dscr_status(value_long),
                    "apply": {
                        "new_loan_amount": amount,
                        "new_loan_rate_pct": rate,
                        "new_loan_term_months": longer,
                        "new_payment_monthly": None,
                    },
                }
            )
    if key == "bank_cash_flow":
        draws = [
            p for p in recurring.get("payees") or [] if p.get("kind") in {"owner_draw", "transfer"}
        ]
        total = sum(p.get("monthly_average") or 0 for p in draws)
        levers.append(
            {
                "key": "income_document",
                "label": "Put a tax return or a profit-and-loss statement on file",
                "detail": "Coverage stands on bank cash flow, a floor: every outflow counts as spend"
                + (f", including {total:,.0f} a month of transfers and draws" if total else "")
                + ". A filed return or a P&L gives EBITDA, which is what a lender underwrites.",
                "dscr_after": None,
                "status_after": "unavailable",
                "apply": None,
            }
        )
    if plan.get("new_loan_repayment_config") is not None:
        # Scalar quote shortcuts cannot reprice a granular schedule. Staff can
        # edit its maturity, rate and events in the schedule editor instead.
        levers = [lever for lever in levers if not isinstance(lever.get("apply"), dict)
                  or not any(key.startswith("new_loan_") for key in lever["apply"])]
    currency_comparable = overridden or bool(earnings_basis.get("currency_comparable"))
    base_surplus = numerator / 12 - base_service if numerator is not None and currency_comparable else None
    nonpositive_income = numerator is not None and numerator <= 0
    for lever in levers:
        income_after = lever.setdefault("income_after_annual", None)
        service_after = lever.setdefault("debt_service_after_monthly", None)
        surplus = round(income_after / 12 - service_after, 2) if income_after is not None and service_after is not None and currency_comparable else None
        lever["cash_surplus_monthly"] = surplus
        lever["cash_surplus_change_monthly"] = round(surplus - base_surplus, 2) if surplus is not None and base_surplus is not None else None
        lever.setdefault("uncertain", False)
        # With a loss, adding debt makes the signed ratio approach zero while
        # worsening the funding gap. Rank payment/earnings changes by their
        # cash effect instead; never change the correctly signed DSCR itself.
        if nonpositive_income:
            change = lever["cash_surplus_change_monthly"]
            lever["improves"] = (change > 0 if change is not None else
                                 service_after is not None and service_after < base_service and income_after == numerator)
        else:
            lever["improves"] = lever["dscr_after"] is not None and (base_value is None or lever["dscr_after"] > base_value)

    def payment_relief_order(lever):
        change = lever["cash_surplus_change_monthly"]
        # Payment relief alone remains comparable in USD even when the source
        # earnings denomination is unconfirmed. Never subtract those earnings.
        if change is None and lever["debt_service_after_monthly"] is not None and lever["income_after_annual"] == numerator:
            change = base_service - lever["debt_service_after_monthly"]
        return change is None, -change if change is not None else 0

    ranked = sorted(levers, key=payment_relief_order) if nonpositive_income else sorted(
        levers, key=lambda lever: -(lever["dscr_after"] if lever["dscr_after"] is not None else -1)
    )
    return ranked[:MAX_LEVERS]
