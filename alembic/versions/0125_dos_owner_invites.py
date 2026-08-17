"""dealer-os: primary-owner flag + owner credit-consent invites.

The CLIENT may run their own soft pull exactly once (is_primary marks
which owner row is the login's own person). Every ADDITIONAL owner's
pull runs through a secure one-time consent link that the super admin
shares with that owner directly — consent must come from the person the
pull is about, never from the client on their behalf.

Revision ID: 0125_dos_owner_invites
Revises: 0124_dos_message_seen
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0125_dos_owner_invites"
down_revision = "0124_dos_message_seen"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dos_owners",
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column("dos_owners", sa.Column("invite_token_hash", sa.String(64), nullable=True))
    op.add_column("dos_owners", sa.Column("invite_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("dos_owners", sa.Column("invite_opened_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_dos_owners_invite_token", "dos_owners", ["invite_token_hash"], unique=False
    )
    # DB-level single-primary guarantee (check-then-insert alone races).
    op.create_index(
        "uq_dos_owners_one_primary",
        "dos_owners",
        ["dealer_id"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
    )


def downgrade() -> None:
    op.drop_index("uq_dos_owners_one_primary", table_name="dos_owners")
    op.drop_index("ix_dos_owners_invite_token", table_name="dos_owners")
    op.drop_column("dos_owners", "invite_opened_at")
    op.drop_column("dos_owners", "invite_sent_at")
    op.drop_column("dos_owners", "invite_token_hash")
    op.drop_column("dos_owners", "is_primary")
