"""Allow every application requirement to retain multiple evidence files.

Revision ID: 0209_requirement_multi_evidence
Revises: 0208_ai_intake_program_readiness
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0209_requirement_multi_evidence"
down_revision = "0208_ai_intake_program_readiness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "application_requirement_evidence_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "requirement_state_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_requirement_states.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bucket_files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=24), nullable=False, server_default="automatic"),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "linked_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column(
            "verified_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "removed_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("reason", sa.Text()),
        sa.Column("provenance", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "source IN ('automatic','filename_suggestion','operator')",
            name="ck_application_requirement_evidence_source",
        ),
    )
    op.create_index(
        "uq_application_requirement_evidence_active",
        "application_requirement_evidence_files",
        ["requirement_state_id", "file_id"],
        unique=True,
        postgresql_where=sa.text("removed_at IS NULL"),
    )
    op.create_index(
        "ix_application_requirement_evidence_state",
        "application_requirement_evidence_files",
        ["requirement_state_id", "removed_at"],
    )

    # Preserve the single-file links and review decisions created by 0208.
    op.execute(
        """
        INSERT INTO application_requirement_evidence_files (
            id, requirement_state_id, file_id, source, linked_at,
            verified_at, verified_by_user_id, created_at, updated_at
        )
        SELECT
            gen_random_uuid(), ars.id, ars.evidence_file_id,
            CASE
                WHEN ars.provenance ->> 'source' = 'operator_link' THEN 'operator'
                ELSE 'automatic'
            END,
            COALESCE(ars.received_at, ars.created_at, now()),
            ars.verified_at, ars.verified_by_user_id, now(), now()
        FROM application_requirement_states ars
        WHERE ars.evidence_file_id IS NOT NULL
        """
    )

    # Every program criterion may be answered by a document set. This also
    # removes the upload-route rejection that previously affected split P&L /
    # balance-sheet packages and supplemental schedules.
    op.execute(
        """
        UPDATE bucket_requested_documents
        SET allow_multiple_files = true
        WHERE requirement_source ->> 'kind' = 'program_readiness'
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_application_requirement_evidence_state",
        table_name="application_requirement_evidence_files",
    )
    op.drop_index(
        "uq_application_requirement_evidence_active",
        table_name="application_requirement_evidence_files",
    )
    op.drop_table("application_requirement_evidence_files")
