"""dos: editable debt schedule

Debt schedules had no home. extract._route_debt_schedule applied a document's
total monthly payment straight onto period.debt_service and kept the line items
only inside doc_meta, so there was nothing a human could edit, and a dealer with
no uploaded debt schedule had debt_service NULL on every period — which is why
DSCR was null on a dealer carrying obvious card and floorplan obligations.

This table is the editable schedule. Rows are drafted from the vendor rollup
(services/vendors.draft_debt_rows) and then owned by the admin: origin records
where a row came from, and an admin edit is never overwritten by a re-draft —
the same precedence law as metric targets and account roles.

Revision ID: 0116_dos_debts
Revises: 0115_dos_account_dedupe
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0116_dos_debts"
down_revision = "0115_dos_account_dedupe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dos_debts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dealer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dos_dealers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("lender", sa.String(180), nullable=False),
        # floorplan | loan | credit_card | other — drives which obligations
        # count toward DSCR and how the lender package groups them.
        sa.Column("category", sa.String(24), nullable=False, server_default="loan"),
        sa.Column("monthly_payment", sa.Numeric(14, 2)),
        sa.Column("balance", sa.Numeric(14, 2)),
        sa.Column("rate", sa.Numeric(6, 3)),
        sa.Column("term_months", sa.Integer()),
        sa.Column("maturity_on", sa.Date()),
        # ai_draft | admin | document — an ai_draft row may be re-proposed;
        # once it is 'admin' the draft never touches it again.
        sa.Column("origin", sa.String(16), nullable=False, server_default="ai_draft"),
        # active | dismissed — dismissing a drafted row keeps it from coming
        # back on the next draft without deleting the evidence of it.
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        # Normalized vendor key this row was drafted from, so a re-draft can
        # recognize the same obligation instead of duplicating it.
        sa.Column("vendor_key", sa.String(60)),
        sa.Column("evidence", JSONB()),  # {observed_months, count, cadence, rationale}
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_dos_debts_dealer", "dos_debts", ["dealer_id"])
    # One row per drafted obligation per dealer: a re-draft updates rather than
    # duplicating. Partial because hand-added rows carry no vendor key.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_dos_debt_vendor
        ON dos_debts (dealer_id, vendor_key)
        WHERE vendor_key IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_dos_debt_vendor;")
    op.drop_index("ix_dos_debts_dealer", table_name="dos_debts")
    op.drop_table("dos_debts")
