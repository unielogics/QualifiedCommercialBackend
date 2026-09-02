"""Reconcile existing Plaid Item product state through /item/get.

This is a release and recovery utility. It records Plaid's current `products`,
`consented_products`, and `billed_products` for active Field Desk and standalone
Funding connections. It does not create Asset Reports, refresh Statements, or
download evidence, so running it does not initiate product collection.

    python -m scripts.reconcile_plaid_products
    python -m scripts.reconcile_plaid_products --limit 25
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from app.db import SessionLocal
from app.dealer_os.models import DealerPlaidItem
from app.dealer_os.services import plaid_client
from app.models.application_profile import ApplicationPlaidItem
from app.services import plaid_policy


@dataclass
class Totals:
    reconciled: int = 0
    failed: int = 0
    skipped: int = 0


async def _ids(model, limit: int | None) -> list[UUID]:
    async with SessionLocal() as db:
        statement = (
            select(model.id)
            .where(
                model.status.notin_(("removed", "revoked")),
                model.environment == plaid_client.environment(),
                model.encrypted_access_token.is_not(None),
            )
            .order_by(model.created_at.asc())
        )
        if limit is not None:
            statement = statement.limit(limit)
        return list((await db.execute(statement)).scalars().all())


async def _one(model, item_id: UUID, totals: Totals) -> None:
    async with SessionLocal() as db:
        item = await db.get(model, item_id)
        if item is None or item.status in {"removed", "revoked"}:
            totals.skipped += 1
            return
        try:
            await plaid_policy.reconcile_item(db, item)
            await db.commit()
        except Exception as exc:  # keep one provider failure from ending the release audit
            await db.rollback()
            totals.failed += 1
            print(f"FAILED {model.__name__} {item_id}: {type(exc).__name__}: {exc}")
            return
        totals.reconciled += 1
        print(
            f"OK {model.__name__} {item_id}: "
            f"products={plaid_policy.item_products(item)} "
            f"consented={list(item.plaid_consented_products or [])} "
            f"billed={list(item.plaid_billed_products or [])}"
        )


async def run(limit: int | None) -> Totals:
    totals = Totals()
    remaining = limit
    for model in (DealerPlaidItem, ApplicationPlaidItem):
        ids = await _ids(model, remaining)
        for item_id in ids:
            await _one(model, item_id, totals)
        if remaining is not None:
            remaining = max(0, remaining - len(ids))
            if remaining == 0:
                break
    return totals


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    return args


async def main() -> None:
    args = _args()
    totals = await run(args.limit)
    print(
        f"Plaid product reconciliation complete: reconciled={totals.reconciled} "
        f"failed={totals.failed} skipped={totals.skipped}"
    )
    if totals.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
