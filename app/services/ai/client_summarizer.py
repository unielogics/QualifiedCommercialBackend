"""Per-client (account-wide) AI summarizer (Phase 8 stub).

The scheduler imports `refresh_all_active_clients` on the daily 3am
cron tick. Phase 8 will replace this stub with the real implementation:

  async def refresh_client_summary(db, client_id) -> ClientLivingProfile:
      \"\"\"Aggregates across all the client's loans + credit pulls +
      30 days of activity. Calls Haiku with a system prompt that
      sees the full borrower context and outputs:
        outstanding_documents [{loan_id, name, days_overdue}]
        blocking_credit_issues [str]
        next_actions [{title, owner, priority, cta, due_at}]
        rate_pressure_notes [str]
        suggested_next_loan str
      Persists onto Client.living_profile JSONB.\"\"\"

  async def refresh_all_active_clients(*, limit) -> int:
      \"\"\"Iterates clients with at least one non-funded loan or one
      open prequal request. Caps at limit per tick.\"\"\"
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def refresh_all_active_clients(*, limit: int = 50) -> int:
    """No-op until Phase 8. Returns count of clients refreshed."""
    log.debug("refresh_all_active_clients: stub (Phase 8 not yet implemented)")
    return 0
