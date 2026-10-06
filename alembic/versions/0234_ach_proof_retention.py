"""ACH proof retention and exact debit notices.

Revision ID: 0234_ach_proof_retention
Revises: 0233_origination_fee_payments
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0234_ach_proof_retention"
down_revision = "0233_origination_fee_payments"
branch_labels = None
depends_on = None


UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    # Bucket artifacts retain their familiar preview/download behavior while
    # gaining a durable compliance hold that deletion paths can enforce.
    op.add_column("bucket_files", sa.Column("retention_class", sa.String(32), nullable=True))
    op.add_column("bucket_files", sa.Column("protected_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "bucket_files",
        sa.Column("legal_hold", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("bucket_files", sa.Column("source_entity_type", sa.String(48), nullable=True))
    op.add_column("bucket_files", sa.Column("source_entity_id", UUID, nullable=True))
    op.add_column("bucket_files", sa.Column("source_immutable_ref", sa.String(160), nullable=True))
    op.add_column("bucket_files", sa.Column("s3_version_id", sa.String(256), nullable=True))
    op.create_index(
        "ix_bucket_files_retention",
        "bucket_files",
        ["retention_class", "protected_until"],
    )
    op.create_index(
        "ix_bucket_files_source_entity",
        "bucket_files",
        ["source_entity_type", "source_entity_id"],
    )

    # Every fee component carries its own governing agreement and earning
    # milestone instead of inheriting an ambiguous obligation-level document.
    op.add_column(
        "fee_obligation_lines",
        sa.Column("governing_agreement_document_id", UUID, nullable=True),
    )
    op.add_column(
        "fee_obligation_lines",
        sa.Column("governing_agreement_sha256", sa.String(64), nullable=True),
    )
    op.add_column(
        "fee_obligation_lines",
        sa.Column("agreement_component_scope", sa.String(32), nullable=True),
    )
    op.add_column(
        "fee_obligation_lines",
        sa.Column("earning_milestone", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "fk_fee_obligation_lines_governing_agreement_document",
        "fee_obligation_lines",
        "bucket_files",
        ["governing_agreement_document_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_fee_obligation_lines_governing_agreement_document_id",
        "fee_obligation_lines",
        ["governing_agreement_document_id"],
    )
    op.create_check_constraint(
        "ck_fee_obligation_lines_governing_agreement_complete",
        "fee_obligation_lines",
        "(governing_agreement_document_id IS NULL "
        "AND governing_agreement_sha256 IS NULL "
        "AND agreement_component_scope IS NULL) OR "
        "(governing_agreement_document_id IS NOT NULL "
        "AND governing_agreement_sha256 IS NOT NULL "
        "AND agreement_component_scope IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_fee_obligation_lines_agreement_scope",
        "fee_obligation_lines",
        "agreement_component_scope IS NULL OR "
        "(agreement_component_scope IN ('origination', 'consulting') "
        "AND agreement_component_scope = line_type)",
    )

    # New fields are nullable so mandates created before this rollout remain
    # readable. New service flows populate the complete disclosure snapshot.
    op.add_column("ach_mandates", sa.Column("authorization_type", sa.String(32), nullable=True))
    op.add_column("ach_mandates", sa.Column("authorization_text_snapshot", sa.Text(), nullable=True))
    op.add_column("ach_mandates", sa.Column("authorization_text_sha256", sa.String(64), nullable=True))
    op.add_column("ach_mandates", sa.Column("agreement_document_id", UUID, nullable=True))
    op.add_column("ach_mandates", sa.Column("agreement_sha256", sa.String(64), nullable=True))
    op.add_column("ach_mandates", sa.Column("certificate_bucket_file_id", UUID, nullable=True))
    op.add_column("ach_mandates", sa.Column("scheduled_debit_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ach_mandates", sa.Column("debit_window_start_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ach_mandates", sa.Column("debit_window_end_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ach_mandates", sa.Column("notice_business_days", sa.Integer(), nullable=True))
    op.add_column("ach_mandates", sa.Column("revocation_method", sa.Text(), nullable=True))
    op.add_column("ach_mandates", sa.Column("revocation_cutoff_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ach_mandates", sa.Column("signer_session_id", sa.String(128), nullable=True))
    op.add_column("ach_mandates", sa.Column("proof_copy_delivery_status", sa.String(32), nullable=True))
    op.add_column("ach_mandates", sa.Column("proof_copy_message_send_id", UUID, nullable=True))
    op.add_column("ach_mandates", sa.Column("proof_copy_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ach_mandates", sa.Column("proof_copy_delivered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ach_mandates", sa.Column("proof_copy_last_error", sa.Text(), nullable=True))
    op.add_column("ach_mandates", sa.Column("terminated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ach_mandates", sa.Column("termination_reason", sa.String(240), nullable=True))
    op.add_column("ach_mandates", sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_ach_mandates_agreement_document",
        "ach_mandates",
        "bucket_files",
        ["agreement_document_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_ach_mandates_certificate_bucket_file",
        "ach_mandates",
        "bucket_files",
        ["certificate_bucket_file_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_ach_mandates_proof_copy_message_send",
        "ach_mandates",
        "message_sends",
        ["proof_copy_message_send_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_ach_mandates_notice_days",
        "ach_mandates",
        "notice_business_days IS NULL OR notice_business_days >= 0",
    )
    op.create_check_constraint(
        "ck_ach_mandates_debit_window",
        "ach_mandates",
        "debit_window_start_at IS NULL OR debit_window_end_at IS NULL "
        "OR debit_window_end_at >= debit_window_start_at",
    )
    op.create_check_constraint(
        "ck_ach_mandates_agreement_complete",
        "ach_mandates",
        "(agreement_document_id IS NULL AND agreement_sha256 IS NULL) OR "
        "(agreement_document_id IS NOT NULL AND agreement_sha256 IS NOT NULL)",
    )
    op.create_index(
        "ix_ach_mandates_funding_source_id",
        "ach_mandates",
        ["funding_source_id"],
    )
    op.create_index(
        "ix_ach_mandates_agreement_document_id",
        "ach_mandates",
        ["agreement_document_id"],
    )
    op.create_index(
        "ix_ach_mandates_certificate_bucket_file_id",
        "ach_mandates",
        ["certificate_bucket_file_id"],
    )
    op.create_index(
        "ix_ach_mandates_proof_copy_message_send_id",
        "ach_mandates",
        ["proof_copy_message_send_id"],
    )
    op.create_index("ix_ach_mandates_retention_until", "ach_mandates", ["retention_until"])

    # Recover legacy mandate certificates into the protected Bucket ledger.
    # The object was written by the pre-retention flow, so its S3 VersionId is
    # unknowable here; all new writes capture it.  Rows with a historical hash
    # retain that digest and rows without one fail closed under a stable legacy
    # immutable reference rather than being left outside the retention system.
    op.execute(
        sa.text(
            """
            INSERT INTO bucket_files (
                id, created_at, updated_at, bucket_id, file_name, s3_key,
                s3_version_id, content_type, size_bytes, uploaded_by_name,
                uploaded_by_email, status, source_kind, source_detail,
                retention_class, protected_until, legal_hold,
                source_entity_type, source_entity_id, source_immutable_ref,
                content_hash
            )
            SELECT
                gen_random_uuid(), now(), now(), ap.primary_bucket_id,
                'QC One-Time ACH Authorization Proof.pdf',
                m.certificate_s3_key, NULL, 'application/pdf', 0,
                m.payer_name, m.payer_email, 'uploaded', 'generated',
                'ach_authorization_proof', 'ach_authorization_proof',
                GREATEST(
                    COALESCE(m.retention_until, m.signed_at + interval '2 years'),
                    GREATEST(
                        m.signed_at,
                        COALESCE(m.revoked_at, m.signed_at),
                        COALESCE(m.terminated_at, m.signed_at),
                        COALESCE((
                            SELECT max(COALESCE(e.event_timestamp, e.created_at))
                            FROM payment_transfer_events e
                            JOIN payment_transfers t ON t.id = e.transfer_id
                            WHERE t.mandate_id = m.id
                        ), m.signed_at)
                    ) + interval '2 years'
                ),
                false, 'ach_mandate', m.id,
                CASE
                    WHEN m.certificate_sha256 ~ '^[0-9A-Fa-f]{64}$'
                        THEN lower(m.certificate_sha256)
                    ELSE 'legacy:' || m.id::text
                END,
                CASE
                    WHEN m.certificate_sha256 ~ '^[0-9A-Fa-f]{64}$'
                        THEN lower(m.certificate_sha256)
                    ELSE NULL
                END
            FROM ach_mandates m
            JOIN application_profiles ap ON ap.id = m.application_profile_id
            WHERE m.certificate_s3_key IS NOT NULL
              AND ap.primary_bucket_id IS NOT NULL
              AND m.certificate_bucket_file_id IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE ach_mandates m
            SET certificate_bucket_file_id = bf.id,
                retention_until = GREATEST(
                    COALESCE(m.retention_until, m.signed_at + interval '2 years'),
                    bf.protected_until
                )
            FROM bucket_files bf
            WHERE bf.source_entity_type = 'ach_mandate'
              AND bf.source_entity_id = m.id
              AND bf.retention_class = 'ach_authorization_proof'
              AND m.certificate_bucket_file_id IS NULL
            """
        )
    )
    op.create_index(
        "uq_bucket_files_protected_source_ref",
        "bucket_files",
        [
            "source_entity_type",
            "source_entity_id",
            "retention_class",
            "source_immutable_ref",
        ],
        unique=True,
        postgresql_where=sa.text(
            "source_entity_type IS NOT NULL "
            "AND source_entity_id IS NOT NULL "
            "AND retention_class IS NOT NULL "
            "AND source_immutable_ref IS NOT NULL"
        ),
    )

    op.create_table(
        "payment_debit_notices",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "application_profile_id",
            UUID,
            sa.ForeignKey("application_profiles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "fee_obligation_id",
            UUID,
            sa.ForeignKey("fee_obligations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("mandate_id", UUID, sa.ForeignKey("ach_mandates.id", ondelete="SET NULL"), nullable=True),
        sa.Column("transfer_id", UUID, sa.ForeignKey("payment_transfers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("installment_id", UUID, sa.ForeignKey("payment_installments.id", ondelete="SET NULL"), nullable=True),
        sa.Column("message_send_id", UUID, sa.ForeignKey("message_sends.id", ondelete="SET NULL"), nullable=True),
        sa.Column(
            "notice_bucket_file_id",
            UUID,
            sa.ForeignKey("bucket_files.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("status", sa.String(24), server_default="draft", nullable=False),
        sa.Column("delivery_status", sa.String(32), nullable=True),
        sa.Column("notice_type", sa.String(32), server_default="one_time_fee", nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), server_default="usd", nullable=False),
        sa.Column("recipient_name", sa.String(180), nullable=True),
        sa.Column("recipient_email", sa.String(320), nullable=False),
        sa.Column("account_mask", sa.String(8), nullable=True),
        sa.Column("scheduled_debit_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("debit_window_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("debit_window_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notice_business_days", sa.Integer(), nullable=True),
        sa.Column("revocation_cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authorization_text_sha256", sa.String(64), nullable=True),
        sa.Column("notice_snapshot", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("notice_sha256", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(24), nullable=True),
        sa.Column("provider_message_id", sa.String(320), nullable=True),
        sa.Column("rfc_message_id", sa.String(320), nullable=True),
        sa.Column(
            "delivery_evidence_snapshot",
            JSONB,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bounced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_payment_debit_notices_idempotency"),
        sa.CheckConstraint("amount_cents > 0", name="ck_payment_debit_notices_amount"),
        sa.CheckConstraint(
            "notice_business_days IS NULL OR notice_business_days >= 0",
            name="ck_payment_debit_notices_notice_days",
        ),
        sa.CheckConstraint(
            "debit_window_start_at IS NULL OR debit_window_end_at IS NULL "
            "OR debit_window_end_at >= debit_window_start_at",
            name="ck_payment_debit_notices_window",
        ),
        sa.CheckConstraint(
            "notice_type != 'one_time_fee' OR "
            "(notice_business_days IS NOT NULL "
            "AND debit_window_start_at IS NOT NULL "
            "AND debit_window_end_at IS NOT NULL)",
            name="ck_payment_debit_notices_one_time_fee_window_required",
        ),
        sa.CheckConstraint(
            "notice_type != 'one_time_fee' OR "
            "(revocation_cutoff_at <= scheduled_debit_at "
            "AND debit_window_start_at <= scheduled_debit_at "
            "AND scheduled_debit_at <= debit_window_end_at)",
            name="ck_payment_debit_notices_one_time_fee_timing",
        ),
    )
    op.create_index(
        "ix_payment_debit_notices_application_profile_id",
        "payment_debit_notices",
        ["application_profile_id"],
    )
    op.create_index(
        "ix_payment_debit_notices_fee_obligation_id",
        "payment_debit_notices",
        ["fee_obligation_id"],
    )
    op.create_index("ix_payment_debit_notices_mandate_id", "payment_debit_notices", ["mandate_id"])
    op.create_index("ix_payment_debit_notices_transfer_id", "payment_debit_notices", ["transfer_id"])
    op.create_index("ix_payment_debit_notices_installment_id", "payment_debit_notices", ["installment_id"])
    op.create_index("ix_payment_debit_notices_message_send_id", "payment_debit_notices", ["message_send_id"])
    op.create_index(
        "ix_payment_debit_notices_notice_bucket_file_id",
        "payment_debit_notices",
        ["notice_bucket_file_id"],
    )
    op.create_index(
        "ix_payment_debit_notices_provider_message_id",
        "payment_debit_notices",
        ["provider_message_id"],
    )
    op.create_index(
        "ix_payment_debit_notices_rfc_message_id",
        "payment_debit_notices",
        ["rfc_message_id"],
    )
    op.create_index(
        "ix_payment_debit_notices_created_by_user_id",
        "payment_debit_notices",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_payment_debit_notices_superseded_by_user_id",
        "payment_debit_notices",
        ["superseded_by_user_id"],
    )
    op.create_index(
        "ix_payment_debit_notices_revoked_by_user_id",
        "payment_debit_notices",
        ["revoked_by_user_id"],
    )
    op.create_index("ix_payment_debit_notices_digest", "payment_debit_notices", ["notice_sha256"])
    op.create_index(
        "ix_payment_debit_notices_due",
        "payment_debit_notices",
        ["status", "scheduled_debit_at"],
    )
    op.create_index(
        "ix_payment_debit_notices_delivery",
        "payment_debit_notices",
        ["delivery_status", "created_at"],
    )
    op.create_index(
        "uq_payment_debit_notices_current_obligation",
        "payment_debit_notices",
        ["fee_obligation_id"],
        unique=True,
        postgresql_where=sa.text(
            "superseded_at IS NULL AND revoked_at IS NULL "
            "AND status NOT IN ('cancelled', 'consumed')"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_payment_debit_notices_current_obligation", table_name="payment_debit_notices")
    op.drop_index("ix_payment_debit_notices_delivery", table_name="payment_debit_notices")
    op.drop_index("ix_payment_debit_notices_due", table_name="payment_debit_notices")
    op.drop_index("ix_payment_debit_notices_digest", table_name="payment_debit_notices")
    op.drop_index("ix_payment_debit_notices_revoked_by_user_id", table_name="payment_debit_notices")
    op.drop_index("ix_payment_debit_notices_superseded_by_user_id", table_name="payment_debit_notices")
    op.drop_index("ix_payment_debit_notices_created_by_user_id", table_name="payment_debit_notices")
    op.drop_index("ix_payment_debit_notices_rfc_message_id", table_name="payment_debit_notices")
    op.drop_index("ix_payment_debit_notices_provider_message_id", table_name="payment_debit_notices")
    op.drop_index("ix_payment_debit_notices_notice_bucket_file_id", table_name="payment_debit_notices")
    op.drop_index("ix_payment_debit_notices_message_send_id", table_name="payment_debit_notices")
    op.drop_index("ix_payment_debit_notices_installment_id", table_name="payment_debit_notices")
    op.drop_index("ix_payment_debit_notices_transfer_id", table_name="payment_debit_notices")
    op.drop_index("ix_payment_debit_notices_mandate_id", table_name="payment_debit_notices")
    op.drop_index("ix_payment_debit_notices_fee_obligation_id", table_name="payment_debit_notices")
    op.drop_index("ix_payment_debit_notices_application_profile_id", table_name="payment_debit_notices")
    op.drop_table("payment_debit_notices")

    op.drop_index("ix_ach_mandates_retention_until", table_name="ach_mandates")
    op.drop_index("ix_ach_mandates_proof_copy_message_send_id", table_name="ach_mandates")
    op.drop_index("ix_ach_mandates_certificate_bucket_file_id", table_name="ach_mandates")
    op.drop_index("ix_ach_mandates_agreement_document_id", table_name="ach_mandates")
    op.drop_index("ix_ach_mandates_funding_source_id", table_name="ach_mandates")
    op.drop_constraint("ck_ach_mandates_agreement_complete", "ach_mandates", type_="check")
    op.drop_constraint("ck_ach_mandates_debit_window", "ach_mandates", type_="check")
    op.drop_constraint("ck_ach_mandates_notice_days", "ach_mandates", type_="check")
    op.drop_constraint("fk_ach_mandates_certificate_bucket_file", "ach_mandates", type_="foreignkey")
    op.drop_constraint("fk_ach_mandates_proof_copy_message_send", "ach_mandates", type_="foreignkey")
    op.drop_constraint("fk_ach_mandates_agreement_document", "ach_mandates", type_="foreignkey")
    for column in (
        "retention_until",
        "termination_reason",
        "terminated_at",
        "proof_copy_last_error",
        "proof_copy_delivered_at",
        "proof_copy_sent_at",
        "proof_copy_message_send_id",
        "proof_copy_delivery_status",
        "signer_session_id",
        "revocation_cutoff_at",
        "revocation_method",
        "notice_business_days",
        "debit_window_end_at",
        "debit_window_start_at",
        "scheduled_debit_at",
        "certificate_bucket_file_id",
        "agreement_sha256",
        "agreement_document_id",
        "authorization_text_sha256",
        "authorization_text_snapshot",
        "authorization_type",
    ):
        op.drop_column("ach_mandates", column)

    op.drop_constraint(
        "fk_fee_obligation_lines_governing_agreement_document",
        "fee_obligation_lines",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_fee_obligation_lines_agreement_scope",
        "fee_obligation_lines",
        type_="check",
    )
    op.drop_constraint(
        "ck_fee_obligation_lines_governing_agreement_complete",
        "fee_obligation_lines",
        type_="check",
    )
    op.drop_index(
        "ix_fee_obligation_lines_governing_agreement_document_id",
        table_name="fee_obligation_lines",
    )
    for column in (
        "earning_milestone",
        "agreement_component_scope",
        "governing_agreement_sha256",
        "governing_agreement_document_id",
    ):
        op.drop_column("fee_obligation_lines", column)

    op.drop_index("uq_bucket_files_protected_source_ref", table_name="bucket_files")
    op.drop_index("ix_bucket_files_source_entity", table_name="bucket_files")
    op.drop_index("ix_bucket_files_retention", table_name="bucket_files")
    for column in (
        "source_immutable_ref",
        "source_entity_id",
        "source_entity_type",
        "legal_hold",
        "protected_until",
        "retention_class",
        "s3_version_id",
    ):
        op.drop_column("bucket_files", column)
