"""Advances the desk records from what the client says out loud.

The client often gives the terms of each merchant cash advance verbally before
any contract arrives: who funded it, what was approved, what is left, how
often it debits and how much. The desk records those here as rows on the
file's debt schedule (dos_debts, category "mca", origin "admin"), so the
financial cockpit can put a debt service and a DSCR against them straight
away and reconcile them with what the AI reads off the statements and the
contracts later.

Same ownership law as the debt schedule form (financial_statements.save_debt_rows):
a save matches rows by id, then by lender among the rows it owns, updates its
own in place, inserts what is new, deletes only its own rows that the payload
dropped, and never touches a row another origin wrote (the client's typed
terms, the AI's draft).
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_os.models import DealerDebt
from app.models.application_profile import ApplicationProfile
from app.services.financial_cockpit import MCA_CATEGORIES, monthly_equivalent

DESK_ORIGIN = "admin"
FREQUENCIES = ("daily", "weekly", "biweekly", "monthly")
_SOURCE_BY_ORIGIN = {
    "admin": "desk",
    "borrower": "client",
    "client": "client",
    "ai_draft": "ai",
    "extraction": "ai",
}


def is_advance_row(row: Any) -> bool:
    category = str(getattr(row, "category", "") or "").strip().lower()
    frequency = str(getattr(row, "payment_frequency", "") or "").strip().lower()
    return (
        category in MCA_CATEGORIES
        or frequency in {"daily", "weekly", "biweekly"}
        or getattr(row, "factor_rate", None) is not None
    )


def _lender_key(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").casefold())


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def advance_read(row: DealerDebt) -> dict[str, Any]:
    origin = str(row.origin or "")
    return {
        "id": row.id,
        "lender": row.lender,
        "original_amount": _num(row.original_amount),
        "balance": _num(row.balance),
        "payment_amount": _num(row.payment_amount),
        "payment_frequency": (row.payment_frequency or None),
        "monthly_payment": _num(row.monthly_payment),
        "factor_rate": _num(row.factor_rate),
        "payoff_amount": _num(row.payoff_amount),
        "maturity_on": row.maturity_on,
        "notes": row.notes,
        "count_in_dscr": bool(row.count_in_dscr),
        "origin": origin,
        "editable": origin == DESK_ORIGIN,
        "source": _SOURCE_BY_ORIGIN.get(origin, origin or "unknown"),
    }


async def advance_rows(db: AsyncSession, profile: ApplicationProfile) -> list[DealerDebt]:
    rows = list(
        (
            await db.execute(
                select(DealerDebt)
                .where(DealerDebt.profile_id == profile.id, DealerDebt.status == "active")
                .order_by(DealerDebt.lender)
            )
        )
        .scalars()
        .all()
    )
    return [row for row in rows if is_advance_row(row)]


def _apply(target: DealerDebt, row: dict[str, Any]) -> None:
    target.lender = str(row.get("lender") or "").strip() or "Unnamed funder"
    target.category = "mca"
    target.original_amount = _num(row.get("original_amount"))
    target.balance = _num(row.get("balance"))
    target.payment_amount = _num(row.get("payment_amount"))
    frequency = str(row.get("payment_frequency") or "").strip().lower() or None
    target.payment_frequency = frequency if frequency in FREQUENCIES else None
    target.factor_rate = _num(row.get("factor_rate"))
    target.payoff_amount = _num(row.get("payoff_amount"))
    maturity = row.get("maturity_on")
    target.maturity_on = (
        maturity
        if isinstance(maturity, date) or maturity is None
        else date.fromisoformat(str(maturity)[:10])
    )
    target.notes = str(row.get("notes") or "").strip() or None
    target.count_in_dscr = bool(row.get("count_in_dscr", True))
    monthly = (
        monthly_equivalent(target.payment_amount, target.payment_frequency or "monthly")
        if target.payment_amount is not None
        else None
    )
    target.monthly_payment = monthly if monthly is not None else _num(row.get("monthly_payment"))


async def save_advances(
    db: AsyncSession,
    profile: ApplicationProfile,
    rows: list[dict[str, Any]] | None,
    *,
    origin: str = DESK_ORIGIN,
) -> list[DealerDebt]:
    """Put the desk's advance rows on the file, touching only what `origin` owns."""
    if rows is None:
        return await advance_rows(db, profile)
    existing = await advance_rows(db, profile)
    by_id = {str(row.id): row for row in existing}
    claimed: set[str] = set()
    pairs: list[tuple[dict[str, Any], DealerDebt | None]] = []
    for row in rows:
        target = by_id.get(str(row.get("id") or ""))
        if target is not None and str(target.id) not in claimed:
            claimed.add(str(target.id))
            pairs.append((row, target))
        else:
            pairs.append((row, None))
    for index, (row, target) in enumerate(pairs):
        if target is not None:
            continue
        wanted = _lender_key(row.get("lender"))
        owned = [
            c
            for c in existing
            if str(c.id) not in claimed and c.origin == origin and _lender_key(c.lender) == wanted
        ]
        if owned:
            claimed.add(str(owned[0].id))
            pairs[index] = (row, owned[0])
    for row, target in pairs:
        if target is None:
            fresh = DealerDebt(
                id=uuid.uuid4(),
                profile_id=profile.id,
                dealer_id=profile.dealer_id,
                origin=origin,
                status="active",
            )
            _apply(fresh, row)
            db.add(fresh)
        elif target.origin == origin:
            _apply(target, row)
        # else: another origin's row (the client's typed terms, the AI's draft): seen, matched, left alone.
    for row in existing:
        if str(row.id) not in claimed and row.origin == origin:
            await db.delete(row)
    await db.flush()
    return await advance_rows(db, profile)
