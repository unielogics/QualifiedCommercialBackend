"""Persist the AI Intake client-reply SMS preference.

Revision ID: 0215_persistent_intake_sms_preference
Revises: 0214_ai_intake_communications
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0215_persistent_intake_sms_preference"
down_revision = "0214_ai_intake_communications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "application_profiles",
        sa.Column(
            "client_sms_delivery_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("application_profiles", "client_sms_delivery_enabled")
