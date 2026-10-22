"""Shared minimum-retention policy for recoverable business records.

Ordinary intake and Bucket records are retained for at least two calendar
years.  A later lifecycle closure or archive extends that deadline; it never
shortens an existing compliance hold.  Reversible archive/soft-delete actions
remain allowed because they keep the database row and object bytes intact.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

MINIMUM_RETENTION_YEARS = 2
AI_INTAKE_RETENTION_CLASS = "ai_intake_record"
APPLICATION_RETENTION_CLASS = "application_record"
BUCKET_RETENTION_CLASS = "bucket_record"
BUCKET_EVIDENCE_RETENTION_CLASS = "bucket_evidence"

RetentionState = Literal["legal_hold", "protected", "expired", "unclassified"]


def utc(value: datetime) -> datetime:
    """Normalize legacy naive timestamps as UTC."""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def two_year_deadline(anchor: datetime) -> datetime:
    """Return two calendar years after ``anchor`` (Feb. 29 becomes Feb. 28)."""

    anchor = utc(anchor)
    try:
        return anchor.replace(year=anchor.year + MINIMUM_RETENTION_YEARS)
    except ValueError:
        return anchor.replace(
            year=anchor.year + MINIMUM_RETENTION_YEARS,
            day=28,
        )


def default_deadline() -> datetime:
    """SQLAlchemy callable used for new rows before their DB timestamp exists."""

    # created_at is stamped by the database a moment after this Python value.
    # The one-day safety margin keeps the DB's strict creation+2y constraint
    # true without weakening the policy.
    return two_year_deadline(datetime.now(UTC)) + timedelta(days=1)


def later_deadline(
    current: datetime | None,
    *,
    anchors: tuple[datetime | None, ...],
) -> datetime:
    """Extend retention from the latest known creation/termination anchor."""

    values = [utc(value) for value in anchors if value is not None]
    if not values:
        values.append(datetime.now(UTC))
    candidate = two_year_deadline(max(values))
    return max(utc(current), candidate) if current is not None else candidate


def retention_state(
    *,
    protected_until: datetime | None,
    legal_hold: bool,
    at: datetime | None = None,
) -> RetentionState:
    if legal_hold:
        return "legal_hold"
    if protected_until is None:
        return "unclassified"
    checked_at = utc(at or datetime.now(UTC))
    return "protected" if utc(protected_until) > checked_at else "expired"


def physical_deletion_prohibited(
    *,
    protected_until: datetime | None,
    legal_hold: bool,
    at: datetime | None = None,
    governed_purge: bool = False,
) -> bool:
    # Expiry makes a record eligible for review, not automatically deletable.
    # A future purge workflow must opt in explicitly and provide the matching
    # transaction-local database purge context.
    if not governed_purge:
        return True
    return retention_state(
        protected_until=protected_until,
        legal_hold=legal_hold,
        at=at,
    ) in {"legal_hold", "protected"}


class ProtectedRecordDeletionError(RuntimeError):
    """Raised when code attempts to physically delete a retained record."""

