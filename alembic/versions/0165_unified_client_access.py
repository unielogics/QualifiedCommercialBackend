"""Unified client product access and access audit.

Revision ID: 0165_unified_client_access
Revises: 0164_plaid_assets_ingestion
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0165_unified_client_access"
down_revision = "0164_plaid_assets_ingestion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("account_status", sa.String(24), nullable=False, server_default="active"),
    )
    op.create_check_constraint(
        "ck_users_account_status",
        "users",
        "account_status IN ('active', 'suspended')",
    )
    op.add_column("users", sa.Column("last_invited_at", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("last_invite_status", sa.String(24)))
    op.add_column("users", sa.Column("last_invite_error", sa.Text()))
    op.add_column("users", sa.Column("suspended_at", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("suspended_by_user_id", postgresql.UUID(as_uuid=True)))
    op.create_foreign_key(
        "fk_users_suspended_by_user_id",
        "users",
        "users",
        ["suspended_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "user_product_access",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product", sa.String(24), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("granted_by_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_by_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("product IN ('funding', 'audit')", name="ck_user_product_access_product"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["granted_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["revoked_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("user_id", "product", name="uq_user_product_access_user_product"),
    )
    op.create_index("ix_user_product_access_user_id", "user_product_access", ["user_id"])
    op.create_index(
        "ix_user_product_access_product_enabled", "user_product_access", ["product", "enabled"]
    )

    op.create_table(
        "user_access_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("before_state", postgresql.JSONB()),
        sa.Column("after_state", postgresql.JSONB()),
        sa.Column("request_metadata", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_user_access_events_user_id", "user_access_events", ["user_id"])
    op.create_index(
        "ix_user_access_events_user_created", "user_access_events", ["user_id", "created_at"]
    )
    op.create_index(
        "ix_user_access_events_actor_created", "user_access_events", ["actor_user_id", "created_at"]
    )

    # Backfill from explicit identity links and legacy client product roles.
    op.execute(
        """
        INSERT INTO user_product_access (id, user_id, product, enabled, granted_at, created_at, updated_at)
        SELECT gen_random_uuid(), u.id, 'funding', true, now(), now(), now()
        FROM users u
        WHERE u.role = 'client'
           OR EXISTS (SELECT 1 FROM clients c WHERE c.user_id = u.id)
        ON CONFLICT (user_id, product) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO user_product_access (id, user_id, product, enabled, granted_at, created_at, updated_at)
        SELECT gen_random_uuid(), u.id, 'audit', true, now(), now(), now()
        FROM users u
        WHERE u.role = 'dealer'
           OR EXISTS (SELECT 1 FROM dos_dealers d WHERE d.dealer_user_id = u.id)
        ON CONFLICT (user_id, product) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("ix_user_access_events_actor_created", table_name="user_access_events")
    op.drop_index("ix_user_access_events_user_created", table_name="user_access_events")
    op.drop_index("ix_user_access_events_user_id", table_name="user_access_events")
    op.drop_table("user_access_events")
    op.drop_index("ix_user_product_access_product_enabled", table_name="user_product_access")
    op.drop_index("ix_user_product_access_user_id", table_name="user_product_access")
    op.drop_table("user_product_access")
    op.drop_constraint("fk_users_suspended_by_user_id", "users", type_="foreignkey")
    op.drop_column("users", "suspended_by_user_id")
    op.drop_column("users", "suspended_at")
    op.drop_column("users", "last_invite_error")
    op.drop_column("users", "last_invite_status")
    op.drop_column("users", "last_invited_at")
    op.drop_constraint("ck_users_account_status", "users", type_="check")
    op.drop_column("users", "account_status")
