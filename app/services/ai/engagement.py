"""AI engagement state — pause/resume helpers for the Deal Workspace chat.

When a super-admin sends a manual message in the client-facing thread, we
park the AI for an hour so it doesn't immediately step on the operator.
The chat handler in routers/loan_workspace.py calls `pause()` after each
super-admin Chat-mode send; `is_paused()` is checked before any auto-reply.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models.loan import Loan

DEFAULT_PAUSE_HOURS = 1


def is_paused(loan: Loan) -> bool:
    """True if loan.ai_paused_until is in the future."""
    until = loan.ai_paused_until
    if until is None:
        return False
    # Make naive datetimes UTC-aware so comparison doesn't blow up; the
    # column is timestamptz so this should be redundant, but be defensive.
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until > datetime.now(timezone.utc)


def pause(loan: Loan, hours: float = DEFAULT_PAUSE_HOURS) -> datetime:
    """Set loan.ai_paused_until = now + hours and return the new value."""
    until = datetime.now(timezone.utc) + timedelta(hours=hours)
    loan.ai_paused_until = until
    return until


def resume(loan: Loan) -> None:
    """Clear the pause — used by the 'Resume AI now' super-admin button."""
    loan.ai_paused_until = None
