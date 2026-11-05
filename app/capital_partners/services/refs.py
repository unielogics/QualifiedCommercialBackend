"""Human-readable references: QC-RE-0001 for listings, TR-0001 for transfers.

Derived from the maximum existing number, never from a row count, so a
withdrawn or deleted row can never make two listings share a reference.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import enums as E
from ..models import CpListing, CpTransfer

_LISTING_RE = re.compile(r"^QC-([A-Z]{2})-(\d+)$")
_TRANSFER_RE = re.compile(r"^TR-(\d+)$")


async def next_listing_ref(db: AsyncSession, vertical: str) -> str:
    prefix = E.VERTICAL_REF_PREFIX.get(vertical, "XX")
    refs = (await db.execute(select(CpListing.ref).where(CpListing.ref.like(f"QC-{prefix}-%")))).scalars().all()
    highest = 0
    for ref in refs:
        m = _LISTING_RE.match(ref or "")
        if m and m.group(1) == prefix:
            highest = max(highest, int(m.group(2)))
    return f"QC-{prefix}-{highest + 1:04d}"


async def next_transfer_ref(db: AsyncSession) -> str:
    refs = (await db.execute(select(CpTransfer.ref))).scalars().all()
    highest = 0
    for ref in refs:
        m = _TRANSFER_RE.match(ref or "")
        if m:
            highest = max(highest, int(m.group(1)))
    return f"TR-{highest + 1:04d}"
