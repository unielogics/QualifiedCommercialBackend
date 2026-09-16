"""Add immutable combined offer deliveries and client decisions.

Revision ID: 0218_combined_offer_deliveries
Revises: 0217_application_client_terms
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0218_combined_offer_deliveries"
down_revision = "0217_application_client_terms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "application_offer_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "room_link_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bucket_upload_links.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("room_token_hash", sa.String(length=64), nullable=False),
        sa.Column("access_passcode_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "email_thread_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dos_rep_inbox_threads.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "message_send_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("message_sends.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="sending"),
        sa.Column("to_contact_id", sa.String(length=80), nullable=False),
        sa.Column("cc_contact_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("recipient_emails", postgresql.JSONB(), nullable=False),
        sa.Column("cc_emails", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("personal_message", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("draft_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("provider", sa.String(length=24), nullable=True),
        sa.Column("provider_correlation_id", sa.String(length=320), nullable=False),
        sa.Column("provider_handoff_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_message_id", sa.String(length=320), nullable=True),
        sa.Column("provider_detail", sa.Text(), nullable=True),
        sa.Column(
            "sent_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effects_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("client_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "reconciled_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reconciliation_outcome", sa.String(length=24), nullable=True),
        sa.Column("reconciliation_attestation", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('sending','sent','partially_decided','completed','expired','superseded','failed')",
            name="ck_application_offer_delivery_status",
        ),
        sa.CheckConstraint(
            "reconciliation_outcome IS NULL OR reconciliation_outcome IN ('provider_accepted','confirmed_not_sent')",
            name="ck_application_offer_delivery_reconciliation_outcome",
        ),
    )
    op.create_index(
        "ix_application_offer_deliveries_idempotency_key",
        "application_offer_deliveries",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_application_offer_deliveries_provider_correlation_id",
        "application_offer_deliveries",
        ["provider_correlation_id"],
        unique=True,
    )
    op.create_index(
        "ix_application_offer_deliveries_room_token_hash",
        "application_offer_deliveries",
        ["room_token_hash"],
    )
    op.create_index(
        "ix_application_offer_deliveries_profile_sent",
        "application_offer_deliveries",
        ["profile_id", "sent_at"],
    )

    op.create_table(
        "application_offer_delivery_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "delivery_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_offer_deliveries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("item_key", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=True),
        sa.Column("label", sa.String(length=180), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("canonical_summary", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("file_name", sa.String(length=240), nullable=False),
        sa.Column("content_type", sa.String(length=160), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=700), nullable=True),
        sa.Column("document_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "decision_status", sa.String(length=24), nullable=False, server_default="pending"
        ),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("responded_name", sa.String(length=180), nullable=True),
        sa.Column("response_reason", sa.Text(), nullable=True),
        sa.Column("response_channel", sa.String(length=24), nullable=True),
        sa.Column("response_ip", sa.String(length=80), nullable=True),
        sa.Column("response_user_agent", sa.String(length=500), nullable=True),
        sa.Column(
            "response_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("response_attestation", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "kind IN ('merchant_offer','production_term_sheet','application_term_sheet','evidence_file')",
            name="ck_application_offer_delivery_item_kind",
        ),
        sa.CheckConstraint(
            "decision_status IN ('pending','accepted','declined','expired','superseded','not_applicable')",
            name="ck_application_offer_delivery_item_decision",
        ),
        sa.CheckConstraint(
            "char_length(sha256) = 64", name="ck_application_offer_delivery_item_sha"
        ),
        sa.UniqueConstraint(
            "delivery_id", "item_key", name="uq_application_offer_delivery_item_key"
        ),
    )
    op.create_index(
        "ix_application_offer_delivery_items_delivery_id",
        "application_offer_delivery_items",
        ["delivery_id"],
    )
    op.create_index(
        "ix_application_offer_delivery_items_source",
        "application_offer_delivery_items",
        ["kind", "source_id", "source_version"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_application_offer_delivery_items_source", table_name="application_offer_delivery_items"
    )
    op.drop_index(
        "ix_application_offer_delivery_items_delivery_id",
        table_name="application_offer_delivery_items",
    )
    op.drop_table("application_offer_delivery_items")
    op.drop_index(
        "ix_application_offer_deliveries_profile_sent", table_name="application_offer_deliveries"
    )
    op.drop_index(
        "ix_application_offer_deliveries_room_token_hash", table_name="application_offer_deliveries"
    )
    op.drop_index(
        "ix_application_offer_deliveries_provider_correlation_id",
        table_name="application_offer_deliveries",
    )
    op.drop_index(
        "ix_application_offer_deliveries_idempotency_key", table_name="application_offer_deliveries"
    )
    op.drop_table("application_offer_deliveries")
