"""Agent AI knowledge documents — PDFs / FAQ text stored per agent.

Revision ID: 0041
Revises: 0040
Create Date: 2026-05-11

Backs the new "Knowledge & Voice" section on /agent-settings/ai. Each
agent uploads PDFs (offer letters, talking points, product sheets) that
the AI assembly layer concatenates into the system prompt at chat time.

This is intentionally a single table — no embeddings, no chunk table,
no separate parse-status table. The agent population is small (single
digits of MB per agent), so we just store the extracted plaintext on
the row and inject it whole (truncated) into the prompt. RAG/embeddings
can come later without breaking this schema.

Columns:
    id              UUID PK
    agent_user_id   FK users.id, ON DELETE CASCADE — the broker that owns it
    filename        original filename for display
    content_type    MIME for the upload (application/pdf, text/plain, ...)
    size_bytes      raw byte size at upload time
    s3_key          full S3 object key (bucket from app config)
    parsed_text     extracted text body (TEXT, nullable while parsing)
    status          'uploading' | 'parsing' | 'ready' | 'failed'
    error           free-text on parse failure
    deleted_at      soft-delete timestamp
    created_at / updated_at

Indexes:
    (agent_user_id) — every read scopes by agent.
    (agent_user_id, status) — load_agent_knowledge() filters ready
        rows for a given agent.

No backfill — empty table on first deploy."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_knowledge_documents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "agent_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(80), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("s3_key", sa.String(512), nullable=False),
        sa.Column("parsed_text", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="uploading",
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_ai_knowledge_documents_agent_user_id",
        "ai_knowledge_documents",
        ["agent_user_id"],
    )
    op.create_index(
        "ix_ai_knowledge_documents_agent_status",
        "ai_knowledge_documents",
        ["agent_user_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_knowledge_documents_agent_status",
        table_name="ai_knowledge_documents",
    )
    op.drop_index(
        "ix_ai_knowledge_documents_agent_user_id",
        table_name="ai_knowledge_documents",
    )
    op.drop_table("ai_knowledge_documents")
