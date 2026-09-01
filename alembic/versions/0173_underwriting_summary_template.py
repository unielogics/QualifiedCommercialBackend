"""Seed the persistent QC underwriting-summary renderer.

Revision ID: 0173_underwriting_summary
Revises: 0172_reminder_sms_messages
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0173_underwriting_summary"
down_revision = "0172_reminder_sms_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO dos_contract_templates
              (id, key, title, s3_key, page_count, has_acroform, field_names,
               field_map, revision, active, render_kind, created_at, updated_at)
            VALUES
              (gen_random_uuid(), 'qc_underwriting_summary',
               'Qualified Commercial Underwriting Summary',
               NULL, NULL, false, '[]'::jsonb, '{}'::jsonb, 1, true,
               'generated_html', now(), now())
            ON CONFLICT (key) DO UPDATE
            SET title = EXCLUDED.title,
                render_kind = EXCLUDED.render_kind,
                active = true,
                updated_at = now()
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DELETE FROM dos_contract_templates
            WHERE key = 'qc_underwriting_summary'
              AND NOT EXISTS (
                SELECT 1 FROM dos_contract_documents
                WHERE template_key = 'qc_underwriting_summary'
              )
            """
        )
    )
