"""dos_dealers.audit_client_since — graduation is a flag, not a copy

A rep file and an audit file are the same row viewed from two apps. That is
the entire transfer story: Plaid items, credit pulls, documents and consent
are keyed to dealer_id and never move. This column only records that (and
when) the desk promoted the file into the audit book.

Revision ID: 0136_dos_audit_client
Revises: 0135_dos_contract_registry
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0136_dos_audit_client"
down_revision = "0135_dos_contract_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dos_dealers", sa.Column("audit_client_since", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("dos_dealers", "audit_client_since")
