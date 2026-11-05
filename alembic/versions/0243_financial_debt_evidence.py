"""Stable duplicate identity and typed debt document associations."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0243_financial_debt_evidence"
down_revision = "0242_financial_workspace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("financial_form_links", sa.Column("financial_family_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_financial_form_link_family", "financial_form_links", "financial_form_families", ["financial_family_id"], ["id"], ondelete="CASCADE")
    op.create_index("ix_financial_form_links_financial_family_id", "financial_form_links", ["financial_family_id"])
    op.add_column("dos_debts", sa.Column("duplicate_of_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_dos_debts_duplicate_of", "dos_debts", "dos_debts", ["duplicate_of_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_dos_debts_duplicate_of_id", "dos_debts", ["duplicate_of_id"])
    op.create_table(
        "financial_debt_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("debt_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dos_debts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bucket_files.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("payoff_amount", sa.Numeric(14, 2)),
        sa.Column("payoff_date", sa.Date()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("debt_id", "file_id", "role", name="uq_financial_debt_evidence"),
        sa.CheckConstraint("role in ('agreement','payoff','supporting')", name="ck_financial_debt_evidence_role"),
        sa.CheckConstraint("not is_current or role = 'payoff'", name="ck_financial_debt_evidence_current_role"),
    )
    for column in ("profile_id", "debt_id", "file_id"):
        op.create_index(f"ix_financial_debt_evidence_{column}", "financial_debt_evidence", [column])
    op.create_index("uq_financial_debt_current_payoff", "financial_debt_evidence", ["debt_id"], unique=True, postgresql_where=sa.text("is_current = true"))


def downgrade() -> None:
    op.drop_index("ix_financial_form_links_financial_family_id", table_name="financial_form_links")
    op.drop_constraint("fk_financial_form_link_family", "financial_form_links", type_="foreignkey")
    op.drop_column("financial_form_links", "financial_family_id")
    op.drop_table("financial_debt_evidence")
    op.drop_index("ix_dos_debts_duplicate_of_id", table_name="dos_debts")
    op.drop_constraint("fk_dos_debts_duplicate_of", "dos_debts", type_="foreignkey")
    op.drop_column("dos_debts", "duplicate_of_id")
