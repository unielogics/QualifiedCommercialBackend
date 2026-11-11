"""Bounded automatic refresh for listings already opted into managed PPMs."""
from __future__ import annotations

import asyncio
import logging
import time
from uuid import UUID

log = logging.getLogger(__name__)
_lock = asyncio.Lock()
_cursor: UUID | None = None
_retry_after: dict[UUID, float] = {}


async def drain_pending(*, limit: int = 5, session_factory=None) -> dict[str, int]:
    """Recheck saved sources without relying on a staff browser remaining open.

    The PPM service owns opt-in, semantic fingerprinting and write concurrency.
    Sessions are separate for each listing so one failed draft cannot roll back
    another. The cursor advances even on failure to keep the sweep fair.
    """
    global _cursor
    from . import ppm

    if _lock.locked():
        return {"checked": 0, "refreshed": 0, "failed": 0}
    if session_factory is None:
        from app.db import SessionLocal

        session_factory = SessionLocal
    limit = min(10, max(1, int(limit)))
    async with _lock:
        async with session_factory() as db:
            ids = await ppm.adopted_listing_ids(db, limit=limit, after_id=_cursor)
            if not ids and _cursor is not None:
                _cursor = None
                ids = await ppm.adopted_listing_ids(db, limit=limit, after_id=None)
        counts = {"checked": 0, "refreshed": 0, "failed": 0}
        now = time.monotonic()
        for key in list(_retry_after):
            if _retry_after[key] <= now:
                _retry_after.pop(key, None)
        for listing_id in ids:
            _cursor = listing_id
            if _retry_after.get(listing_id, 0) > time.monotonic():
                continue
            counts["checked"] += 1
            async with session_factory() as db:
                try:
                    result = await asyncio.wait_for(ppm.refresh_managed_listing(db, listing_id, actor_id=None), timeout=75)
                    await db.commit()
                    if result and result.get("status") == "refreshed":
                        counts["refreshed"] += 1
                except Exception as exc:
                    await db.rollback()
                    _retry_after[listing_id] = time.monotonic() + 300
                    counts["failed"] += 1
                    log.warning("Managed PPM refresh failed listing=%s error=%s", listing_id, type(exc).__name__)
        return counts
