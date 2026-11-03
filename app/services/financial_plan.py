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
from app.services.payee_classification import match_names

TARGET_DSCR = 1.25
#: Payees worth suggesting as "not operating spend" start here.
EXCLUDE_MIN_MONTHLY = 500.0
#: Kinds a desk may plausibly mark as not operating spend.
EXCLUDABLE_KINDS = frozenset({"transfer", "owner_draw"})
DEFAULT_RATE_PCT = 16.0
DEFAULT_TERM_MONTHS = 120
MAX_LEVERS = 6


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


def normalize_plan(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
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
        "new_loan_term_months": int(raw["new_loan_term_months"]) if _num(raw.get("new_loan_term_months")) else None,
        "note": str(raw.get("note") or "")[:600] or None,
        "updated_at": raw.get("updated_at"),
        "updated_by": raw.get("updated_by"),
    }


# ---------------------------------------------------------------------------
# Obligations: rows joined to payees
# ---------------------------------------------------------------------------


def obligations(debt: dict[str, Any], recurring: dict[str, Any]) -> list[dict[str, Any]]:
    """One list of what the business pays lenders, whichever source the debt
    service came from, each entry carrying the monthly amount it puts in the
    denominator and the statement payee it is linked to."""
    rows = debt.get("rows") or []
    service = debt.get("service") or {}
    payees = [p for p in recurring.get("payees") or [] if p.get("is_debt") or p.get("is_card")]
    by_key = {p["key"]: p for p in payees}
    used: set[str] = set()
    out: list[dict[str, Any]] = []
    source = service.get("source") or "none"
    rows_in_service = source in {"debt_schedule_rows", "debt_schedule_rows+bank_observed"}
    for row in rows:
        linked = None
        vendor_key = row.get("vendor_key")
        if vendor_key and vendor_key in by_key and vendor_key not in used:
            linked = by_key[vendor_key]
        else:
            linked = next((p for p in payees if p["key"] not in used and match_names(row["lender"], p["payee"])), None)
        if linked is not None:
            used.add(linked["key"])
        counted = bool(row.get("count_in_dscr")) and rows_in_service
        out.append(
            {
                "id": f"row:{row.get('id')}",
                "kind": "row",
                "lender": row["lender"],
                "category": "merchant_cash_advance" if row.get("is_mca") else row.get("category") or "loan",
                "is_mca": bool(row.get("is_mca")),
                "monthly": row.get("monthly_payment"),
                "balance": row.get("balance"),
                "payoff": row.get("payoff_amount") if row.get("payoff_amount") is not None else row.get("balance"),
                "in_debt_service": counted,
                "count_in_dscr": bool(row.get("count_in_dscr")),
                "linked_payee_key": linked["key"] if linked else None,
                "linked_payee": linked["payee"] if linked else None,
                "observed_monthly": linked["monthly_average"] if linked else None,
                "cadence": row.get("payment_frequency") or (linked["cadence"] if linked else None),
                "source": "debt_schedule_row",
                "origin": row.get("origin"),
            }
        )
    if source == "advance_terms":
        for index, doc in enumerate(debt.get("documented") or []):
            linked = next((p for p in payees if p["key"] not in used and match_names(doc["lender"], p["payee"])), None)
            if linked is not None:
                used.add(linked["key"])
            out.append(
                {
                    "id": f"doc:{index}",
                    "kind": "document",
                    "lender": doc["lender"],
                    "category": "merchant_cash_advance" if doc.get("is_mca") else "loan",
                    "is_mca": bool(doc.get("is_mca")),
                    "monthly": doc.get("monthly_payment"),
                    "balance": doc.get("balance"),
                    "payoff": doc.get("balance"),
                    "in_debt_service": True,
                    "count_in_dscr": True,
                    "linked_payee_key": linked["key"] if linked else None,
                    "linked_payee": linked["payee"] if linked else None,
                    "observed_monthly": linked["monthly_average"] if linked else None,
                    "cadence": doc.get("payment_frequency") or (linked["cadence"] if linked else None),
                    "source": "advance_terms",
                    "origin": "document",
                }
            )
    for payee in payees:
        if payee["key"] in used:
            continue
        in_service = payee.get("is_debt") and source in {"bank_observed", "debt_schedule_rows+bank_observed"}
        out.append(
            {
                "id": f"payee:{payee['key']}",
                "kind": "payee",
                "lender": payee["payee"],
                "category": payee["kind"],
                "is_mca": payee["kind"] == "merchant_cash_advance",
                "monthly": payee.get("monthly_average"),
                "balance": None,
                "payoff": None,
                "in_debt_service": bool(in_service),
                "count_in_dscr": bool(payee.get("is_debt")),
                "linked_payee_key": payee["key"],
                "linked_payee": payee["payee"],
                "observed_monthly": payee.get("monthly_average"),
                "cadence": payee.get("cadence"),
                "source": "bank_observed",
                "origin": "statements",
            }
        )
    return out


