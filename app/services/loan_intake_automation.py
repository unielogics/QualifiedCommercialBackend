"""Per-loan-type doc collection + reminder + escalation pipeline.

Phase 4 stub. The scheduler imports `evaluate_doc_reminders` on the
daily 9am cron tick and we want it to no-op cleanly until the real
implementation lands.

Phase 4 will fill in:

  async def kickoff_loan(db, loan, settings) -> None:
      \"\"\"On Loan create: read settings.checklists[loan.type],
      create Document(status=REQUESTED, requested_on=today) for every
      item with auto_request=True, emit a 'document_due' calendar
      event for each, due in first_reminder_days. Idempotent —
      checks existing docs by name+loan_id.\"\"\"

  async def evaluate_doc_reminders(db=None, settings=None) -> None:
      \"\"\"For every Document with status=REQUESTED:
        age = today - requested_on
        - age >= first_reminder_days  → Activity(kind='doc.reminder.first')
                                        + chat message to borrower
                                        + bump CalendarEvent priority
        - age >= second_reminder_days → Activity('doc.reminder.second')
                                        + AITask(source=DOCUMENTS, owner=broker)
        - age >= escalate_after_days  → Activity('doc.escalated')
                                        + AITask(priority=HIGH, super_admin)\"\"\"
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def evaluate_doc_reminders() -> None:
    """No-op until Phase 4."""
    log.debug("evaluate_doc_reminders: stub (Phase 4 not yet implemented)")
