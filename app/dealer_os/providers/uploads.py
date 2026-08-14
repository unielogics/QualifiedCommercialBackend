"""Uploads provider — launch data path (no external keys).

Stream 2 implements the full CSV/statement/P&L parsing here, reusing the
existing extraction primitives (extract_bank_months / extract_debt_schedule /
extract_tax_years in app/services/public_underwriting_packet_pdf.py) imported
read-only per the isolation contract. Stream 1 ships the shell so the provider
registry and source-connection rows are real from day one.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from .base import FeedProvider


class UploadsProvider(FeedProvider):
    kind = "uploads"

    async def sync(self, db: AsyncSession, dealer_id: UUID) -> dict:
        """No-op reconcile hook: uploads are pushed via the cash-events import
        endpoint rather than pulled. Reconciliation itself is
        app.dealer_os.services.normalize.rebuild_periods — every push path
        calls it after mutating events, so a pull-style sync has nothing to do
        for this provider.
        """
        return {"periods": 0, "events": 0}
