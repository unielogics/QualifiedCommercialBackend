"""Bank-ledger associations, kept apart from the accepted debt schedule.

Linking a payee explains which observed transactions support a loan. It does
not add a loan, change its payment, or create a financial document version.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from app.dealer_os.models import DealerBusiness, DealerDebt
from app.services.financial_version_selection import accepted_debt_schedule_snapshot


def _belongs_to_profile(row, profile) -> bool:
    return row.profile_id == profile.id or (
        row.profile_id is None and profile.dealer_id is not None and row.dealer_id == profile.dealer_id
    )


async def _payees(db, profile):
    if isinstance(getattr(db, "info", None), dict):
        from app.services import application_programs, financial_cockpit

        files, latest, _classes = await application_programs._evidence_inventory(db, profile)
        accepted = await application_programs.accepted_evidence_file_ids(db, profile, files, latest)
        analyses = financial_cockpit._analysis_rows(files, latest, accepted)
        observed = financial_cockpit.recurring_payments(analyses).get("payees") or []
        if observed:
            return [SimpleNamespace(key=row["key"], sample_description=row["payee"], direction=-1,
                                    category=row["kind"], monthly_average=row.get("monthly_average") or 0,
                                    months=row.get("months_seen") or 0, count=row.get("occurrences") or 0)
                    for row in observed]
    if not profile.dealer_id:
        return []
    dealer = await db.get(DealerBusiness, profile.dealer_id)
    if dealer is None:
        return []
    # Use the same event loading, category overrides and vendor normalization
    # as the banking report; do not introduce another payee classifier.
    from app.dealer_os.router import _vendor_rollup

    rolled, _count = await _vendor_rollup(db, dealer)
    return [row for row in rolled if row.direction < 0]


async def inventory(db, profile) -> dict:
    accepted = await accepted_debt_schedule_snapshot(db, profile.id)
    scope = DealerDebt.profile_id == profile.id
    if profile.dealer_id:
        scope = or_(scope, and_(DealerDebt.profile_id.is_(None), DealerDebt.dealer_id == profile.dealer_id))
    query = select(DealerDebt).where(scope)
    if accepted is None:
        query = query.where(DealerDebt.status == "active", DealerDebt.duplicate_of_id.is_(None))
    debts = list((await db.execute(query.order_by(DealerDebt.created_at, DealerDebt.id))).scalars().all())
    if accepted is not None:
        from app.dealer_os.services.financial_debt_drafts import accepted_debt_views
        from app.services.financial_version_selection import active_debt_schedule_rows

        debts = accepted_debt_views(debts, active_debt_schedule_rows(accepted.body))
    payees = await _payees(db, profile)
    return {
        "available": bool(getattr(profile, "primary_bucket_id", None) or profile.dealer_id or payees),
        "schedule_complete": accepted is not None,
        "obligations": [{"id": str(row.id), "lender": row.lender, "vendor_key": row.vendor_key,
                         "monthly_payment": str(row.monthly_payment) if row.monthly_payment is not None else None}
                        for row in debts],
        "payees": [{"vendor_key": row.key, "label": row.sample_description, "category": row.category,
                    "monthly_average": abs(row.monthly_average), "months": row.months, "count": row.count}
                   for row in payees],
    }


async def link_payee(db, profile, row_id: UUID, vendor_key: str | None, *, actor=None) -> dict:
    row = (await db.execute(select(DealerDebt).where(DealerDebt.id == row_id).with_for_update())).scalar_one_or_none()
    if row is None or not _belongs_to_profile(row, profile):
        raise HTTPException(404, "Obligation not found on this file")
    accepted = await accepted_debt_schedule_snapshot(db, profile.id)
    if accepted is not None and str(row.id) not in {
        str(item.get("id")) for item in (accepted.body or {}).get("debts", [])
        if item.get("status", "active") == "active" and not item.get("duplicate_of_id")
    }:
        raise HTTPException(409, "This obligation is outside the completed debt schedule")
    if accepted is None and (row.status != "active" or row.duplicate_of_id):
        raise HTTPException(409, "Restore the obligation before linking its bank payee")
    if vendor_key is not None:
        if vendor_key not in {payee.key for payee in await _payees(db, profile)}:
            raise HTTPException(422, "Choose an outgoing payee observed in this file's bank statements")
    row.vendor_key = vendor_key
    row.evidence = {**(row.evidence if isinstance(row.evidence, dict) else {}), "payee_link": {
        "vendor_key": vendor_key, "linked_at": datetime.now(UTC).isoformat(),
        "linked_by_user_id": str(actor.id) if getattr(actor, "id", None) else None,
    }}
    await db.flush()
    return await inventory(db, profile)