def excludable_payees(recurring: dict[str, Any]) -> list[dict[str, Any]]:
    """Statement debits a desk may mark as not operating spend: transfers and
    owner draws large enough to matter."""
    return [
        p
        for p in recurring.get("payees") or []
        if p.get("kind") in EXCLUDABLE_KINDS and (p.get("monthly_average") or 0) >= EXCLUDE_MIN_MONTHLY
    ]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _payee_amount_in_month(payee: dict[str, Any] | None, month: str, months_read: list[str], fallback: float | None) -> float:
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
            removed += _payee_amount_in_month(payee, month["month"], months_read, obligation.get("monthly"))
        removed = round(removed, 2)
        change = removed - added
        before = cumulative
        cumulative = round(cumulative + change, 2)
        withdrawals = month.get("withdrawals")
        deposits = month.get("deposits")
        withdrawals_after = round(withdrawals - removed + added, 2) if withdrawals is not None else None
        rows.append(
            {
                "month": month["month"],
                "label": month.get("label") or month_label(month["month"]),
                "deposits": deposits,
                "withdrawals": withdrawals,
                "withdrawals_after": withdrawals_after,
                "net": month.get("net"),
                "net_after": round(deposits - withdrawals_after, 2) if deposits is not None and withdrawals_after is not None else None,
                "ending": month.get("ending_balance"),
                "ending_after": round(month["ending_balance"] + cumulative, 2) if month.get("ending_balance") is not None else None,
                "lowest": month.get("low_balance"),
                "lowest_after": round(month["low_balance"] + before + change / 2, 2) if month.get("low_balance") is not None else None,
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
) -> dict[str, Any]:
    retire = set(plan.get("retire") or [])
    exclude = set(plan.get("exclude") or [])
    payees_by_key = {p["key"]: p for p in recurring.get("payees") or []}
    retired = [o for o in obligations_list if o["id"] in retire]
    retired_in_service = [o for o in retired if o.get("in_debt_service")]
    retired_monthly = round(sum(o.get("monthly") or 0 for o in retired_in_service), 2)
    payoff_known = [o.get("payoff") for o in retired if o.get("payoff") is not None]
    payoff_total = round(sum(payoff_known), 2) if payoff_known else None
    payoff_unknown = sum(1 for o in retired if o.get("payoff") is None)
    excluded = [payees_by_key[k.split(":", 1)[1]] for k in exclude if k.startswith("payee:") and k.split(":", 1)[1] in payees_by_key]
    excluded_monthly = round(sum(p.get("monthly_average") or 0 for p in excluded), 2)

    new_payment = plan.get("new_payment_monthly")
    source = plan.get("new_payment_source")
    if new_payment is None and plan.get("new_loan_amount") and plan.get("new_loan_term_months"):
        new_payment = amortized_payment(float(plan["new_loan_amount"]), float(plan.get("new_loan_rate_pct") or 0.0), int(plan["new_loan_term_months"]))
        source = source or "manual_terms"
    if new_payment is None and proposed:
        new_payment, source = proposed.get("monthly_payment"), "term_sheet"
    service_monthly = service.get("monthly") or 0.0
    before_new = round(max(0.0, service_monthly - retired_monthly), 2)
    after_monthly = round(max(0.0, before_new + float(new_payment or 0.0)), 2)

    candidates_after = []
    for candidate in dscr.get("candidates") or []:
        numerator = candidate.get("numerator")
        if numerator is None:
            continue
        numerator_after = numerator + (excluded_monthly * 12 if candidate.get("key") == "bank_cash_flow" else 0.0)
        value = round(numerator_after / (after_monthly * 12), 2) if after_monthly > 0 else None
        candidates_after.append(
            {
                "key": candidate.get("key"),
                "label": candidate.get("label"),
                "numerator_before": numerator,
                "numerator_after": round(numerator_after, 2),
                "value_before": candidate.get("value"),
                "value_after": value,
                "status_after": dscr_status(value),
                "no_debt_left": after_monthly <= 0 and numerator_after > 0,
            }
        )
    headline_key = (dscr.get("headline") or {}).get("key")
    headline_after = next((c for c in candidates_after if c["key"] == headline_key), candidates_after[0] if candidates_after else None)
    rows = replay_months(months, retired, payees_by_key, recurring.get("months_read") or [], new_payment)
    net_before = [r["net"] for r in rows if r["net"] is not None]
    net_after = [r["net_after"] for r in rows if r["net_after"] is not None]
    active = bool(retire or exclude or plan.get("new_payment_monthly") is not None or plan.get("new_loan_amount"))
    return {
        "active": active,
        "retired_ids": sorted(retire),
        "excluded_ids": sorted(exclude),
        "retired_monthly": retired_monthly,
        "retired_count": len(retired),
        "payoff_total": payoff_total,
        "payoff_unknown": payoff_unknown,
        "excluded_monthly": excluded_monthly,
        "new_payment_monthly": _round(float(new_payment)) if new_payment is not None else None,
        "new_payment_source": source if new_payment is not None else None,
        "loan_amount_needed": _round(max(payoff_total or 0.0, requested_amount or 0.0)) or None,
        "debt_service_before_monthly": _round(service_monthly),
        "debt_service_before_new_payment_monthly": before_new,
        "debt_service_after_monthly": after_monthly,
        "debt_service_after_annual": _round(after_monthly * 12),
        "candidates": candidates_after,
        "headline_before": (dscr.get("headline") or {}).get("value"),
        "headline_after": headline_after["value_after"] if headline_after else None,
        "headline_after_status": headline_after["status_after"] if headline_after else "unavailable",
        "headline_key": headline_key,
        "no_debt_left": bool(headline_after and headline_after["no_debt_left"]),
        "replay": rows,
        "net_before_avg": _round(sum(net_before) / len(net_before)) if net_before else None,
        "net_after_avg": _round(sum(net_after) / len(net_after)) if net_after else None,
        "ending_before_last": rows[-1]["ending"] if rows else None,
        "ending_after_last": rows[-1]["ending_after"] if rows else None,
        "months_dipping_negative_before": sum(1 for r in rows if r["lowest"] is not None and r["lowest"] < 0),
        "months_dipping_negative_after": sum(1 for r in rows if r["lowest_after"] is not None and r["lowest_after"] < 0),
        "cash_kept": rows[-1]["cumulative"] if rows else 0.0,
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
) -> list[dict[str, Any]]:
    """Actions, each with the coverage it would leave, ranked best first.
    Every lever is a change to the plan the desk can apply with one click,
    or a document the file is missing."""
    headline = dscr.get("headline") or {}
    numerator = headline.get("numerator")
    if numerator is None:
        candidates = dscr.get("candidates") or []
        numerator = candidates[0].get("numerator") if candidates else None
    key = headline.get("key") or ((dscr.get("candidates") or [{}])[0].get("key"))
    base_service = evaluation["debt_service_after_monthly"]
    base_value = evaluation["headline_after"]
    rate = float((terms or {}).get("rate_pct") or DEFAULT_RATE_PCT)
    term = int((terms or {}).get("term_months") or DEFAULT_TERM_MONTHS)
    retire = set(plan.get("retire") or [])
    exclude = set(plan.get("exclude") or [])
    levers: list[dict[str, Any]] = []

    advances = [o for o in obligations_list if o.get("is_mca") and o.get("in_debt_service") and o["id"] not in retire]
    if advances and numerator is not None:
        payoff = sum(o.get("payoff") or 0 for o in advances)
        unknown = [o for o in advances if o.get("payoff") is None]
        amount = max(payoff, requested_amount or 0.0) or None
        payment = amortized_payment(amount, rate, term) if amount else None
        after = round(max(0.0, base_service - sum(o.get("monthly") or 0 for o in advances) + (payment or 0)), 2)
        value = _dscr_for(numerator, after)
        sizing = (
            f"The loan covers {payoff:,.0f} of payoffs"
            + (f" plus the {requested_amount:,.0f} requested" if requested_amount and requested_amount > payoff else "")
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
                "detail": f"Payments stop: {sum(o.get('monthly') or 0 for o in advances):,.0f} a month. " + sizing,
                "dscr_after": value,
                "status_after": dscr_status(value),
                "uncertain": bool(unknown),
                "apply": {"retire": sorted(retire | {o["id"] for o in advances}), "new_loan_amount": amount, "new_loan_rate_pct": rate, "new_loan_term_months": term},
            }
        )
        for o in sorted(advances, key=lambda x: -(x.get("monthly") or 0)):
            after_one = round(max(0.0, base_service - (o.get("monthly") or 0) + (evaluation.get("new_payment_monthly") or 0) * 0), 2)
            value_one = _dscr_for(numerator, after_one)
            levers.append(
                {
                    "key": f"payoff:{o['id']}",
                    "label": f"Pay off {o['lender']} alone",
                    "detail": f"Removes {o.get('monthly') or 0:,.0f} a month" + (f"; payoff about {o['payoff']:,.0f}." if o.get("payoff") else "; payoff balance not recorded yet."),
                    "dscr_after": value_one,
                    "status_after": dscr_status(value_one),
                    "apply": {"retire": sorted(retire | {o["id"]})},
                }
            )
    if key == "bank_cash_flow" and numerator is not None:
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
            after_long = round(max(0.0, evaluation["debt_service_before_new_payment_monthly"] + payment), 2)
            value_long = _dscr_for(numerator, after_long)
            levers.append(
                {
                    "key": f"term:{longer}",
                    "label": f"Stretch the new loan to {longer} months",
                    "detail": f"{amount:,.0f} at {rate:g}% over {longer} months is {payment:,.0f} a month instead of {new_payment:,.0f}.",
                    "dscr_after": value_long,
                    "status_after": dscr_status(value_long),
                    "apply": {"new_loan_amount": amount, "new_loan_rate_pct": rate, "new_loan_term_months": longer, "new_payment_monthly": None},
                }
            )
    if key == "bank_cash_flow":
        draws = [p for p in recurring.get("payees") or [] if p.get("kind") in {"owner_draw", "transfer"}]
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
    ranked = sorted(levers, key=lambda lever: -(lever["dscr_after"] if lever["dscr_after"] is not None else -1))
    for lever in ranked:
        lever.setdefault("uncertain", False)
        lever["improves"] = lever["dscr_after"] is not None and (base_value is None or lever["dscr_after"] > base_value)
    return ranked[:MAX_LEVERS]
