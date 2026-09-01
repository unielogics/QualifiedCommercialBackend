"""Allow one signing envelope to contain forms for multiple programs.

Revision ID: 0174_contract_program_sets
Revises: 0173_underwriting_summary
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0174_contract_program_sets"
down_revision = "0173_underwriting_summary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dos_contract_envelopes",
        sa.Column(
            "program_keys",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE dos_contract_envelopes
            SET program_keys = jsonb_build_array(program_key)
            WHERE program_key IS NOT NULL
              AND program_keys = '[]'::jsonb
            """
        )
    )


def downgrade() -> None:
    op.drop_column("dos_contract_envelopes", "program_keys")
