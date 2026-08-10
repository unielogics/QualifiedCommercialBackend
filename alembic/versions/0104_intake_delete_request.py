"""Two-step delete-request fields on public_underwriting_intakes.

Adds:
  - delete_requested_at: nullable timestamp, set when a broker (or admin)
    flags a lead for deletion. Setting this alone destroys nothing -- it
    only hides the lead from the requesting broker's own list (mirrors
    Bucket.archived_at's existing filter-out convention). The admin list
    never filters this out, so admin always sees pending requests.
  - delete_requested_by_user_id: nullable FK -> users.id (ON DELETE SET
    NULL), who requested it.

A separate super-admin "confirm delete" endpoint (gated on
delete_requested_at IS NOT NULL) performs the actual irreversible hard
delete of the bucket/files/intake -- this migration only adds the request
flag, not any delete mechanics.

Purely additive; no existing columns touched.

Revision ID: 0104_intake_delete_request
Revises: 0103_deal_registrations
Create Date: 2026-08-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg


revision = "0104_intake_delete_request"
down_revision = "0103_deal_registrations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "public_underwriting_intakes",
        sa.Column("delete_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "public_underwriting_intakes",
        sa.Column(
            "delete_requested_by_user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("public_underwriting_intakes", "delete_requested_by_user_id")
    op.drop_column("public_underwriting_intakes", "delete_requested_at")
