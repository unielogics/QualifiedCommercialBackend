"""Attention rollups for the dealer list — Phase 3 Wave 2.

Pure helpers only (no IO, unit-testable). The router batches the raw counts
(open alerts, last-month period presence, overdue plan actions, unresolved
fundability_* alerts) across all listed dealers in grouped queries and feeds
them through attention_score for a deterministic sort key.
"""

from __future__ import annotations

from datetime import date

# Weights are a triage heuristic, not policy: a missing monthly statement and
# overdue plan work outrank generic alerts; fundable paths are opportunity
# attention (worth a look, not a fire).
WEIGHT_OPEN_ALERT = 2
WEIGHT_MISSING_STATEMENT = 3
WEIGHT_OVERDUE_ACTION = 2
WEIGHT_FUNDABLE_PATH = 1


def last_calendar_month(today: date) -> date:
    """First day of the month before ``today``'s month."""
    year, month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return date(year, month, 1)


def attention_score(
    open_alerts: int,
    missing_statement: bool,
    overdue_actions: int,
    fundable_paths: int,
) -> int:
    """Deterministic weighted sum used to sort the team book by 'needs eyes'."""
    return (
        WEIGHT_OPEN_ALERT * max(0, int(open_alerts))
        + (WEIGHT_MISSING_STATEMENT if missing_statement else 0)
        + WEIGHT_OVERDUE_ACTION * max(0, int(overdue_actions))
        + WEIGHT_FUNDABLE_PATH * max(0, int(fundable_paths))
    )
