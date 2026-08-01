"""Broker NDA / non-solicitation e-signature capture.

Adds broker_nda_acceptances: one row per dealer-partner ("broker") signing
the platform's non-disclosure / non-solicitation agreement, with the same
evidentiary shape as bucket_document_signatures (typed name, canvas
signature, rendered certificate, document hash/version, server-captured IP/
user-agent, signed_at) plus a free-text prior-relationships disclosure field
the broker fills in themselves.

Also adds users.nda_signed_at, denormalized from the latest acceptance row so
the AppShell hard-gate and every broker-router endpoint's guard is a cheap
single-column read.

Purely additive; no existing columns touched.

Revision ID: 0101_broker_nda_acceptance
Revises: 0100_intake_preferred_language
Create Date: 2026-07-31
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0101_broker_nda_acceptance"
down_revision = "0100_intake_preferred_language"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("nda_signed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "broker_nda_acceptances",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("document_version", sa.String(32), nullable=False),
        sa.Column("document_hash", sa.String(128), nullable=False),
        sa.Column("typed_name", sa.String(160), nullable=False),
        sa.Column("esign_consent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("signature_s3_key", sa.String(512), nullable=True),
        sa.Column("signature_hash", sa.String(128), nullable=True),
        sa.Column("certificate_s3_key", sa.String(512), nullable=True),
        sa.Column("certificate_hash", sa.String(128), nullable=True),
        sa.Column("prior_relationships_disclosure", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_broker_nda_acceptances_user_id", "broker_nda_acceptances", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_broker_nda_acceptances_user_id", table_name="broker_nda_acceptances")
    op.drop_table("broker_nda_acceptances")
    op.drop_column("users", "nda_signed_at")
