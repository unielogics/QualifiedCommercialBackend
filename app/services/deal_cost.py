"""What the deal costs the client once QC's fees are in: for the desk only.

The package and the offering print the structure (amount, rate, term, the
schedule, the principal at risk) and never a fee. Internally the desk needs
the other half: the origination points and the consulting fee come off the
proceeds, so the client nets less than the loan, and the true cost of the
money (the rate that equates what the client actually receives with what the
client pays back) is higher than the stated APR. Nothing here is copied into
a summary, a package or a listing; those build their own dicts key by key.
"""

from __future__ import annotations

from typing import Any

from app.services.deal_economics import calculate_deal_earnings, optional_float


def _r2(value: float) -> float:
    return round(value + 0.0, 2)


def effective_rate_pct(
    proceeds: float,
    payments: list[float],
    *,
    balloon: float = 0.0,
    periods_per_year: float = 12.0,
) -> float | None:
    """The nominal annual rate (periodic IRR × periods a year, the same
    convention as the stated APR) at which the payments repay the proceeds.
    None when it cannot be found (no proceeds, no payments)."""
    if proceeds <= 0 or not payments:
        return None
    flows = list(payments)
    if balloon:
        flows[-1] = flows[-1] + balloon

    def present(rate: float) -> float:
        return sum(p / (1.0 + rate) ** (i + 1) for i, p in enumerate(flows)) - proceeds

    if sum(flows) <= proceeds:
        return 0.0
    lo, hi = 0.0, 1.0
    if present(hi) > 0:  # more than 100% a period: out of any sensible range
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if present(mid) > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-10:
            break
    return round((lo + hi) / 2.0 * periods_per_year * 100.0, 2)


def internal_economics(
    inputs: dict[str, Any], proposed: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The fees, what the client nets, and the true cost of the money.

    `inputs`: accepted_amount, origination_points, consulting_fee as the desk
    recorded them (Deal economics on the Underwriting tab). `proposed`: the
    proposed terms with their schedule (financial_cockpit.proposed_terms_block).
    None when there is neither a loan nor a fee to speak of.
    """
    earnings = calculate_deal_earnings(
        accepted_amount=inputs.get("accepted_amount"),
        origination_points=inputs.get("origination_points"),
        consulting_fee=inputs.get("consulting_fee"),
    )
    accepted = optional_float(earnings.accepted_amount)
    points = optional_float(earnings.origination_points)
    consulting = optional_float(earnings.consulting_fee)
    loan_amount = float((proposed or {}).get("amount") or 0.0) or accepted
    if not loan_amount and consulting is None and points is None:
        return None
    # Origination points are forecast on the accepted amount; before one is
    # recorded they read on the proposed loan so the desk sees the shape.
    origination = optional_float(earnings.origination_earnings)
    origination_basis = "accepted_amount"
    if origination is None and points is not None and loan_amount:
        origination = _r2(loan_amount * points / 100.0)
        origination_basis = "proposed_amount"
    fees = [f for f in (origination, consulting) if f is not None]
    total_fees = _r2(sum(fees)) if fees else 0.0
    schedule = (proposed or {}).get("schedule") or {}
    rows = (proposed or {}).get("schedule_rows") or []
    out: dict[str, Any] = {
        "audience": "desk",
        "loan_amount": _r2(loan_amount) if loan_amount else None,
        "accepted_amount": accepted,
        "origination_points": points,
        "origination_fee": origination,
        "origination_basis": origination_basis if origination is not None else None,
        "consulting_fee": consulting,
        "total_fees": total_fees,
        "qc_earnings": optional_float(earnings.total),
        "net_to_client": _r2(loan_amount - total_fees) if loan_amount else None,
        "fees_pct_of_loan": _r2(total_fees / loan_amount * 100.0) if loan_amount else None,
        "total_interest": schedule.get("total_interest"),
        "client_total_cost": _r2(total_fees + float(schedule.get("total_interest") or 0.0))
        if schedule
        else total_fees,
        "stated_rate_pct": (proposed or {}).get("rate_pct"),
        "structure_rate_pct": None,
        "effective_rate_pct": None,
        "words": "",
    }
    if rows and loan_amount:
        from app.services.loan_schedule import schedule_cash_flows

        payments = schedule_cash_flows({**schedule, "rows": rows})
        ppy = float(schedule.get("periods_per_year") or 12.0)
        out["structure_rate_pct"] = effective_rate_pct(
            loan_amount, payments, periods_per_year=ppy
        )
        out["effective_rate_pct"] = effective_rate_pct(
            loan_amount - total_fees, payments, periods_per_year=ppy
        )
    words: list[str] = []
    if total_fees and loan_amount:
        pieces = []
        if origination:
            pieces.append(f"{origination:,.0f} origination ({points:g} points)")
        if consulting:
            pieces.append(f"{consulting:,.0f} consulting")
        words.append(
            f"Fees {total_fees:,.0f} ({', '.join(pieces)}), {out['fees_pct_of_loan']:g}% of the loan: "
            f"the client nets {out['net_to_client']:,.0f}."
        )
    elif loan_amount:
        words.append(
            "No origination points or consulting fee recorded yet: the client nets the full loan."
        )
    if out["effective_rate_pct"] is not None:
        stated = out["stated_rate_pct"]
        structure_rate = out["structure_rate_pct"]
        if (
            structure_rate is not None
            and stated is not None
            and abs(structure_rate - float(stated)) > 0.5
        ):
            words.append(
                f"The structure itself costs {structure_rate:g}% against the stated {float(stated):g}% "
                f"(interest on the original principal); with the fees in, the money costs "
                f"{out['effective_rate_pct']:g}%."
            )
        else:
            words.append(
                f"With the fees in, the money costs {out['effective_rate_pct']:g}%"
                + (f" against the stated {float(stated):g}%." if stated is not None else ".")
            )
    out["words"] = " ".join(words)
    return out
