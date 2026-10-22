"""Canonical AI Intake lifecycle and workspace visibility helpers.

An intake can leave active work for two very different reasons:

* the business lifecycle closed (denied, lost, disqualified, or withdrawn); or
* an operator explicitly archived the document bucket.

Those states must remain distinct.  In particular, a denial must never mutate
``Bucket.archived_at`` because that flag is also an access gate for evidence
and a manual restore control.  Closed files stay intact and readable; list
queries merely move them from the active queue to the archive/closed queue.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

WorkspaceState = Literal["active", "closed", "archived"]

# ``disqualified`` is retained as a compatibility value for historical/imported
# rows even though the canonical underwriting lifecycle calls it ``denied``.
TERMINAL_INTAKE_STATUSES = ("closed", "closed_lost", "denied", "disqualified")
TERMINAL_OUTCOME_STATUSES = ("denied", "disqualified")
TERMINAL_UNDERWRITING_STATUSES = ("closed_lost", "denied")
TERMINAL_FORECLOSURE_STATUSES = ("declined", "withdrawn_expired")

UNDERWRITING_TO_INTAKE_STATUS = {
    "submitted": "collecting",
    "collecting_docs": "reviewing",
    "in_underwriting": "reviewed",
    "term_sheet_provided": "reviewed",
    "approved": "approved",
    "closed_won": "completed",
    "closed_lost": "closed_lost",
    "denied": "denied",
}

UNDERWRITING_TO_INTAKE_OUTCOME = {
    "submitted": "submitted",
    "collecting_docs": "submitted",
    "in_underwriting": "submitted",
    "term_sheet_provided": "submitted",
    "approved": "submitted",
    "closed_won": "closed",
    "closed_lost": "closed",
    "denied": "denied",
}


class _MutableIntake(Protocol):
    status: str
    outcome_status: str


def sync_intake_from_underwriting(
    intake: _MutableIntake,
    underwriting_status: str,
) -> None:
    """Mirror a canonical underwriting status onto the legacy intake fields.

    This is deliberately a value-only helper.  It neither archives the bucket
    nor deletes evidence, and callers keep ownership of their transaction.
    """

    intake_status = UNDERWRITING_TO_INTAKE_STATUS.get(underwriting_status)
    outcome_status = UNDERWRITING_TO_INTAKE_OUTCOME.get(underwriting_status)
    if intake_status is None or outcome_status is None:
        return
    intake.status = intake_status
    intake.outcome_status = outcome_status


def intake_workspace_state(
    *,
    bucket_archived_at: datetime | None,
    intake_status: str | None,
    outcome_status: str | None,
    foreclosure_status: str | None = None,
    underwriting_status: str | None = None,
) -> tuple[WorkspaceState, str | None]:
    """Classify list visibility without conflating closure with deletion.

    Explicit/manual archive wins because it is the only state that should
    expose a Restore action.  A lifecycle closure is kept as ``closed`` so the
    UI can show it in the archive queue while retaining full evidence access.
    ``completed`` alone is intentionally not terminal: older workflows use it
    for review completion.  Only canonical ``closed_won`` or an explicit
    foreclosure ``funded`` value proves funding.
    """

    if bucket_archived_at is not None:
        return "archived", "manual_archive"

    normalized_underwriting = (underwriting_status or "").strip().lower()
    normalized_foreclosure = (foreclosure_status or "").strip().lower()
    if normalized_underwriting == "closed_won" or normalized_foreclosure == "funded":
        # Funding is an affirmative terminal fact and must not be hidden by a
        # stale pre-funding denial value left on an imported intake.
        return "active", None
    if normalized_underwriting in TERMINAL_UNDERWRITING_STATUSES:
        return "closed", normalized_underwriting
    if normalized_foreclosure in TERMINAL_FORECLOSURE_STATUSES:
        return "closed", normalized_foreclosure

    normalized_status = (intake_status or "").strip().lower()
    if normalized_status in TERMINAL_INTAKE_STATUSES:
        return "closed", normalized_status

    normalized_outcome = (outcome_status or "").strip().lower()
    if normalized_outcome in TERMINAL_OUTCOME_STATUSES:
        return "closed", normalized_outcome

    return "active", None
