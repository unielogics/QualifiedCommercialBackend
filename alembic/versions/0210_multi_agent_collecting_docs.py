"""Allow multiple agents and start new files in document collection.

Revision ID: 0210_multi_agent_collecting_docs
Revises: 0209_requirement_multi_evidence
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0210_multi_agent_collecting_docs"
down_revision = "0209_requirement_multi_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("uq_file_team_one_agent", table_name="file_team_members")
    op.alter_column(
        "application_profiles",
        "underwriting_status",
        existing_type=sa.String(length=32),
        nullable=False,
        server_default="collecting_docs",
    )
    op.alter_column(
        "loans",
        "stage",
        existing_type=sa.String(length=32),
        existing_nullable=True,
        server_default="collecting_docs",
    )


def downgrade() -> None:
    # The old schema can retain only one agent. Prefer the source-derived
    # primary agent, then the oldest manually assigned agent.
    op.execute(
        """
        DELETE FROM file_team_members AS member
        USING (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY profile_id
                       ORDER BY
                           CASE WHEN derived_from IS NOT NULL THEN 0 ELSE 1 END,
                           created_at ASC,
                           id ASC
                   ) AS position
            FROM file_team_members
            WHERE seat = 'agent'
        ) AS ranked
        WHERE member.id = ranked.id AND ranked.position > 1
        """
    )
    op.create_index(
        "uq_file_team_one_agent",
        "file_team_members",
        ["profile_id"],
        unique=True,
        postgresql_where=sa.text("seat = 'agent'"),
    )
    op.alter_column(
        "loans",
        "stage",
        existing_type=sa.String(length=32),
        existing_nullable=True,
        server_default="prequalified",
    )
    op.alter_column(
        "application_profiles",
        "underwriting_status",
        existing_type=sa.String(length=32),
        nullable=False,
        server_default="submitted",
    )
