"""dos_contract_templates + dos_contract_documents: step 5 as data

The lender documents a case executes are uploaded as templates, their fillable
fields discovered from the PDF itself, and mapped by the desk. Adding the next
contract becomes an upload, not a deploy.

Seeds the two slots the desk already named — the loan application and the
consulting agreement — inactive until their PDFs are uploaded, so the UI can
show the package shape before the paper arrives.

Revision ID: 0135_dos_contract_registry
Revises: 0134_dos_use_of_proceeds
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

revision = "0135_dos_contract_registry"
down_revision = "0134_dos_use_of_proceeds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dos_contract_templates",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("key", sa.String(48), nullable=False, unique=True),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("s3_key", sa.String(512)),
        sa.Column("page_count", sa.Integer()),
        sa.Column("has_acroform", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("field_names", JSONB()),
        sa.Column("field_map", JSONB()),
        sa.Column(
            "uploaded_by_user_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_table(
        "dos_contract_documents",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dealer_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("dos_dealers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("template_key", sa.String(48), nullable=False),
        sa.Column("template_revision", sa.Integer()),
        sa.Column("status", sa.String(24), nullable=False, server_default="draft"),
        sa.Column("field_values", JSONB()),
        sa.Column("filled_s3_key", sa.String(512)),
        sa.Column("filled_sha256", sa.String(64)),
        sa.Column("executed_s3_key", sa.String(512)),
        sa.Column("executed_sha256", sa.String(64)),
        sa.Column("esign_consent_at", sa.DateTime(timezone=True)),
        sa.Column("esign_consent_ip", sa.String(64)),
        sa.Column("signed_at", sa.DateTime(timezone=True)),
        sa.Column("signer_name", sa.String(160)),
        sa.Column("signer_ip", sa.String(64)),
        sa.Column("signer_user_agent", sa.String(400)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_dos_contract_docs_dealer", "dos_contract_documents", ["dealer_id"])
    op.create_unique_constraint(
        "uq_dos_contract_doc", "dos_contract_documents", ["dealer_id", "template_key"]
    )
    # The two slots the desk has already named. Inactive until their PDFs
    # arrive, so the step 5 UI shows the package before the paper does.
    op.execute(
        """
        INSERT INTO dos_contract_templates (id, key, title, active)
        VALUES
          (gen_random_uuid(), 'loan_app', 'Loan application', false),
          (gen_random_uuid(), 'consulting_agreement', 'Consulting agreement', false)
        """
    )


def downgrade() -> None:
    op.drop_table("dos_contract_documents")
    op.drop_table("dos_contract_templates")
