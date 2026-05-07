"""Doc categorization + AI vision scan + per-loan chat threads.

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-07

Two interlocking surfaces land here:

(1) Documents grow a real link to checklist items + columns to
    drive the AI vision scanner.

      checklist_key (String 120, nullable)
        matches DocChecklistItem.name for the loan's product.
        Natural key. Set on kickoff_loan rows + borrower uploads
        that pick a checklist item.

      is_other (Boolean, default false)
        True when the borrower deliberately picked
        "Other / not in checklist" on upload.

      ai_notes (Text, nullable)
        Vision scan output: extracted facts, named borrowers,
        dates, amounts, red flags.

      ai_scan_status (String 24, default 'unscanned')
        unscanned | queued | scanning | scanned | failed.
        Distinct from `status` (workflow state).

      ai_scan_confidence (Numeric(3,2), nullable)
        0.00–1.00. Drives auto-verify gating (>= 0.85).

      scan_dirty (Boolean, default false)
        Flipped True on upload-complete; the scheduler's
        job_document_scan_drain picks up scan_dirty=True rows.

    Backfill: for every existing REQUESTED row whose loan has a
    matching checklist entry by name, set checklist_key=name. We
    don't try to backfill uploaded rows — they predate the
    feature and operators can re-categorize manually if it
    matters.

(2) ai_chat_threads.loan_id (UUID nullable FK → loans). Lets the
    same threading infra cover account-wide threads (loan_id NULL)
    and per-loan threads. Partial unique index on
    (user_id, loan_id) WHERE loan_id IS NOT NULL keeps the
    (user, loan) thread canonical; account threads stay
    unconstrained.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── documents: checklist linkage + scan state ─────────────────
    op.add_column(
        "documents",
        sa.Column("checklist_key", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column(
            "is_other",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "documents",
        sa.Column("ai_notes", sa.Text(), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column(
            "ai_scan_status",
            sa.String(length=24),
            nullable=False,
            server_default="unscanned",
        ),
    )
    op.add_column(
        "documents",
        sa.Column("ai_scan_confidence", sa.Numeric(3, 2), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column(
            "scan_dirty",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # Backfill checklist_key on existing REQUESTED rows where the
    # name matches a known checklist item. We seed checklist_key
    # from the document's name for every requested row — kickoff
    # creates docs with name=checklist_item.name verbatim, so this
    # is a safe identity copy. Uploaded rows are deliberately
    # skipped (operators can recategorize).
    op.execute(
        """
        UPDATE documents
        SET checklist_key = name
        WHERE status = 'requested' AND checklist_key IS NULL
        """
    )

    # ── ai_chat_threads.loan_id ──────────────────────────────────
    op.add_column(
        "ai_chat_threads",
        sa.Column("loan_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_ai_chat_threads_loan_id",
        "ai_chat_threads",
        "loans",
        ["loan_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_ai_chat_threads_loan_id",
        "ai_chat_threads",
        ["loan_id"],
    )
    # Partial unique — one canonical thread per (user, loan) pair.
    # Account threads (loan_id NULL) aren't constrained by this idx
    # since postgres ignores NULL in partial unique with WHERE
    # IS NOT NULL.
    op.create_index(
        "ix_ai_chat_threads_user_loan_uniq",
        "ai_chat_threads",
        ["user_id", "loan_id"],
        unique=True,
        postgresql_where=sa.text("loan_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_ai_chat_threads_user_loan_uniq", table_name="ai_chat_threads")
    op.drop_index("ix_ai_chat_threads_loan_id", table_name="ai_chat_threads")
    op.drop_constraint("fk_ai_chat_threads_loan_id", "ai_chat_threads", type_="foreignkey")
    op.drop_column("ai_chat_threads", "loan_id")

    op.drop_column("documents", "scan_dirty")
    op.drop_column("documents", "ai_scan_confidence")
    op.drop_column("documents", "ai_scan_status")
    op.drop_column("documents", "ai_notes")
    op.drop_column("documents", "is_other")
    op.drop_column("documents", "checklist_key")
