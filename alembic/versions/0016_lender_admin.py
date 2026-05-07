"""Lender admin v2 — contact fields, products, domain, is_active + Loan.lender_id.

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-07

The Lender model shipped in an earlier phase as a 4-column stub
(id, name, submission_email, matrix). This migration grows it into
the shape the new super-admin Lenders tab + Connect-Lender flow
need:

  contact_name / contact_email / contact_phone / contact_title
    Primary point of contact. Distinct from submission_email which
    is typically a shared inbox.

  products  JSONB list of LoanType values
    Drives the Connect-Lender dropdown ("filter to lenders that
    actually fund this product").

  email_domain  Phase-2 hook for inbound matching when an unknown
    sender's domain matches the lender. Captured now, consumed
    later in app/services/email/orchestrator.py.

  notes  Operator scratchpad.

  is_active  Soft-delete switch. Hides a lender from new
    Connect-Lender dropdowns without orphaning historical
    LoanParticipant rows / Activity log entries.

Plus loans.lender_id (UUID FK, ON DELETE SET NULL) — single
authoritative "which lender is on this deal?" record. The
LoanParticipant row stays the trigger for the email machinery, but
the FK lets the loan UI render "Connected: Acme Capital" without
joining through participants.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "lenders",
        sa.Column("contact_name", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "lenders",
        sa.Column("contact_email", sa.String(length=320), nullable=True),
    )
    op.add_column(
        "lenders",
        sa.Column("contact_phone", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "lenders",
        sa.Column("contact_title", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "lenders",
        sa.Column(
            "products",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "lenders",
        sa.Column("email_domain", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "lenders",
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.add_column(
        "lenders",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )

    op.add_column(
        "loans",
        sa.Column("lender_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_loans_lender_id",
        "loans",
        "lenders",
        ["lender_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_loans_lender_id", "loans", ["lender_id"])


def downgrade() -> None:
    op.drop_index("ix_loans_lender_id", table_name="loans")
    op.drop_constraint("fk_loans_lender_id", "loans", type_="foreignkey")
    op.drop_column("loans", "lender_id")

    op.drop_column("lenders", "is_active")
    op.drop_column("lenders", "notes")
    op.drop_column("lenders", "email_domain")
    op.drop_column("lenders", "products")
    op.drop_column("lenders", "contact_title")
    op.drop_column("lenders", "contact_phone")
    op.drop_column("lenders", "contact_email")
    op.drop_column("lenders", "contact_name")
