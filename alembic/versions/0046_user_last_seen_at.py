"""User.last_seen_at — presence timestamp bumped on every authed request.

Revision ID: 0046
Revises: 0045
Create Date: 2026-05-12

Adds `users.last_seen_at` so operators can see at a glance whether the
borrower is actively using the app. The column is bumped automatically
by the auth dependency on every request; nothing the client has to opt
in to.

The loan-detail header surfaces this as a green dot ("Online · just
now") or a gray dot with relative time ("Last seen 18 min ago"). All
roles get the bump — agents + super-admins also benefit downstream
when we want a "who's working right now" view.

Nullable so existing rows continue to work; treated as "never seen"
until the user hits any authed endpoint.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Partial index — queries that want "currently-online" filter on
    # last_seen_at > now() - interval. Avoids scanning never-seen rows.
    op.create_index(
        "ix_users_last_seen_recent",
        "users",
        ["last_seen_at"],
        postgresql_where=sa.text("last_seen_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_users_last_seen_recent", table_name="users")
    op.drop_column("users", "last_seen_at")
