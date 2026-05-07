"""System-level pipeline digest (Phase 10 stub).

The scheduler imports `scan_pipeline` on the daily 8am cron tick.
Phase 10 will replace this stub with the real implementation:

  async def scan_pipeline() -> dict:
      \"\"\"Aggregate anomalies across all loans:
        - Loans with no Activity in 7+ days
        - Credit pulls expiring in next 14 days
        - Rate-pressure clusters (FRED 10-yr ±25 bps in a week)
        - Approved prequals with expected_closing_date < 14d away

      Emit super_admin AITask(source=CALENDAR, priority=HIGH) and
      loan_id=NULL CalendarEvent(external_ref_kind='stalled_loan',
      external_ref_id=loan.id) for each. Caps at 5 stalled-loan
      events per super_admin per day to avoid spam.\"\"\"
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def scan_pipeline() -> dict:
    """No-op until Phase 10."""
    log.debug("scan_pipeline: stub (Phase 10 not yet implemented)")
    return {"stalled_loans": 0, "expiring_credit": 0, "alerts_emitted": 0}
