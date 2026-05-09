"""Lead routing / attribution / promotion fields.

Revision ID: 0029
Revises: 0028
Create Date: 2026-05-09

Spec: AgentLeadModal vs SmartIntakeModal split. Agent flow captures a
real-estate lead (Client only); super-admin / underwriter flow creates
the Loan. Promotion from agent-lead → loan is a separate controlled
action via /clients/{id}/request-funding-review.

CLIENT additions — capture how the lead came in + who owns it:

  lead_source                 manual_entry | open_house | referral |
                              listing_inquiry | buyer_consultation |
                              existing_database | other
  lead_temperature            hot | warm | nurture
  financing_support_needed    yes | maybe | no | unknown
  contact_permission          send_invite_now | save_lead_only |
                              agent_will_introduce_first
  relationship_context        new_lead | existing_client | past_client |
                              referral_from_other | other
  lead_promotion_status       not_ready (default) | agent_requested_review |
                              funding_reviewing | promoted_to_intake |
                              declined
  originating_agent_id        FK users.id — who FIRST captured the lead
  current_agent_id            FK users.id — who owns it today
  source_channel              agent_dashboard | agent_mobile | super_admin |
                              direct_signup | etc.

LOAN additions — attribution + ownership at loan-origination time:

  source_attribution          direct_borrower | agent_referral |
                              existing_client | website | phone_call | other
  referring_agent_id          FK users.id — when source_attribution =
                              agent_referral
  assigned_owner_id           FK users.id — operational owner of the
                              loan (defaults to creator)
  invite_behavior             send_immediately (default) | save_draft |
                              send_after_review

DOCUMENT additions — who is the AI chasing?

  requested_from              borrower (default) | agent | funding_team |
                              title | internal

The cron only chases borrower-tagged docs. Other tags hold the doc
visible-to-operator without firing borrower-side reminders.

All columns nullable; existing rows leave them NULL.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Client ────────────────────────────────────────────────────────
    op.add_column("clients", sa.Column("lead_source", sa.String(32), nullable=True))
    op.add_column("clients", sa.Column("lead_temperature", sa.String(16), nullable=True))
    op.add_column("clients", sa.Column("financing_support_needed", sa.String(16), nullable=True))
    op.add_column("clients", sa.Column("contact_permission", sa.String(32), nullable=True))
    op.add_column("clients", sa.Column("relationship_context", sa.String(32), nullable=True))
    op.add_column(
        "clients",
        sa.Column(
            "lead_promotion_status",
            sa.String(32),
            nullable=False,
            server_default="not_ready",
        ),
    )
    op.add_column(
        "clients",
        sa.Column(
            "originating_agent_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "clients",
        sa.Column(
            "current_agent_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("clients", sa.Column("source_channel", sa.String(32), nullable=True))

    # ── Loan ──────────────────────────────────────────────────────────
    op.add_column("loans", sa.Column("source_attribution", sa.String(32), nullable=True))
    op.add_column(
        "loans",
        sa.Column(
            "referring_agent_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "loans",
        sa.Column(
            "assigned_owner_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "loans",
        sa.Column(
            "invite_behavior",
            sa.String(32),
            nullable=False,
            server_default="send_immediately",
        ),
    )

    # ── Document ──────────────────────────────────────────────────────
    op.add_column(
        "documents",
        sa.Column(
            "requested_from",
            sa.String(16),
            nullable=False,
            server_default="borrower",
        ),
    )


def downgrade() -> None:
    op.drop_column("documents", "requested_from")
    op.drop_column("loans", "invite_behavior")
    op.drop_column("loans", "assigned_owner_id")
    op.drop_column("loans", "referring_agent_id")
    op.drop_column("loans", "source_attribution")
    op.drop_column("clients", "source_channel")
    op.drop_column("clients", "current_agent_id")
    op.drop_column("clients", "originating_agent_id")
    op.drop_column("clients", "lead_promotion_status")
    op.drop_column("clients", "relationship_context")
    op.drop_column("clients", "contact_permission")
    op.drop_column("clients", "financing_support_needed")
    op.drop_column("clients", "lead_temperature")
    op.drop_column("clients", "lead_source")
