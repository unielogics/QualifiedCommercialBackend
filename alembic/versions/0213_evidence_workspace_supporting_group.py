"""Add the permanent optional supporting-document group.

Revision ID: 0213_evidence_workspace_supporting_group
Revises: 0212_legacy_fit_rule_drafts
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from alembic import op

revision = "0213_evidence_workspace_supporting_group"
down_revision = "0212_legacy_fit_rule_drafts"
branch_labels = None
depends_on = None


SUPPORTING_KEY = "supporting_documents"


def upgrade() -> None:
    bind = op.get_bind()
    bucket_ids = [
        row[0]
        for row in bind.execute(
            sa.text(
                """
                SELECT DISTINCT primary_bucket_id
                FROM application_profiles
                WHERE primary_bucket_id IS NOT NULL
                """
            )
        ).fetchall()
    ]
    for bucket_id in bucket_ids:
        candidates = [
            row[0]
            for row in bind.execute(
                sa.text(
                    """
                    SELECT id
                    FROM bucket_requested_documents
                    WHERE bucket_id = :bucket_id
                      AND (
                        requirement_key = :requirement_key
                        OR (
                          requirement_key IS NULL
                          AND required = false
                          AND requires_signature = false
                          AND lower(trim(name)) IN ('supporting / other', 'supporting/other')
                        )
                      )
                    ORDER BY
                      CASE WHEN requirement_key = :requirement_key THEN 0 ELSE 1 END,
                      created_at ASC NULLS LAST,
                      id ASC
                    """
                ),
                {"bucket_id": bucket_id, "requirement_key": SUPPORTING_KEY},
            ).fetchall()
        ]
        keeper_id = candidates[0] if candidates else uuid.uuid4()
        for duplicate_id in candidates[1:]:
            bind.execute(
                sa.text(
                    """
                    UPDATE bucket_files
                    SET requested_document_id = :keeper_id
                    WHERE requested_document_id = :duplicate_id
                    """
                ),
                {"keeper_id": keeper_id, "duplicate_id": duplicate_id},
            )
            bind.execute(
                sa.text("DELETE FROM bucket_requested_documents WHERE id = :duplicate_id"),
                {"duplicate_id": duplicate_id},
            )

        if not candidates:
            bind.execute(
                sa.text(
                    """
                    INSERT INTO bucket_requested_documents (
                        id, bucket_id, name, category, description, required,
                        allow_multiple_files, status, is_custom, requires_signature,
                        requirement_key, requirement_source
                    ) VALUES (
                        :id, :bucket_id, 'Supporting / Other', 'Supporting documents',
                        'Optional supporting material that does not match a requested item. Files are still analyzed and may be reassigned later.',
                        false, true, 'requested', false, false,
                        :requirement_key,
                        CAST('{"kind":"system_supporting","client_visible":true}' AS jsonb)
                    )
                    """
                ),
                {
                    "id": keeper_id,
                    "bucket_id": bucket_id,
                    "requirement_key": SUPPORTING_KEY,
                },
            )
        else:
            bind.execute(
                sa.text(
                    """
                    UPDATE bucket_requested_documents
                    SET name = 'Supporting / Other',
                        category = 'Supporting documents',
                        description = 'Optional supporting material that does not match a requested item. Files are still analyzed and may be reassigned later.',
                        required = false,
                        allow_multiple_files = true,
                        status = 'requested',
                        is_custom = false,
                        requires_signature = false,
                        requirement_key = :requirement_key,
                        requirement_source = CAST('{"kind":"system_supporting","client_visible":true}' AS jsonb)
                    WHERE id = :keeper_id
                    """
                ),
                {"keeper_id": keeper_id, "requirement_key": SUPPORTING_KEY},
            )

    bind.execute(
        sa.text(
            """
            UPDATE bucket_requested_documents
            SET allow_multiple_files = true
            WHERE requires_signature = false
              AND requirement_source ->> 'kind' = 'program_readiness'
            """
        )
    )

    op.create_index(
        "uq_bucket_requested_documents_supporting_group",
        "bucket_requested_documents",
        ["bucket_id"],
        unique=True,
        postgresql_where=sa.text("requirement_key = 'supporting_documents'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_bucket_requested_documents_supporting_group",
        table_name="bucket_requested_documents",
    )
    op.execute(
        sa.text(
            """
            UPDATE bucket_requested_documents
            SET requirement_key = NULL,
                requirement_source = NULL
            WHERE requirement_key = 'supporting_documents'
              AND requirement_source ->> 'kind' = 'system_supporting'
            """
        )
    )
