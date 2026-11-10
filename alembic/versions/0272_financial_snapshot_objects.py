"""Repair only known JSON-null invalidation wrappers, retaining their contents.

Revision ID: 0272_financial_snapshot_objects
Revises: 0271_cp_investor_presentation
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0272_financial_snapshot_objects"
down_revision = "0271_cp_investor_presentation"
branch_labels = None
depends_on = None


def repair_sql(table, column, marker_keys):
    # These names are migration-owned constants, never request input. The
    # original defect produces [null, object, ...], not an arbitrary JSON list.
    markers = " OR ".join(f"part.value -> '{key}' = 'true'::jsonb" for key in marker_keys)
    wrapped = f"(CASE WHEN jsonb_typeof({column}) = 'array' THEN {column} ELSE '[]'::jsonb END)"
    return f"""
        WITH candidates AS (
            SELECT id, {wrapped} AS wrapped
            FROM {table}
            WHERE jsonb_typeof({column}) = 'array'
              AND jsonb_array_length({wrapped}) >= 2
              AND {wrapped} -> 0 = 'null'::jsonb
              AND NOT EXISTS (
                  SELECT 1 FROM jsonb_array_elements({wrapped}) WITH ORDINALITY AS part(value, position)
                  WHERE part.position > 1 AND jsonb_typeof(part.value) IS DISTINCT FROM 'object'
              )
              AND EXISTS (
                  SELECT 1 FROM jsonb_array_elements({wrapped}) AS part(value)
                  WHERE {markers}
              )
        ), recovered AS (
            SELECT candidates.id,
                   jsonb_object_agg(entry.key, entry.value ORDER BY part.position) AS body
            FROM candidates
            CROSS JOIN LATERAL jsonb_array_elements(candidates.wrapped) WITH ORDINALITY AS part(value, position)
            CROSS JOIN LATERAL jsonb_each(CASE WHEN jsonb_typeof(part.value) = 'object'
                                             THEN part.value ELSE '{{}}'::jsonb END) AS entry(key, value)
            GROUP BY candidates.id
        )
        UPDATE {table} AS target SET {column} = recovered.body
        FROM recovered WHERE target.id = recovered.id
    """


def upgrade():
    op.execute(sa.text(repair_sql("public_underwriting_intakes", "result_snapshot",
                                 ("_funding_request_stale", "_document_versions_stale"))))
    op.execute(sa.text(repair_sql("bucket_ai_reviews", "context_snapshot",
                                 ("funding_request_stale", "document_versions_stale"))))


def downgrade():
    # Object shape is the declared schema before and after this repair. Never
    # reintroduce corrupt arrays or manufacture an earlier AI snapshot.
    pass
