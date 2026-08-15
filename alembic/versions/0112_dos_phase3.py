"""Dealer Capital OS Phase 3 Wave 1 — accounts, audit, category rules, doc requests.

- dos_accounts: bank accounts per dealer with AI-proposed roles. Precedence
  contract: AI proposes (role_set_by='ai'), a human role edit flips
  role_set_by='admin' and is never overwritten by later AI rematches.
- account_id (nullable, SET NULL) lands on dos_financial_periods,
  dos_cash_events, dos_documents so ledgers/periods become per-account while
  legacy null-account rows keep working.
- dos_financial_periods uniqueness moves from (dealer_id, period) to a
  functional unique index over (dealer_id, coalesce(account_id, zero-uuid),
  period) so one dealer can hold one row per (account, month) plus the
  legacy null-account row per month.
- dos_doc_requests: team-requested documents (Wave 3 consumes; table lands now).
- dos_audit_log: append-only human/AI action trail (created_at only).
- dos_category_rules: substring -> category rules; dealer-scoped rows override
  global (dealer_id NULL) rows, and rules beat heuristics at classify time.
- dos_addbacks.document_id: evidence document link (SET NULL).
- dos_dealers.handoff_intake_id: plain UUID breadcrumb to the intake that
  handed the dealer off — intentionally NO FK to avoid coupling to intakes.

Revision ID: 0112_dos_phase3
Revises: 0111_dos_bucket_link
Create Date: 2026-08-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg


revision = "0112_dos_phase3"
down_revision = "0111_dos_bucket_link"
branch_labels = None
depends_on = None


def _id():
    return sa.Column("id", pg.UUID(as_uuid=True), primary_key=True)


def _dealer_fk(nullable=False):
    return sa.Column(
        "dealer_id", pg.UUID(as_uuid=True), sa.ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=nullable
    )


def _ts():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


# Tables gaining a nullable account_id FK -> dos_accounts (SET NULL).
_ACCOUNT_LINKED = ("dos_financial_periods", "dos_cash_events", "dos_documents")


def upgrade() -> None:
    # --- dos_accounts -------------------------------------------------------
    op.create_table(
        "dos_accounts",
        _id(),
        _dealer_fk(),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("institution", sa.String(160)),
        sa.Column("mask", sa.String(8)),
        sa.Column("role", sa.String(24), nullable=False, server_default="other"),
        sa.Column("ai_proposed_role", sa.String(24)),
        sa.Column("ai_rationale", sa.Text()),
        sa.Column("role_set_by", sa.String(8), nullable=False, server_default="ai"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        *_ts(),
    )
    op.create_index("ix_dos_accounts_dealer", "dos_accounts", ["dealer_id"])

    # --- account_id on periods / cash events / documents --------------------
    for table in _ACCOUNT_LINKED:
        op.add_column(table, sa.Column("account_id", pg.UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_account_id", table, "dos_accounts", ["account_id"], ["id"], ondelete="SET NULL"
        )

    # --- per-account period uniqueness (functional index; legacy rows keep
    # --- account_id NULL and collapse onto the zero-uuid sentinel) ----------
    op.drop_constraint("uq_dos_period", "dos_financial_periods", type_="unique")
    op.execute(
        "CREATE UNIQUE INDEX uq_dos_period_acct ON dos_financial_periods "
        "(dealer_id, coalesce(account_id, '00000000-0000-0000-0000-000000000000'::uuid), period)"
    )

    # --- dos_doc_requests (Wave 3 consumes; schema lands now) ---------------
    op.create_table(
        "dos_doc_requests",
        _id(),
        _dealer_fk(),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False, server_default="statement"),
        sa.Column("account_id", pg.UUID(as_uuid=True), sa.ForeignKey("dos_accounts.id", ondelete="SET NULL")),
        sa.Column("due_on", sa.Date()),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),  # open|fulfilled|cancelled
        sa.Column(
            "fulfilled_document_id", pg.UUID(as_uuid=True), sa.ForeignKey("dos_documents.id", ondelete="SET NULL")
        ),
        sa.Column("note", sa.Text()),
        *_ts(),
    )

    # --- dos_audit_log (append-only: created_at only, no updated_at) --------
    op.create_table(
        "dos_audit_log",
        _id(),
        _dealer_fk(),
        sa.Column("actor_user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("actor_name", sa.String(120), nullable=False),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("entity_kind", sa.String(24), nullable=False),
        sa.Column("entity_id", pg.UUID(as_uuid=True)),
        sa.Column("before", pg.JSONB()),
        sa.Column("after", pg.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_dos_audit_dealer_created", "dos_audit_log", ["dealer_id", "created_at"])

    # --- dos_category_rules (dealer_id NULL = global rule) ------------------
    op.create_table(
        "dos_category_rules",
        _id(),
        _dealer_fk(nullable=True),
        sa.Column("pattern", sa.String(160), nullable=False),  # lowercase substring match
        sa.Column("category", sa.String(48), nullable=False),
        sa.Column("created_by_user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        *_ts(),
    )
    op.create_index("ix_dos_category_rules_dealer", "dos_category_rules", ["dealer_id"])

    # --- add-back evidence link + dealer handoff breadcrumb -----------------
    op.add_column("dos_addbacks", sa.Column("document_id", pg.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_dos_addbacks_document_id", "dos_addbacks", "dos_documents", ["document_id"], ["id"], ondelete="SET NULL"
    )
    # Plain UUID on purpose — no FK across to intakes (coupling avoidance).
    op.add_column("dos_dealers", sa.Column("handoff_intake_id", pg.UUID(as_uuid=True), nullable=True))


def downgrade() -> None:
    op.drop_column("dos_dealers", "handoff_intake_id")
    op.drop_constraint("fk_dos_addbacks_document_id", "dos_addbacks", type_="foreignkey")
    op.drop_column("dos_addbacks", "document_id")
    op.drop_index("ix_dos_category_rules_dealer", table_name="dos_category_rules")
    op.drop_table("dos_category_rules")
    op.drop_index("ix_dos_audit_dealer_created", table_name="dos_audit_log")
    op.drop_table("dos_audit_log")
    op.drop_table("dos_doc_requests")
    op.execute("DROP INDEX uq_dos_period_acct")
    op.create_unique_constraint("uq_dos_period", "dos_financial_periods", ["dealer_id", "period"])
    for table in reversed(_ACCOUNT_LINKED):
        op.drop_constraint(f"fk_{table}_account_id", table, type_="foreignkey")
        op.drop_column(table, "account_id")
    op.drop_index("ix_dos_accounts_dealer", table_name="dos_accounts")
    op.drop_table("dos_accounts")
