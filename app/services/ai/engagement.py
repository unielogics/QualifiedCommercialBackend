"""AI engagement state — pause/resume helpers for human takeovers.

When a human sends a manual message in a client-facing thread, we park the AI
for an hour so it doesn't immediately step on the operator. The chat handler in
routers/loan_workspace.py calls `pause()` after each super-admin Chat-mode send;
routers/dealer_ai_intake.py does the same for the AI-intake client thread (the
window lives on the bucket upload link there). `is_paused()` is checked before
any auto-reply.

Any row carrying an `ai_paused_until` timestamp works — Loan and
BucketUploadLink today.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Pausable(Protocol):
    ai_paused_until: datetime | None


DEFAULT_PAUSE_HOURS = 1


def is_paused(loan: Pausable) -> bool:
    """True if the row's ai_paused_until is in the future."""
    until = loan.ai_paused_until
    if until is None:
        return False
    # Make naive datetimes UTC-aware so comparison doesn't blow up; the
    # column is timestamptz so this should be redundant, but be defensive.
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until > datetime.now(timezone.utc)


def pause(loan: Pausable, hours: float = DEFAULT_PAUSE_HOURS) -> datetime:
    """Set ai_paused_until = now + hours and return the new value."""
    until = datetime.now(timezone.utc) + timedelta(hours=hours)
    loan.ai_paused_until = until
    return until


def resume(loan: Pausable) -> None:
    """Clear the pause — used by the 'Resume AI now' super-admin button."""
    loan.ai_paused_until = None
