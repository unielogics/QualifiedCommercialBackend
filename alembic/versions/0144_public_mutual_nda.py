"""Public mutual NDA counterparties and protected signing sessions.

Revision ID: 0144_public_mutual_nda
Revises: 0143_dos_dealer_archive
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0144_public_mutual_nda"
down_revision = "0143_dos_dealer_archive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("contract_agreements", sa.Column("email_delivery_status", sa.String(length=32)))
    op.add_column("contract_agreements", sa.Column("email_delivery_message_id", sa.String(length=255)))
    op.add_column("contract_agreements", sa.Column("email_delivery_error", sa.Text()))

    op.create_table(
        "agreement_counterparties",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("legal_name", sa.String(length=255), nullable=False),
        sa.Column("normalized_legal_name", sa.String(length=255), nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=False),
        sa.Column("state_of_formation", sa.String(length=80), nullable=False),
        sa.Column("normalized_state_of_formation", sa.String(length=80), nullable=False),
        sa.Column("principal_business_address", sa.String(length=512), nullable=False),
        sa.Column("signer_email", sa.String(length=320), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "normalized_legal_name",
            "normalized_state_of_formation",
            name="uq_agreement_counterparty_name_state",
        ),
    )
    op.create_index(
        "ix_agreement_counterparties_normalized_legal_name",
        "agreement_counterparties",
        ["normalized_legal_name"],
    )
    op.create_index(
        "ix_agreement_counterparties_signer_email",
        "agreement_counterparties",
        ["signer_email"],
    )

    op.create_table(
        "public_contract_sign_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("contract_type", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("ip_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("user_agent", sa.String(length=512)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("token_hash", name="uq_public_contract_sign_sessions_token_hash"),
    )
    op.create_index(
        "ix_public_contract_sign_sessions_contract_type",
        "public_contract_sign_sessions",
        ["contract_type"],
    )
    op.create_index(
        "ix_public_contract_sign_sessions_token_hash",
        "public_contract_sign_sessions",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_public_contract_sign_sessions_ip_hash",
        "public_contract_sign_sessions",
        ["ip_hash"],
    )
    op.create_index(
        "ix_public_contract_sign_sessions_expires_at",
        "public_contract_sign_sessions",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_table("public_contract_sign_sessions")
    op.drop_table("agreement_counterparties")
    op.drop_column("contract_agreements", "email_delivery_error")
    op.drop_column("contract_agreements", "email_delivery_message_id")
    op.drop_column("contract_agreements", "email_delivery_status")
