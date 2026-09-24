"""Add call-back outcome, prospect CC defaults, and email void audit.

Revision ID: 0227_prospect_outcome_cc_voiding
Revises: 0226_marketing_contact_archive
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0227_prospect_outcome_cc_voiding"
down_revision = "0226_marketing_contact_archive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dealer_prospects",
        sa.Column(
            "default_cc_emails",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "dealer_prospect_email_drafts",
        sa.Column(
            "cc_emails",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "dealer_prospect_email_drafts",
        sa.Column(
            "ai_context_manifest",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "dealer_prospect_email_drafts",
        sa.Column("cancelled_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "dealer_prospect_email_drafts",
        sa.Column("cancellation_source", sa.String(32), nullable=True),
    )
    op.create_foreign_key(
        "fk_prospect_email_drafts_cancelled_by_user",
        "dealer_prospect_email_drafts",
        "users",
        ["cancelled_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Preserve admin-customized labels: only rename the untouched default.
    op.execute(
        sa.text(
            "UPDATE dealer_prospect_outcome_definitions "
            "SET label = 'We will call the client back' "
            "WHERE key = 'call_back' AND label = 'Not available / call back'"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO dealer_prospect_outcome_definitions "
            "(id, key, label, sort_order, is_active, is_system, action_config) VALUES "
            "('3f55d826-3be7-44dd-92cb-09461c15aa08', "
            "'client_will_call_back', 'Client will call back', 15, true, true, "
            "'{\"increment_call_attempt\": true, \"follow_up_business_days\": 2, "
            "\"email_action\": \"client_will_call_back\"}'::jsonb) "
            "ON CONFLICT (key) DO NOTHING"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM dealer_prospect_outcome_definitions "
            "WHERE key = 'client_will_call_back' AND is_system = true"
        )
    )
    op.execute(
        sa.text(
            "UPDATE dealer_prospect_outcome_definitions "
            "SET label = 'Not available / call back' "
            "WHERE key = 'call_back' AND label = 'We will call the client back'"
        )
    )
    op.drop_constraint(
        "fk_prospect_email_drafts_cancelled_by_user",
        "dealer_prospect_email_drafts",
        type_="foreignkey",
    )
    op.drop_column("dealer_prospect_email_drafts", "cancellation_source")
    op.drop_column("dealer_prospect_email_drafts", "cancelled_by_user_id")
    op.drop_column("dealer_prospect_email_drafts", "ai_context_manifest")
    op.drop_column("dealer_prospect_email_drafts", "cc_emails")
    op.drop_column("dealer_prospects", "default_cc_emails")
