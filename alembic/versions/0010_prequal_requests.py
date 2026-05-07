"""Pre-qualification letter requests — async approval workflow

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-07

A borrower-submitted ask for a one-page pre-qualification letter PDF.
Goes pending → approved (PDF uploaded to S3 + status flipped) | rejected
(admin_notes set).

Submitting a request also spawns or attaches to a Loan record so the
operator pipeline picks the file up naturally — that join lives in
the application code, not at the DB level.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "prequal_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("loans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "requester_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Borrower-submitted fields
        sa.Column("target_property_address", sa.Text(), nullable=False),
        sa.Column("purchase_price", sa.Numeric(14, 2), nullable=False),
        sa.Column("requested_loan_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("loan_type", sa.String(16), nullable=False),  # "dscr" | "bridge"
        sa.Column("expected_closing_date", sa.Date(), nullable=True),
        sa.Column("borrower_notes", sa.Text(), nullable=True),
        # Admin-overridable
        sa.Column("approved_purchase_price", sa.Numeric(14, 2), nullable=True),
        sa.Column("approved_loan_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("admin_notes", sa.Text(), nullable=True),
        # State
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("pdf_s3_key", sa.String(512), nullable=True),
        sa.Column(
            "reviewed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_prequal_requests_status_closing",
        "prequal_requests",
        ["status", "expected_closing_date"],
    )
    op.create_index(
        "ix_prequal_requests_loan",
        "prequal_requests",
        ["loan_id"],
    )
    op.create_index(
        "ix_prequal_requests_requester_status",
        "prequal_requests",
        ["requester_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_prequal_requests_requester_status", table_name="prequal_requests")
    op.drop_index("ix_prequal_requests_loan", table_name="prequal_requests")
    op.drop_index("ix_prequal_requests_status_closing", table_name="prequal_requests")
    op.drop_table("prequal_requests")
