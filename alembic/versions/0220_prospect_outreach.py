"""Add Dealer Prospect outreach, collateral, replies, and suppression.

Revision ID: 0220_prospect_outreach
Revises: 0219_dealer_prospect_pipeline
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0220_prospect_outreach"
down_revision = "0219_dealer_prospect_pipeline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dealer_prospect_email_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "prospect_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dealer_prospects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "approved_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("recipient_email", sa.String(320), nullable=False),
        sa.Column("from_email", sa.String(320), nullable=False),
        sa.Column("from_name", sa.String(160), nullable=False),
        sa.Column("reply_to_email", sa.String(320), nullable=False),
        sa.Column("reply_token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("unsubscribe_token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("rfc_message_id", sa.String(320), nullable=False, unique=True),
        sa.Column("subject", sa.String(240), nullable=False),
        sa.Column("editable_body", sa.Text(), nullable=False),
        sa.Column("locked_footer_text", sa.Text(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text()),
        sa.Column("ai_instructions", sa.Text()),
        sa.Column("purpose", sa.String(32), nullable=False, server_default="dealer_information"),
        sa.Column("draft_source", sa.String(16), nullable=False),
        sa.Column("model_id", sa.String(160)),
        sa.Column("catalog_version", sa.String(64), nullable=False),
        sa.Column(
            "catalog_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending_review"),
        sa.Column("auto_send_at", sa.DateTime(timezone=True)),
        sa.Column("review_stopped_at", sa.DateTime(timezone=True)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("dispatch_started_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("provider", sa.String(24)),
        sa.Column("provider_message_id", sa.String(320)),
        sa.Column("failure_code", sa.String(64)),
        sa.Column("failure_detail", sa.Text()),
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("attachment_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attachment_total_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delivery_mode", sa.String(24), nullable=False, server_default="attachments"),
        sa.Column("secure_bundle_token_hash", sa.String(64), unique=True),
        sa.Column("secure_bundle_expires_at", sa.DateTime(timezone=True)),
        sa.Column("secure_bundle_selected_at", sa.DateTime(timezone=True)),
        sa.Column(
            "secure_bundle_selected_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("secure_bundle_downloaded_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('pending_review','editing','sending','sent','cancelled','failed','blocked')",
            name="ck_dealer_prospect_email_draft_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_dealer_prospect_email_draft_version"),
        sa.CheckConstraint(
            "delivery_mode IN ('attachments','secure_link')",
            name="ck_dealer_prospect_email_draft_delivery_mode",
        ),
    )
    op.create_index(
        "ix_dealer_prospect_email_drafts_due",
        "dealer_prospect_email_drafts",
        ["status", "auto_send_at"],
    )
    op.create_index(
        "ix_dealer_prospect_email_drafts_prospect_created",
        "dealer_prospect_email_drafts",
        ["prospect_id", "created_at"],
    )
    op.create_index(
        "ix_dealer_prospect_email_drafts_provider_message_id",
        "dealer_prospect_email_drafts",
        ["provider_message_id"],
    )
    op.create_index(
        "ix_dealer_prospect_email_drafts_idempotency_key",
        "dealer_prospect_email_drafts",
        ["idempotency_key"],
        unique=True,
    )

    op.create_table(
        "marketing_collateral_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("assignment", sa.String(48), nullable=False, server_default="dealer_outreach"),
        sa.Column("logical_key", sa.String(120), nullable=False),
        sa.Column("name", sa.String(180), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending_approval"),
        sa.Column("file_name", sa.String(240), nullable=False),
        sa.Column("content_type", sa.String(80), nullable=False, server_default="application/pdf"),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("document_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("validation_status", sa.String(32), nullable=False),
        sa.Column("validation_detail", sa.Text()),
        sa.Column(
            "uploaded_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "approved_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column(
            "retired_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "assignment", "logical_key", "version", name="uq_marketing_collateral_version"
        ),
        sa.CheckConstraint(
            "status IN ('pending_approval','active','retired')",
            name="ck_marketing_collateral_status",
        ),
        sa.CheckConstraint("size_bytes > 0", name="ck_marketing_collateral_size"),
        sa.CheckConstraint("char_length(sha256) = 64", name="ck_marketing_collateral_sha"),
    )
    op.create_index(
        "ix_marketing_collateral_active_order",
        "marketing_collateral_assets",
        ["assignment", "status", "sort_order"],
    )
    op.create_index(
        "ix_marketing_collateral_assets_sha256",
        "marketing_collateral_assets",
        ["sha256"],
    )
    op.create_index(
        "uq_marketing_collateral_one_active_version",
        "marketing_collateral_assets",
        ["assignment", "logical_key"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "marketing_collateral_asset_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("marketing_collateral_assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column(
            "details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_marketing_collateral_events_asset_created",
        "marketing_collateral_asset_events",
        ["asset_id", "created_at"],
    )

    op.create_table(
        "dealer_prospect_email_draft_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "draft_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dealer_prospect_email_drafts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("marketing_collateral_assets.id", ondelete="SET NULL"),
        ),
        sa.Column("asset_name", sa.String(180), nullable=False),
        sa.Column("asset_version", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.String(240), nullable=False),
        sa.Column("content_type", sa.String(80), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("validation_status", sa.String(32), nullable=False),
        sa.Column("document_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("draft_id", "asset_id", name="uq_prospect_draft_asset"),
        sa.CheckConstraint("size_bytes > 0", name="ck_prospect_draft_asset_size"),
        sa.CheckConstraint("char_length(sha256) = 64", name="ck_prospect_draft_asset_sha"),
    )
    op.create_index(
        "ix_prospect_draft_assets_order",
        "dealer_prospect_email_draft_assets",
        ["draft_id", "sort_order"],
    )

    op.create_table(
        "email_suppressions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email_normalized", sa.String(320), nullable=False, unique=True),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("source", sa.String(48), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "revoked_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "reason IN ('unsubscribe','bounce','complaint','bad_address','administrative')",
            name="ck_email_suppression_reason",
        ),
    )
    op.create_index(
        "ix_email_suppressions_active", "email_suppressions", ["active", "email_normalized"]
    )

    op.create_table(
        "dealer_prospect_inbound_replies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "prospect_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dealer_prospects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "draft_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dealer_prospect_email_drafts.id", ondelete="SET NULL"),
        ),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("provider_message_id", sa.String(320), nullable=False),
        sa.Column("from_email", sa.String(320), nullable=False),
        sa.Column(
            "to_emails", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("subject", sa.String(998)),
        sa.Column("body_text_enc", sa.Text()),
        sa.Column("encryption_provider", sa.String(24), nullable=False, server_default="fernet"),
        sa.Column("in_reply_to", sa.String(500)),
        sa.Column(
            "references", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("received_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "provider", "provider_message_id", name="uq_prospect_reply_provider_id"
        ),
    )
    op.create_index(
        "ix_prospect_replies_prospect_received",
        "dealer_prospect_inbound_replies",
        ["prospect_id", "received_at"],
    )

    op.add_column("message_sends", sa.Column("from_email", sa.String(320)))
    op.add_column("message_sends", sa.Column("reply_to_email", sa.String(320)))
    op.add_column("message_sends", sa.Column("rfc_message_id", sa.String(320)))
    op.add_column("message_sends", sa.Column("prospect_id", postgresql.UUID(as_uuid=True)))
    op.add_column("message_sends", sa.Column("contact_id", postgresql.UUID(as_uuid=True)))
    op.add_column("message_sends", sa.Column("prospect_draft_id", postgresql.UUID(as_uuid=True)))
    op.create_foreign_key(
        "fk_message_sends_prospect_id",
        "message_sends",
        "dealer_prospects",
        ["prospect_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_message_sends_contact_id",
        "message_sends",
        "dos_rep_contacts",
        ["contact_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_message_sends_prospect_draft_id",
        "message_sends",
        "dealer_prospect_email_drafts",
        ["prospect_draft_id"],
        ["id"],
        ondelete="SET NULL",
    )
    for name in ("rfc_message_id", "prospect_id", "contact_id", "prospect_draft_id"):
        op.create_index(f"ix_message_sends_{name}", "message_sends", [name])


def downgrade() -> None:
    for name in ("prospect_draft_id", "contact_id", "prospect_id", "rfc_message_id"):
        op.drop_index(f"ix_message_sends_{name}", table_name="message_sends")
    op.drop_constraint("fk_message_sends_prospect_draft_id", "message_sends", type_="foreignkey")
    op.drop_constraint("fk_message_sends_contact_id", "message_sends", type_="foreignkey")
    op.drop_constraint("fk_message_sends_prospect_id", "message_sends", type_="foreignkey")
    for column in (
        "prospect_draft_id",
        "contact_id",
        "prospect_id",
        "rfc_message_id",
        "reply_to_email",
        "from_email",
    ):
        op.drop_column("message_sends", column)

    op.drop_index(
        "ix_prospect_replies_prospect_received", table_name="dealer_prospect_inbound_replies"
    )
    op.drop_table("dealer_prospect_inbound_replies")
    op.drop_index("ix_email_suppressions_active", table_name="email_suppressions")
    op.drop_table("email_suppressions")
    op.drop_index("ix_prospect_draft_assets_order", table_name="dealer_prospect_email_draft_assets")
    op.drop_table("dealer_prospect_email_draft_assets")
    op.drop_index(
        "ix_marketing_collateral_events_asset_created",
        table_name="marketing_collateral_asset_events",
    )
    op.drop_table("marketing_collateral_asset_events")
    op.drop_index(
        "uq_marketing_collateral_one_active_version",
        table_name="marketing_collateral_assets",
    )
    op.drop_index("ix_marketing_collateral_assets_sha256", table_name="marketing_collateral_assets")
    op.drop_index("ix_marketing_collateral_active_order", table_name="marketing_collateral_assets")
    op.drop_table("marketing_collateral_assets")
    op.drop_index(
        "ix_dealer_prospect_email_drafts_idempotency_key",
        table_name="dealer_prospect_email_drafts",
    )
    op.drop_index(
        "ix_dealer_prospect_email_drafts_provider_message_id",
        table_name="dealer_prospect_email_drafts",
    )
    op.drop_index(
        "ix_dealer_prospect_email_drafts_prospect_created",
        table_name="dealer_prospect_email_drafts",
    )
    op.drop_index("ix_dealer_prospect_email_drafts_due", table_name="dealer_prospect_email_drafts")
    op.drop_table("dealer_prospect_email_drafts")
