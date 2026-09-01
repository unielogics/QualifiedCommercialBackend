"""Training-file classification and persistent workflow gating.

Revision ID: 0162_training_workflow_controls
Revises: 0161_field_desk_routing_resolution
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0162_training_workflow_controls"
down_revision = "0161_field_desk_routing_resolution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dos_dealers",
        sa.Column("is_training", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "dos_dealers",
        sa.Column("workflow_ungated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_dos_dealers_is_training", "dos_dealers", ["is_training"])


def downgrade() -> None:
    op.drop_index("ix_dos_dealers_is_training", table_name="dos_dealers")
    op.drop_column("dos_dealers", "workflow_ungated")
    op.drop_column("dos_dealers", "is_training")
