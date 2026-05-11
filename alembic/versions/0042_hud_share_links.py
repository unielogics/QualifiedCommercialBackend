"""HUD share links — invite external parties to fill in HUD line items.

Revision ID: 0042
Revises: 0041
Create Date: 2026-05-11

Adds a `hud_share_links` table that mints a public token per loan, so
the operator can drop a URL on title / escrow / insurance contacts and
have them upload their settlement line items directly into the loan's
HUD without a Clerk account.

Token is a long random URL-safe string (no PII); revocation is a soft
flag so we keep audit history. Optional `expires_at` lets the operator
auto-expire the link if they want a short window.

Also widens `hud_line_items` with:
  • created_by_share_link_id — points back to the share link if the row
    was added by an external party (so we can mark it visually as
    "from <vendor>"); NULL for operator-created rows.
  • payee — free-text "who is being paid" so the table reads like a
    real settlement statement and not just "category + amount".
  • note — small free-text field for vendor-side context (invoice #,
    quote ref, anything).

Non-destructive — existing rows keep working with the new fields NULL.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hud_share_links",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("loan_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("loans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token", sa.String(64), nullable=False, unique=True),
        sa.Column("label", sa.String(120), nullable=True),
        sa.Column("invitee_email", sa.String(254), nullable=True),
        sa.Column("invitee_role", sa.String(40), nullable=True),
        sa.Column("created_by", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_hud_share_links_loan_active",
        "hud_share_links",
        ["loan_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "ix_hud_share_links_token",
        "hud_share_links",
        ["token"],
        unique=True,
    )

    op.add_column(
        "hud_line_items",
        sa.Column(
            "created_by_share_link_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("hud_share_links.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "hud_line_items",
        sa.Column("payee", sa.String(160), nullable=True),
    )
    op.add_column(
        "hud_line_items",
        sa.Column("note", sa.String(500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("hud_line_items", "note")
    op.drop_column("hud_line_items", "payee")
    op.drop_column("hud_line_items", "created_by_share_link_id")
    op.drop_index("ix_hud_share_links_token", table_name="hud_share_links")
    op.drop_index("ix_hud_share_links_loan_active", table_name="hud_share_links")
    op.drop_table("hud_share_links")
