"""AI Deal Secretary — assignments + outreach events + catalog upgrade.

Revision ID: 0038
Revises: 0037
Create Date: 2026-05-11

The Deal Secretary lets agents/operators drag tasks from a "standard"
column to an "AI handles this" column. This migration lays the schema
foundation:

  1.  AICollectionRequirement (the catalog) gets:
        - default_owner_type        — what owner a fresh deal inherits
        - default_channels          — which outreach channels are pre-armed
        - default_cadence_hours     — how often to follow up
        - link_url / link_label /
          link_kind                 — optional DocuSign / e-sign URL
        - objective_text            — plain-language "what AI is doing"
        - completion_criteria       — explicit "done" definition
        - completion_mode           — ai_can_complete / requires_human_verify / borrower_self_attest
        - wrong_upload_response_template — what AI says when scanner flags a
                                            mis-uploaded file
        - category is WIDENED to varchar(40) so the new RequirementCategory
          enum (12 values) fits. Existing rows are remapped:
            fact        → borrower_info
            document    → financials        (re-categorized properly in 0039 seed)
            appointment → scheduling
            agreement   → agreements
            task        → communication

  2.  ClientRequirementStatus (the per-deal state) gets:
        - owner_type        — human / ai / shared / funding_locked
        - ai_assignment_id  — FK to ai_task_assignments
        - last_response_at  — last borrower reply timestamp; cadence
                              engine reads this to avoid re-asking inside
                              the configured window

  3.  ClientAIPlan gets ai_secretary_settings JSONB for the FILE-LEVEL
      outreach_mode (off / draft_first / portal_auto / portal_email /
      portal_email_sms), complete_file_by date, SMS consent + email
      opt-out state. This is the sticky kill-switch the user sees at the
      top of Step 4 and the AI Workbench.

  4.  AICadenceRule gets requires_ai_owner bool. Existing rules backfill
      to false so legacy behavior is preserved; new rules created by the
      Deal Secretary write true (no outreach without an assignment).

  5.  NEW TABLE ai_task_assignments — one row per CRS row that's been
      assigned to AI. Carries instructions, channels, cadence policy,
      approval mode, complete_file_by, consent state, link override.

  6.  NEW TABLE ai_outreach_events — append-only log. Every dispatch
      attempt writes a row BEFORE the provider call so failures stay
      in the audit trail.

Notes on safety:
- Defaults are non-NULL, so the DDL is non-blocking.
- ClientRequirementStatus has 0 rows in prod today; backfill is a no-op.
- AICollectionRequirement has 71 rows; the category remap is the only
  data transformation and it's idempotent.
- Defensive assertion at the end of upgrade() verifies no row ends up
  with owner_type='ai' post-backfill — if it did, every existing deal
  would queue outreach the moment Phase 4 (outreach engine) ships.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0038"
down_revision: Union[str, None] = "0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ----------------------------------------------------------------------
    # 1. ai_collection_requirements — widen category + add 8 columns
    # ----------------------------------------------------------------------
    # Widen category from varchar(16) → varchar(40) so the new closed
    # taxonomy fits.
    op.alter_column(
        "ai_collection_requirements",
        "category",
        type_=sa.String(40),
        existing_nullable=False,
    )

    # Remap legacy 5-value category set onto the new 12-value taxonomy.
    # The 0039 seed migration later re-categorizes individual platform
    # requirements correctly (e.g. purchase_contract → agreements rather
    # than the broad "financials" landing zone). This pass just keeps
    # every existing row valid against the new enum.
    op.execute("""
        UPDATE ai_collection_requirements SET category = CASE category
            WHEN 'fact'        THEN 'borrower_info'
            WHEN 'document'    THEN 'financials'
            WHEN 'appointment' THEN 'scheduling'
            WHEN 'agreement'   THEN 'agreements'
            WHEN 'task'        THEN 'communication'
            ELSE category
        END
    """)

    op.add_column(
        "ai_collection_requirements",
        sa.Column(
            "default_owner_type",
            sa.String(16),
            nullable=False,
            server_default="human",
        ),
    )
    op.add_column(
        "ai_collection_requirements",
        sa.Column(
            "default_channels",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default='["portal"]',
        ),
    )
    op.add_column(
        "ai_collection_requirements",
        sa.Column(
            "default_cadence_hours",
            sa.Integer(),
            nullable=False,
            server_default="48",
        ),
    )
    op.add_column(
        "ai_collection_requirements",
        sa.Column("link_url", sa.String(2000), nullable=True),
    )
    op.add_column(
        "ai_collection_requirements",
        sa.Column("link_label", sa.String(120), nullable=True),
    )
    op.add_column(
        "ai_collection_requirements",
        sa.Column("link_kind", sa.String(32), nullable=True),
    )
    op.add_column(
        "ai_collection_requirements",
        sa.Column(
            "objective_text",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "ai_collection_requirements",
        sa.Column(
            "completion_criteria",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "ai_collection_requirements",
        sa.Column(
            "completion_mode",
            sa.String(24),
            nullable=False,
            server_default="ai_can_complete",
        ),
    )
    op.add_column(
        "ai_collection_requirements",
        sa.Column("wrong_upload_response_template", sa.Text(), nullable=True),
    )

    # ----------------------------------------------------------------------
    # 2. client_requirement_status — owner_type + ai_assignment_id + last_response_at
    # ----------------------------------------------------------------------
    op.add_column(
        "client_requirement_status",
        sa.Column(
            "owner_type",
            sa.String(16),
            nullable=False,
            server_default="human",
        ),
    )
    op.add_column(
        "client_requirement_status",
        sa.Column(
            "ai_assignment_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "client_requirement_status",
        sa.Column(
            "last_response_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_client_requirement_status_owner",
        "client_requirement_status",
        ["loan_id", "owner_type"],
    )

    # ----------------------------------------------------------------------
    # 3. client_ai_plan.ai_secretary_settings — file-level outreach mode
    # ----------------------------------------------------------------------
    op.add_column(
        "client_ai_plan",
        sa.Column(
            "ai_secretary_settings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default='{"outreach_mode": "draft_first"}',
        ),
    )

    # ----------------------------------------------------------------------
    # 4. ai_cadence_rules.requires_ai_owner — new rules gate on assignment
    # ----------------------------------------------------------------------
    op.add_column(
        "ai_cadence_rules",
        sa.Column(
            "requires_ai_owner",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # ----------------------------------------------------------------------
    # 5. ai_task_assignments — the per-deal AI work record
    # ----------------------------------------------------------------------
    op.create_table(
        "ai_task_assignments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "client_requirement_status_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("client_requirement_status.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_type",
            sa.String(16),
            nullable=False,
            server_default="ai",
        ),
        # Free-text per-task instructions the agent/operator typed.
        sa.Column("instructions", sa.Text(), nullable=False, server_default=""),
        # Visibility label inherited from AICollectionRequirement.visibility
        # but overridable per assignment (e.g. internal-only note that the
        # AI consults but never renders to the borrower).
        sa.Column(
            "instructions_visibility",
            sa.String(16),
            nullable=False,
            server_default="agent",
        ),
        # Per-task channel list — subset of the file-level OutreachMode
        # capabilities. Default ["portal"] is the safest channel.
        sa.Column(
            "channels",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default='["portal"]',
        ),
        # Cadence policy:
        # {hours_between_attempts, max_attempts, escalation_user_id,
        #  quiet_hours_start, quiet_hours_end}
        sa.Column(
            "cadence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default='{"hours_between_attempts": 48, "max_attempts": 3}',
        ),
        # Per-task override of the file-level OutreachMode. Sensitive tasks
        # can stay draft_first even when the file is in portal_auto.
        sa.Column(
            "approval_mode",
            sa.String(24),
            nullable=False,
            server_default="draft_first",
        ),
        # Optional per-task deadline. Distinct from the file-level
        # complete_file_by on ai_secretary_settings (this is per-task).
        sa.Column("complete_file_by", sa.Date(), nullable=True),
        # Per-deal override of the catalog row's link (e.g. agent's
        # personal DocuSign envelope URL for THIS borrower).
        sa.Column("link_url", sa.String(2000), nullable=True),
        sa.Column("link_label", sa.String(120), nullable=True),
        sa.Column("link_kind", sa.String(32), nullable=True),
        # Per-deal override of the catalog row's objective + completion
        # definition. NULL means "fall back to the catalog default".
        sa.Column("objective_text", sa.Text(), nullable=True),
        sa.Column("completion_criteria", sa.Text(), nullable=True),
        sa.Column("completion_mode", sa.String(24), nullable=True),
        # Consent state:
        # {sms: {state, captured_at, language}, email: {opt_out_at},
        #  voice: "disabled"}
        sa.Column(
            "consent_state",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default='{}',
        ),
        sa.Column(
            "attempts_made",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        # next_run_at is when the cadence engine should next consider
        # this assignment. NULL means "evaluate every pass".
        sa.Column(
            "next_run_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # One canonical assignment per CRS row (partial unique idx so we can
    # soft-delete and re-create without a constraint conflict).
    op.create_index(
        "uq_ai_task_assignments_crs_live",
        "ai_task_assignments",
        ["client_requirement_status_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_ai_task_assignments_next_run",
        "ai_task_assignments",
        ["next_run_at"],
        postgresql_where=sa.text("deleted_at IS NULL AND next_run_at IS NOT NULL"),
    )

    # Now that the table exists, add the FK from CRS → assignment.
    op.create_foreign_key(
        "fk_client_requirement_status_ai_assignment",
        "client_requirement_status",
        "ai_task_assignments",
        ["ai_assignment_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ----------------------------------------------------------------------
    # 6. ai_outreach_events — append-only dispatch log
    # ----------------------------------------------------------------------
    op.create_table(
        "ai_outreach_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "assignment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_task_assignments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column(
            "direction",
            sa.String(8),
            nullable=False,
            server_default="outbound",
        ),
        sa.Column("template_key", sa.String(120), nullable=True),
        # Provider-side message reference: gmail message id, sms provider
        # sid, internal ai_chat_messages.id for portal events.
        sa.Column("message_id", sa.String(200), nullable=True),
        sa.Column(
            "ai_chat_message_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "scheduled_for",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_ai_outreach_events_assignment_sent",
        "ai_outreach_events",
        ["assignment_id", "sent_at"],
        postgresql_using="btree",
    )

    # ----------------------------------------------------------------------
    # Defensive assertion: after backfill, no CRS row should already have
    # owner_type='ai'. If somehow one does, the migration aborts — without
    # this, every existing deal would queue outreach the moment Phase 4
    # (outreach engine) ships.
    # ----------------------------------------------------------------------
    op.execute("""
        DO $$
        DECLARE
            ai_count integer;
        BEGIN
            SELECT COUNT(*) INTO ai_count
            FROM client_requirement_status
            WHERE owner_type = 'ai';
            IF ai_count > 0 THEN
                RAISE EXCEPTION
                    '0038 safety check failed: % CRS rows already have owner_type=ai. '
                    'This should never happen on a fresh backfill. Investigate before retrying.',
                    ai_count;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    # Drop in reverse order. Indexes go before tables; FK before column.
    op.drop_index("ix_ai_outreach_events_assignment_sent", table_name="ai_outreach_events")
    op.drop_table("ai_outreach_events")

    op.drop_constraint(
        "fk_client_requirement_status_ai_assignment",
        "client_requirement_status",
        type_="foreignkey",
    )
    op.drop_index("ix_ai_task_assignments_next_run", table_name="ai_task_assignments")
    op.drop_index("uq_ai_task_assignments_crs_live", table_name="ai_task_assignments")
    op.drop_table("ai_task_assignments")

    op.drop_column("ai_cadence_rules", "requires_ai_owner")
    op.drop_column("client_ai_plan", "ai_secretary_settings")

    op.drop_index("ix_client_requirement_status_owner", table_name="client_requirement_status")
    op.drop_column("client_requirement_status", "last_response_at")
    op.drop_column("client_requirement_status", "ai_assignment_id")
    op.drop_column("client_requirement_status", "owner_type")

    op.drop_column("ai_collection_requirements", "wrong_upload_response_template")
    op.drop_column("ai_collection_requirements", "completion_mode")
    op.drop_column("ai_collection_requirements", "completion_criteria")
    op.drop_column("ai_collection_requirements", "objective_text")
    op.drop_column("ai_collection_requirements", "link_kind")
    op.drop_column("ai_collection_requirements", "link_label")
    op.drop_column("ai_collection_requirements", "link_url")
    op.drop_column("ai_collection_requirements", "default_cadence_hours")
    op.drop_column("ai_collection_requirements", "default_channels")
    op.drop_column("ai_collection_requirements", "default_owner_type")

    # Reverse category remap. Best-effort — `borrower_info`/`scheduling`
    # are 1:1 reversible, but `financials` collapses both `document` and
    # the new in-the-wild `financials` rows; we can't tell them apart, so
    # we round-trip to `document` (the original superset).
    op.execute("""
        UPDATE ai_collection_requirements SET category = CASE category
            WHEN 'borrower_info'  THEN 'fact'
            WHEN 'financials'     THEN 'document'
            WHEN 'scheduling'     THEN 'appointment'
            WHEN 'agreements'     THEN 'agreement'
            WHEN 'communication'  THEN 'task'
            ELSE 'document'
        END
    """)
    op.alter_column(
        "ai_collection_requirements",
        "category",
        type_=sa.String(16),
        existing_nullable=False,
    )
