"""Rebuild a Dealer OS dealer's derived state from its event ledger.

Run after any structural change to accounts or events — notably the 0115
account de-duplication, which repoints cash events onto the surviving account
and drops the loser's duplicate period rows. Deposits and withdrawals are
derived data ("the event ledger is truth"), so they must be recomputed through
the real production functions rather than reimplemented in SQL, where the two
would silently drift.

    python -m scripts.dos_recompute_dealer <dealer_id> [<dealer_id> ...]
    python -m scripts.dos_recompute_dealer --all
"""

from __future__ import annotations

import asyncio
import sys
from uuid import UUID

from sqlalchemy import select

from app.db import SessionLocal
from app.dealer_os.models import DealerBusiness, DealerCashEvent
from app.dealer_os.services.engines import recompute_snapshot
from app.dealer_os.services.normalize import rebuild_periods


async def recompute(dealer_id: UUID) -> None:
    async with SessionLocal() as db:
        dealer = await db.get(DealerBusiness, dealer_id)
        if dealer is None:
            print(f"  {dealer_id}: not found")
            return

        # Rebuild is scoped to one (dealer, account) pair per call, so group
        # the ledger's months by the account that owns them — including the
        # legacy null-account scope.
        rows = (
            await db.execute(
                select(DealerCashEvent.account_id, DealerCashEvent.period)
                .where(DealerCashEvent.dealer_id == dealer_id)
                .distinct()
            )
        ).all()
        by_account: dict[UUID | None, set] = {}
        for account_id, period in rows:
            by_account.setdefault(account_id, set()).add(period)

        touched = 0
        for account_id, periods in by_account.items():
            touched += await rebuild_periods(db, dealer_id, periods, account_id=account_id)

        snapshot = await recompute_snapshot(db, dealer_id)
        await db.commit()
        print(
            f"  {dealer.name}: rebuilt {touched} period(s) across "
            f"{len(by_account)} account scope(s) — score "
            f"{snapshot.score} tier {snapshot.tier}"
        )


async def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        raise SystemExit(2)

    if args[0] == "--all":
        async with SessionLocal() as db:
            ids = (await db.execute(select(DealerBusiness.id))).scalars().all()
    else:
        ids = [UUID(a) for a in args]

    print(f"Recomputing {len(ids)} dealer(s)")
    for dealer_id in ids:
        await recompute(dealer_id)


if __name__ == "__main__":
    asyncio.run(main())
