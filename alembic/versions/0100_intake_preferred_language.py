"""Client-facing language preference on public_underwriting_intakes.

Adds preferred_language: the client's chosen (or admin/broker-assigned) UI +
AI-chat language for a dealer/funding-review intake lead. Set once at
creation (self-serve pick on the public landing page, or an admin/broker's
pick on the client's behalf) and sticky thereafter -- only an admin can
change it post-creation, via a new PATCH endpoint that mirrors how
outcome_status is admin-only writable.

Purely additive; no existing columns touched.

Revision ID: 0100_intake_preferred_language
Revises: 0099_intake_broker_and_outcome
Create Date: 2026-07-31
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0100_intake_preferred_language"
down_revision = "0099_intake_broker_and_outcome"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "public_underwriting_intakes",
        sa.Column(
            "preferred_language",
            sa.String(8),
            nullable=False,
            server_default="en",
        ),
    )


def downgrade() -> None:
    op.drop_column("public_underwriting_intakes", "preferred_language")
