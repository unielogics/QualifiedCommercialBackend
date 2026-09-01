"""Store the current client-room PIN encrypted for authorized staff display.

Revision ID: 0175_client_room_pin_recovery
Revises: 0174_contract_program_sets
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0175_client_room_pin_recovery"
down_revision = "0174_contract_program_sets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bucket_upload_links",
        sa.Column("encrypted_passcode", sa.Text(), nullable=True),
    )
    op.add_column(
        "bucket_upload_links",
        sa.Column("passcode_encryption_provider", sa.String(length=24), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bucket_upload_links", "passcode_encryption_provider")
    op.drop_column("bucket_upload_links", "encrypted_passcode")
