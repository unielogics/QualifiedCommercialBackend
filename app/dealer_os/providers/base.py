"""FeedProvider interface — the keys-deferred contract (v5).

Engines only ever read normalized dos_financial_periods / dos_cash_events;
providers write them. uploads.py is the launch path (no external keys),
fixtures.py backs demos/tests, and plaid.py / quickbooks.py implement this
same interface when DEALER_OS_PLAID_* / DEALER_OS_QBO_* keys are provided —
no engine or UI change required at that point.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession


class FeedProvider(ABC):
    kind: str = "base"

    @abstractmethod
    async def sync(self, db: AsyncSession, dealer_id: UUID) -> dict:
        """Pull whatever this source offers and upsert normalized periods/events.
        Returns a summary dict {periods: n, events: n} for logging/UI freshness."""
        raise NotImplementedError
