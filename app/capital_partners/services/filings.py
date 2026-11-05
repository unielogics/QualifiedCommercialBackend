"""The Regulation D clock, as rows in cp_filings.

* Form D is due 15 calendar days after the first sale of an offering, where a
  sale is the first subscription that becomes binding (``signed``), the SEC's
  "irrevocably committed" test (Rule 503(a)). Funds arriving later change
  nothing.
* A state notice is due in each state where a member resides, generally 15
  days after the first sale to a resident of that state. The row is created
  by that first binding subscription; the desk confirms the deadline and fee
  against the state's current rule before filing (they vary).
* An annual amendment is due on the anniversary of the Form D filing while the
  offering stays open. It is created when Form D is marked filed.
* Bad-actor checks are desk-entered rows per covered person, program-level.

Everything here is idempotent: calling ``on_binding_subscription`` twice for
the same listing and state creates nothing new.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import enums as E
from ..models import CpFiling, CpListing, CpMember, CpSubscription


def form_d_due(first_sale_at: datetime) -> date:
    return (first_sale_at + timedelta(days=E.FORM_D_DAYS)).date()


def state_notice_due(first_sale_at: datetime) -> date:
    return (first_sale_at + timedelta(days=E.STATE_NOTICE_DAYS)).date()


def amendment_due(filed_at: date) -> date:
    try:
        return filed_at.replace(year=filed_at.year + 1)
    except ValueError:  # 29 February
        return filed_at.replace(year=filed_at.year + 1, day=28)


async def _existing(db: AsyncSession, *, kind: str, listing_id: uuid.UUID | None, state: str | None) -> CpFiling | None:
    stmt = select(CpFiling).where(CpFiling.kind == kind)
    stmt = stmt.where(CpFiling.listing_id == listing_id) if listing_id else stmt.where(CpFiling.listing_id.is_(None))
    stmt = stmt.where(CpFiling.state == state) if state else stmt.where(CpFiling.state.is_(None))
    return (await db.execute(stmt.limit(1))).scalar_one_or_none()


async def on_binding_subscription(
    db: AsyncSession,
    *,
    listing: CpListing,
    member: CpMember,
    signed_at: datetime,
    actor_user_id: uuid.UUID | None,
) -> list[CpFiling]:
    """Stamp the listing's first sale and create the filing rows it triggers."""
    created: list[CpFiling] = []
    if listing.first_sale_at is None or signed_at < listing.first_sale_at:
        listing.first_sale_at = signed_at
    first_sale = listing.first_sale_at or signed_at

    if await _existing(db, kind=E.FilingKind.FORM_D, listing_id=listing.id, state=None) is None:
        row = CpFiling(
            kind=E.FilingKind.FORM_D,
            listing_id=listing.id,
            first_sale_at=first_sale,
            due_at=form_d_due(first_sale),
            status=E.FilingStatus.DUE,
            created_by_id=actor_user_id,
        )
        db.add(row)
        created.append(row)

    state = (member.state_of_residence or "").upper()[:2]
    if state and await _existing(db, kind=E.FilingKind.STATE_NOTICE, listing_id=listing.id, state=state) is None:
        row = CpFiling(
            kind=E.FilingKind.STATE_NOTICE,
            listing_id=listing.id,
            state=state,
            first_sale_at=signed_at,
            due_at=state_notice_due(signed_at),
            status=E.FilingStatus.DUE,
            created_by_id=actor_user_id,
            notes="Deadline and fee vary by state; confirm before filing.",
        )
        db.add(row)
        created.append(row)
    await db.flush()
    return created


async def on_form_d_filed(db: AsyncSession, filing: CpFiling, *, filed_at: date, actor_user_id: uuid.UUID | None) -> CpFiling | None:
    """Marking Form D filed schedules the annual amendment, once."""
    if filing.kind != E.FilingKind.FORM_D:
        return None
    if await _existing(db, kind=E.FilingKind.FORM_D_AMENDMENT, listing_id=filing.listing_id, state=None) is not None:
        return None
    row = CpFiling(
        kind=E.FilingKind.FORM_D_AMENDMENT,
        listing_id=filing.listing_id,
        first_sale_at=filing.first_sale_at,
        due_at=amendment_due(filed_at),
        status=E.FilingStatus.DUE,
        created_by_id=actor_user_id,
        notes="Due on the anniversary of the original filing while the offering remains open.",
    )
    db.add(row)
    await db.flush()
    return row


async def investor_counts(db: AsyncSession) -> dict[uuid.UUID, int]:
    """Distinct members with a binding or funded subscription, per listing
    (what Form D asks for; non-accredited is always zero here)."""
    rows = (
        await db.execute(
            select(CpSubscription.listing_id, func.count(func.distinct(CpSubscription.member_id)))
            .where(CpSubscription.status.in_(sorted(E.SOLD_SUBSCRIPTION_STATUSES)))
            .group_by(CpSubscription.listing_id)
        )
    ).all()
    return {listing_id: int(n) for listing_id, n in rows}


def summary(filings: list[CpFiling], *, today: date | None = None) -> dict[str, Any]:
    """The four numbers at the top of the compliance screen."""
    today = today or datetime.now(UTC).date()
    due = [f for f in filings if f.status == E.FilingStatus.DUE and f.due_at]
    form_d_due_rows = sorted((f for f in due if f.kind == E.FilingKind.FORM_D), key=lambda f: f.due_at)
    state_due_rows = sorted((f for f in due if f.kind == E.FilingKind.STATE_NOTICE), key=lambda f: f.due_at)
    amendments = sorted((f for f in due if f.kind == E.FilingKind.FORM_D_AMENDMENT), key=lambda f: f.due_at)
    checks = [f for f in filings if f.kind == E.FilingKind.BAD_ACTOR_CHECK]
    cleared = [f for f in checks if f.status == E.FilingStatus.CLEARED]
    overdue = [f for f in due if f.due_at < today]
    return {
        "form_d_due": len(form_d_due_rows),
        "form_d_next_due": form_d_due_rows[0].due_at if form_d_due_rows else None,
        "state_notices_due": len(state_due_rows),
        "state_notice_next_due": state_due_rows[0].due_at if state_due_rows else None,
        "covered_persons": len(checks),
        "covered_persons_cleared": len(cleared),
        "amendment_next_due": amendments[0].due_at if amendments else None,
        "overdue": len(overdue),
    }
