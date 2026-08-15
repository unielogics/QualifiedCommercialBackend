"""Re-ingest a dealer's failed bucket-sourced documents through the fixed path.

The bucket ingest path used to understand only bank statements, so tax returns
and archives pulled from a linked bucket were marked 'failed' even though their
cached analyses were complete. This deletes those failed rows and re-runs
_ingest_bucket_file_core, which now classifies and routes them.

Only rows with a bucket_file_id are touched — a direct upload's bytes may not
be recoverable, so those are left alone and reported.

    python -m scripts.dos_reingest_failed <dealer_id> [--apply]

Without --apply it prints what it would do and changes nothing.
"""

from __future__ import annotations

import asyncio
import sys
from uuid import UUID

from sqlalchemy import select

from app.db import SessionLocal
from app.dealer_os.models import DealerBusiness, DealerDocument
from app.dealer_os.router import _ingest_bucket_file_core
from app.dealer_os.services.engines import recompute_snapshot


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    if not args:
        print(__doc__)
        raise SystemExit(2)
    dealer_id = UUID(args[0])

    async with SessionLocal() as db:
        dealer = await db.get(DealerBusiness, dealer_id)
        if dealer is None:
            raise SystemExit(f"dealer {dealer_id} not found")
        failed = (
            (
                await db.execute(
                    select(DealerDocument)
                    .where(
                        DealerDocument.dealer_id == dealer_id,
                        DealerDocument.status == "failed",
                    )
                    .order_by(DealerDocument.filename)
                )
            )
            .scalars()
            .all()
        )
        targets = [(d.id, d.bucket_file_id, d.filename, d.error) for d in failed]

    from_bucket = [t for t in targets if t[1] is not None]
    orphans = [t for t in targets if t[1] is None]

    print(f"{dealer.name}: {len(targets)} failed document(s)")
    for _, _, name, err in targets:
        print(f"  - {name[:60]:62s} {(err or '')[:50]}")
    if orphans:
        print(f"\n{len(orphans)} not bucket-sourced — skipped (re-upload needed):")
        for _, _, name, _ in orphans:
            print(f"  ! {name}")
    if not apply:
        print(f"\nDry run. {len(from_bucket)} would be re-ingested. Pass --apply to run.")
        return

    ok = failures = 0
    for doc_id, bucket_file_id, name, _ in from_bucket:
        async with SessionLocal() as db:
            try:
                dealer = await db.get(DealerBusiness, dealer_id)
                doc = await db.get(DealerDocument, doc_id)
                if doc is not None:
                    # Drop the failed row first: _ingest_bucket_file_core is
                    # idempotent on bucket_file_id and would return this row
                    # as-is rather than reprocessing it.
                    await db.delete(doc)
                    await db.flush()
                fresh = await _ingest_bucket_file_core(db, dealer, bucket_file_id)
                await db.commit()
                print(f"  ✓ {name[:56]:58s} {fresh.status:10s} {fresh.detected_kind or ''}")
                ok += 1
            except Exception as exc:
                await db.rollback()
                print(f"  ✕ {name[:56]:58s} {type(exc).__name__}: {str(exc)[:60]}")
                failures += 1

    async with SessionLocal() as db:
        snap = await recompute_snapshot(db, dealer_id)
        await db.commit()
    print(f"\n{ok} re-ingested, {failures} failed — score {snap.score} tier {snap.tier}")


if __name__ == "__main__":
    asyncio.run(main())
