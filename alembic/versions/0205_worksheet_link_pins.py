"""A second factor on a shared worksheet, and a record of what was typed into it.

Split from 0204 rather than folded into it because the two answer different
questions and were built in parallel: 0204 is what a worksheet *is* — the clock,
the sheets a link opens, the edit/view bit. This one is what stands in front of
it and what it leaves behind.

**The PIN columns** are the production package's, column for column, because
the problem is the same one and a second implementation of a lockout is a second
place to get it wrong. `pin_attempts` and `pin_locked_until` live on the row and
not in process memory: this instance restarts on every deploy, and a lockout a
deploy forgets is not a lockout. They are nullable/zero-defaulted, so every
financial form link minted before today is untouched and keeps behaving exactly
as it does now — a bare bearer token, which for a form somebody opens once was
the deliberate trade recorded in `financial_form_link.py`. A worksheet changes
the duration and the reach: bookmarked, forwarded, writable for weeks.

**`financial_worksheet_edits`** closes a real gap. The public draft route saves
a borrower's balance sheet today and writes no audit of any kind — no action
log, no file event, not even a `last_used_at` touch on the POST. A worksheet
invites continuous editing by someone with no account, so it needs the trail
the form never had: one row per accepted batch, the `{address: {before, after}}`
diff shape `production_packages.apply_changes` already uses, and what was
*proved* (link, ip, user agent) kept in different columns from what was merely
*claimed* (a name typed into a prompt).

`link_id` is ON DELETE SET NULL, not CASCADE. Revoking a link must never erase
the record of what was done with it — that is the moment the record matters most.

Revision ID: 0205_worksheet_link_pins
Revises: 0204_financial_worksheets_and_link_scopes
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0205_worksheet_link_pins"
down_revision = "0204_financial_worksheets_and_link_scopes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("financial_form_links", sa.Column("pin_hash", sa.String(160), nullable=True))
    op.add_column(
        "financial_form_links",
        sa.Column("pin_set_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "financial_form_links",
        sa.Column("pin_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "financial_form_links",
        sa.Column("pin_locked_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "financial_form_links",
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "financial_worksheet_edits",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "worksheet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("financial_worksheets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("sheet_kind", sa.String(24), nullable=False),
        sa.Column(
            "changes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "link_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("financial_form_links.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("claimed_name", sa.String(120), nullable=True),
        sa.Column("via", sa.String(16), nullable=False, server_default="share_link"),
        sa.Column("ip", sa.String(80), nullable=True),
        sa.Column("user_agent", sa.String(400), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "sheet_kind in ('pfs','debt_schedule','p_and_l','balance_sheet')",
            name="ck_financial_worksheet_edits_kind",
        ),
        sa.CheckConstraint(
            "via in ('operator','share_link')", name="ck_financial_worksheet_edits_via"
        ),
    )
    op.create_index(
        "ix_financial_worksheet_edits_worksheet_id",
        "financial_worksheet_edits",
        ["worksheet_id"],
    )
    op.create_index(
        "ix_financial_worksheet_edits_link_id", "financial_worksheet_edits", ["link_id"]
    )
    # The read this table exists for: "what did this worksheet's session
    # change, in order". Composite so that answer is one index scan.
    op.create_index(
        "ix_financial_worksheet_edits_worksheet_created",
        "financial_worksheet_edits",
        ["worksheet_id", "created_at"],
    )


def downgrade() -> None:
    # The audit log goes with the feature it audits. Nothing else reads it, and
    # keeping an orphaned table whose foreign keys point at a dropped worksheet
    # table would not survive 0204's own downgrade anyway.
    op.drop_index(
        "ix_financial_worksheet_edits_worksheet_created",
        table_name="financial_worksheet_edits",
    )
    op.drop_index("ix_financial_worksheet_edits_link_id", table_name="financial_worksheet_edits")
    op.drop_index(
        "ix_financial_worksheet_edits_worksheet_id", table_name="financial_worksheet_edits"
    )
    op.drop_table("financial_worksheet_edits")

    op.drop_column("financial_form_links", "use_count")
    op.drop_column("financial_form_links", "pin_locked_until")
    op.drop_column("financial_form_links", "pin_attempts")
    op.drop_column("financial_form_links", "pin_set_at")
    op.drop_column("financial_form_links", "pin_hash")
