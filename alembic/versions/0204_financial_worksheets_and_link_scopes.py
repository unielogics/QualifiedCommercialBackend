"""The four forms as one live worksheet, and links that open part of it.

`financial_worksheets` is a clock, not a store. The figures stay in
`business_financial_statements.body`, `financial_statements.body` and
`dos_debts`; what this table adds is a single monotonic `revision` per
workbook, bumped under `SELECT … FOR UPDATE` once per accepted batch of cell
edits. The lock is what makes read-modify-write on a JSONB body safe when a
save is a keystroke rather than a button; the counter is what lets a client
tell a stale echo from news. `sheet_rev` on the two statement tables records
the clock reading at which that sheet last changed. The debt schedule has no
statement row of its own and reads the clock directly.

`financial_form_links` gains the two columns that make a shared link narrower
than the file. `permission` defaults to `'edit'` because that is precisely
what every link minted before today already allowed — a form link has always
been a write credential — so every existing row keeps working untouched, and
a view-only link is a deliberate narrowing. `worksheet_id` joins links to each
other by data rather than by the packet's `{base}.{kind}` token derivation,
under which any child token yields the base and the base yields all four
children; a link advertised as "P&L only" would have been a lie.

`financial_form_link_sheets` is which sheets one link opens — a child table
rather than a JSONB column so that "which live links open the personal
financial statement?" is a query, and so the four kinds are constrained by the
same CHECK vocabulary `ck_financial_form_links_kind` already uses.

The `kind` CHECK is dropped and recreated with `'worksheet'` added. The
downgrade restores the five-value list to four, which **fails loudly if a
worksheet link still exists** — the right outcome: silently deleting somebody's
live share links to make a downgrade succeed is worse than refusing.

Revision ID: 0204_financial_worksheets_and_link_scopes
Revises: 0203_business_statements_and_packets
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0204_financial_worksheets_and_link_scopes"
down_revision = "0203_business_statements_and_packets"
branch_labels = None
depends_on = None

_SHEET_KINDS = "'pfs','debt_schedule','p_and_l','balance_sheet'"


def upgrade() -> None:
    op.create_table(
        "financial_worksheets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("packet_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_financial_worksheets_profile_id", "financial_worksheets", ["profile_id"])
    op.create_index("ix_financial_worksheets_packet_id", "financial_worksheets", ["packet_id"])
    op.create_index(
        "ix_financial_worksheets_profile_id_created_at",
        "financial_worksheets",
        ["profile_id", "created_at"],
    )

    op.add_column(
        "financial_form_links",
        sa.Column("permission", sa.String(8), nullable=False, server_default="edit"),
    )
    op.create_check_constraint(
        "ck_financial_form_links_permission",
        "financial_form_links",
        "permission in ('edit','view')",
    )
    op.add_column(
        "financial_form_links",
        sa.Column(
            "worksheet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("financial_worksheets.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_financial_form_links_worksheet_id", "financial_form_links", ["worksheet_id"]
    )
    op.drop_constraint("ck_financial_form_links_kind", "financial_form_links", type_="check")
    op.create_check_constraint(
        "ck_financial_form_links_kind",
        "financial_form_links",
        f"kind in ({_SHEET_KINDS},'worksheet')",
    )

    op.create_table(
        "financial_form_link_sheets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "link_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("financial_form_links.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sheet_kind", sa.String(24), nullable=False),
        sa.UniqueConstraint("link_id", "sheet_kind", name="uq_financial_form_link_sheets_link_kind"),
        sa.CheckConstraint(
            f"sheet_kind in ({_SHEET_KINDS})", name="ck_financial_form_link_sheets_kind"
        ),
    )
    op.create_index(
        "ix_financial_form_link_sheets_link_id", "financial_form_link_sheets", ["link_id"]
    )

    for table in ("business_financial_statements", "financial_statements"):
        op.add_column(
            table, sa.Column("sheet_rev", sa.BigInteger(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    for table in ("financial_statements", "business_financial_statements"):
        op.drop_column(table, "sheet_rev")

    op.drop_index("ix_financial_form_link_sheets_link_id", table_name="financial_form_link_sheets")
    op.drop_table("financial_form_link_sheets")

    op.drop_constraint("ck_financial_form_links_kind", "financial_form_links", type_="check")
    # Deliberately narrower than what we just allowed: this raises if a
    # worksheet link still exists rather than quietly dropping somebody's live
    # share. Revoke and delete those rows first if you really mean it.
    op.create_check_constraint(
        "ck_financial_form_links_kind",
        "financial_form_links",
        f"kind in ({_SHEET_KINDS})",
    )
    op.drop_index("ix_financial_form_links_worksheet_id", table_name="financial_form_links")
    op.drop_column("financial_form_links", "worksheet_id")
    op.drop_constraint("ck_financial_form_links_permission", "financial_form_links", type_="check")
    op.drop_column("financial_form_links", "permission")

    op.drop_index(
        "ix_financial_worksheets_profile_id_created_at", table_name="financial_worksheets"
    )
    op.drop_index("ix_financial_worksheets_packet_id", table_name="financial_worksheets")
    op.drop_index("ix_financial_worksheets_profile_id", table_name="financial_worksheets")
    op.drop_table("financial_worksheets")
