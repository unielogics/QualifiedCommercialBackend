"""Activity-log helper + summary-dirty drain (Phase 6).

For now this is a stub — the scheduler imports `drain_summary_dirty`
on every 5-min tick and we want it to no-op cleanly until Phase 6
fills in the real continuous-note-awareness logic.

Phase 6 will replace `drain_summary_dirty` with code that:
  - Selects up to N Loan rows where summary_dirty=True
  - For each, calls app.services.ai.summarizer.refresh_summary
  - Clears summary_dirty + sets summary_refreshed_at
  - Caps at `limit` per tick to bound LLM spend

Phase 6 will also add `log_activity(...)` — a thin wrapper around
`db.add(Activity(...))` that ALSO flips `loan.summary_dirty=True`
when a loan_id is supplied. Existing routers will gradually migrate
to that helper.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def drain_summary_dirty(*, limit: int = 20) -> int:
    """No-op until Phase 6. Returns the number of loans refreshed."""
    log.debug("drain_summary_dirty: stub (Phase 6 not yet implemented)")
    return 0
