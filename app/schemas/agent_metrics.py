"""Agent dashboard metrics — funnel KPIs + Next Best Actions.

Mirrors `/agents/me/funnel` and `/agents/me/next-actions`.

Two principles:

1. Every velocity / percentage metric carries a `sample_size` so
   the UI can dim itself when the value is computed over a tiny N.
   "Lead → Prequal averaged 4.2d" is meaningless if N=1; the API
   should make that visible without forcing the UI to recompute.

2. Each NextAction row carries BOTH a `deeplink` (web URL the
   desktop uses) AND a polymorphic `(target_type, target_id)` pair
   so the future mobile agent app can route natively without
   parsing URLs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class FunnelStat(BaseModel):
    """A single funnel metric + the count it was computed over."""
    value: float | None = None
    sample_size: int = 0


class FunnelMetricsRead(BaseModel):
    """Response shape for GET /agents/me/funnel."""

    # Counts
    leads_this_week: int
    contacted: int
    stale_lead_count: int

    # Percentages (0-100). value=None when sample_size=0.
    intake_completion: FunnelStat
    prequal_conversion: FunnelStat

    # Velocities (avg days). value=None when sample_size=0.
    lead_to_prequal: FunnelStat
    prequal_to_funded: FunnelStat

    # Distribution: {ClientStage value: count}
    clients_by_stage: dict[str, int]


class NextActionRead(BaseModel):
    """Response shape for one row in GET /agents/me/next-actions.

    Sorted server-side by priority desc → kind weight → created_at.
    Capped at 8 items per call. Per-client dedup keeps the highest-
    priority action; null-loan `pending_task` items ride alongside
    without affecting dedup.
    """

    # Idempotent key — `f"{kind}:{target_id}"`. Stable across
    # refetches as long as the underlying signal persists. The UI
    # uses this as the React `key` for animation / focus.
    id: str

    kind: Literal[
        "call_lead",       # stale client in lead/contacted, no recent contact
        "chase_doc",       # one of the broker's loans has overdue docs
        "closing_prep",    # loan close_date within 7d + open required docs
        "pending_task",    # AITask in PENDING for the broker's loans
    ]
    priority: Literal["high", "medium", "low"]

    # Display copy. Title is one line of action ("Call Sarah Smith").
    # Subtitle is the why ("Lead — no contact in 8 days").
    title: str
    subtitle: str

    # Polymorphic target so mobile can route without parsing the
    # deeplink. `target_id` is always present; for null-loan
    # pending_task items it points at the AITask UUID.
    target_type: Literal["client", "loan", "document", "ai_task"]
    target_id: UUID

    # Web-shaped path the desktop dashboard uses for click-through.
    # Mobile ignores this in favor of (target_type, target_id).
    deeplink: str

    # When the underlying signal materialized (last contact age,
    # doc overdue date, etc.). Used for sort + freshness display.
    created_at: datetime

    # Optional client_id — populated for client-scoped kinds. Used
    # by the per-client dedup so a client with both call_lead AND
    # chase_doc collapses to the single highest-priority signal.
    client_id: UUID | None = None
    loan_id: UUID | None = None
