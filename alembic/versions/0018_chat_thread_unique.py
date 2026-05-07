"""Dedupe AI chat threads + enforce one canonical thread per (user, loan).

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-07

Operator surfaced 87 duplicate "New conversation" rows for a single
user (all loan_id=NULL, all empty). Root cause was the
`POST /ai/chat/threads` endpoint being hit on every send-from-blank
in the mobile sheet — a fresh row each time instead of looking up
the canonical (user, NULL) thread.

This migration:
  1. Dedupes existing rows: groups by (user_id, COALESCE(loan_id)),
     picks the oldest as canonical, moves any AIChatMessage rows
     from siblings onto it, deletes the siblings.
  2. Adds a partial unique idx on (user_id) WHERE loan_id IS NULL
     so account threads are also DB-enforced canonical (the
     loan-scoped uniqueness was already enforced in alembic 0017).

The find-or-create endpoint already does the right thing — this
migration just hardens the schema so future bugs in caller code
can't re-introduce duplicates.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) Pick the canonical thread per (user_id, loan_id) — the
    #    oldest row wins. Move every AIChatMessage from the
    #    sibling threads onto the canonical one, then delete the
    #    siblings. CTE keeps it a single round-trip.
    op.execute(
        """
        WITH ranked AS (
          SELECT
            id,
            user_id,
            loan_id,
            created_at,
            ROW_NUMBER() OVER (
              PARTITION BY user_id, loan_id
              ORDER BY created_at ASC, id ASC
            ) AS rn
          FROM ai_chat_threads
        ),
        canonical AS (
          SELECT user_id, loan_id, id AS canonical_id
          FROM ranked WHERE rn = 1
        )
        UPDATE ai_chat_messages m
        SET thread_id = c.canonical_id
        FROM ai_chat_threads t
        JOIN canonical c
          ON c.user_id = t.user_id
         AND (
           (c.loan_id IS NULL AND t.loan_id IS NULL)
           OR c.loan_id = t.loan_id
         )
        WHERE m.thread_id = t.id
          AND t.id <> c.canonical_id;
        """
    )

    # Now drop the de-duped sibling thread rows.
    op.execute(
        """
        WITH ranked AS (
          SELECT
            id,
            ROW_NUMBER() OVER (
              PARTITION BY user_id, loan_id
              ORDER BY created_at ASC, id ASC
            ) AS rn
          FROM ai_chat_threads
        )
        DELETE FROM ai_chat_threads
        WHERE id IN (SELECT id FROM ranked WHERE rn > 1);
        """
    )

    # 2) Partial unique idx on (user_id) WHERE loan_id IS NULL so
    #    account threads are DB-canonical. (loan-scoped uniqueness
    #    was already added in alembic 0017.)
    op.create_index(
        "ix_ai_chat_threads_user_account_uniq",
        "ai_chat_threads",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("loan_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_chat_threads_user_account_uniq",
        table_name="ai_chat_threads",
    )
    # Dedupe is irreversible — we keep the canonical merged data.
