"""Client.client_experience_mode + reason + locked_by.

Revision ID: 0026
Revises: 0025
Create Date: 2026-05-08

Three nullable VARCHAR columns on `clients`:

  client_experience_mode           — guided | self_directed | hybrid | NULL
  client_experience_mode_reason    — audit trail (set when mode changed)
  client_experience_mode_locked_by — agent | client | firm | NULL

NULL = "not yet decided" — the UI falls back to deriving from broker_id
(presence → guided, absence → self_directed).

The desktop has been PATCHing these names against /clients/{id} for
weeks; the backend silently ignored them. This migration makes the
PATCH actually persist.

Default behavior at insert time:
  - Dashboard-created clients (agent invites the borrower) → set to
    "guided" by the create-client path.
  - Self-signups (borrower lands on /signup directly) → leave NULL,
    which the UI treats as self_directed.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "clients",
        sa.Column("client_experience_mode", sa.String(16), nullable=True),
    )
    op.add_column(
        "clients",
        sa.Column("client_experience_mode_reason", sa.String(32), nullable=True),
    )
    op.add_column(
        "clients",
        sa.Column("client_experience_mode_locked_by", sa.String(16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("clients", "client_experience_mode_locked_by")
    op.drop_column("clients", "client_experience_mode_reason")
    op.drop_column("clients", "client_experience_mode")
