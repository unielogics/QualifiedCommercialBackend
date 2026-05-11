"""AITaskAssignment — one row per CRS row that's been assigned to AI.

This is the bulky JSON-config sibling to ClientRequirementStatus. CRS
carries the per-deal STATE (status, due date, evidence); this table
carries the per-deal AI INSTRUCTIONS (what to say, where to say it,
how often, whether to wait for human approval).

The 1:1 relationship is enforced by a partial unique index in the
0038 migration (`uq_ai_task_assignments_crs_live` where deleted_at
IS NULL). Soft-deleting an assignment lets us re-create it later
without a constraint conflict — useful for re-arming a task that
the user un-assigned and later changed their mind on.

Cadence engine reads this every 30 min:
  - assignment.channels  → which dispatcher to call
  - assignment.cadence   → hours_between_attempts + max_attempts
  - assignment.approval_mode → per-task override of the file-level mode
  - assignment.consent_state → SMS/email gates
  - assignment.next_run_at → when to next consider this assignment
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AITaskAssignment(Base):
    __tablename__ = "ai_task_assignments"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    client_requirement_status_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("client_requirement_status.id", ondelete="CASCADE"),
        nullable=False,
    )

    owner_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ai", server_default="ai",
    )
    """TaskOwnerType — always 'ai' on a live row, but the column exists
    in case we later want to record shared / handoff states."""

    instructions: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    """Free-text per-task instructions the agent/operator typed in the
    AssignmentDrawer ("Ask in a warm tone, mention the open house
    Saturday")."""

    instructions_visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="agent", server_default="agent",
    )
    """Visibility label inherited from AICollectionRequirement.visibility
    but overridable per assignment. internal | agent | borrower.
    Internal-only instructions are read by the AI as context but never
    appear in the rendered borrower-facing message."""

    channels: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False,
        default=lambda: ["portal"], server_default='["portal"]',
    )
    """OutreachChannel list. Subset of the file-level OutreachMode
    capabilities. Empty list disables outreach for this task even
    when the file is fully armed."""

    cadence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False,
        default=lambda: {"hours_between_attempts": 48, "max_attempts": 3},
        server_default='{"hours_between_attempts": 48, "max_attempts": 3}',
    )
    """Cadence policy:
      {
        "hours_between_attempts": int,
        "max_attempts": int,
        "escalation_user_id": uuid | null,
        "quiet_hours_start": "08:00",
        "quiet_hours_end":   "20:00"
      }
    """

    approval_mode: Mapped[str] = mapped_column(
        String(24), nullable=False,
        default="draft_first", server_default="draft_first",
    )
    """ApprovalMode — draft_first | auto_send_portal | require_per_message.
    Per-task override of the file-level OutreachMode. Sensitive tasks
    can stay draft_first even when the file is in portal_auto."""

    complete_file_by: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Per-task deadline. Distinct from the file-level complete_file_by
    on client_ai_plan.ai_secretary_settings."""

    # Per-deal override of the catalog row's link. NULL means "fall back
    # to the AICollectionRequirement.link_url".
    link_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    link_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    link_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Per-deal override of the catalog row's objective + completion
    # definition. NULL means "fall back to the catalog default".
    objective_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    completion_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    completion_mode: Mapped[str | None] = mapped_column(String(24), nullable=True)

    consent_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}",
    )
    """Consent state captured at the assignment level:
      {
        "sms":   {"state": "granted" | "not_granted",
                  "captured_at": "...", "language": "..."},
        "email": {"opt_out_at": "...", "reason": "..."},
        "voice": "disabled"
      }
    """

    attempts_made: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    """Incremented by the dispatcher on every successful send. Cadence
    engine stops firing when attempts_made >= cadence.max_attempts."""

    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    """When the cadence engine should next consider this assignment.
    NULL means "evaluate every pass" — set by the dispatcher to
    last_sent + hours_between_attempts after each send."""

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()",
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    """Soft-delete timestamp. The partial unique idx on
    client_requirement_status_id excludes soft-deleted rows so we can
    un-assign and re-assign without a constraint conflict."""
