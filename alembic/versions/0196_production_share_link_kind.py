"""A production package can be forwarded to someone with no account.

The share link was built for the opposite: its own docstring said the link
"is useless without the named rep's own session", and rep_user_id was NOT
NULL under a unique index. The owner wants the debt-schedule pattern — a
link an agent can open and fill in without logging in — so a link now has a
kind. `rep` is what shipped, unchanged. `public` carries no rep, and is
opened with a six-digit PIN the sharer communicates separately.

The PIN is a PBKDF2 hash like the client room's. Wrong attempts are counted
on the row and lock it for a while, because an in-memory counter on a
single worker forgets on every restart and a forwarded link is reachable
from the whole internet.

Revision ID: 0196_production_share_link_kind
Revises: 0195_message_sends
"""

import sqlalchemy as sa

from alembic import op

revision = "0196_production_share_link_kind"
down_revision = "0195_message_sends"
branch_labels = None
depends_on = None

_TABLE = "production_package_share_links"
_LIVE = "uq_production_package_share_links_live"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("kind", sa.String(16), nullable=False, server_default="rep"))
    op.alter_column(_TABLE, "rep_user_id", existing_type=sa.dialects.postgresql.UUID(as_uuid=True), nullable=True)
    op.add_column(_TABLE, sa.Column("recipient_name", sa.String(120), nullable=True))
    op.add_column(_TABLE, sa.Column("recipient_email", sa.String(320), nullable=True))
    op.add_column(_TABLE, sa.Column("pin_hash", sa.String(160), nullable=True))
    op.add_column(_TABLE, sa.Column("pin_set_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(_TABLE, sa.Column("pin_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column(_TABLE, sa.Column("pin_locked_until", sa.DateTime(timezone=True), nullable=True))
    # One live link per rep per package still holds; a package may carry any
    # number of live forwarded links.
    op.drop_index(_LIVE, table_name=_TABLE)
    op.create_index(_LIVE, _TABLE, ["package_id", "rep_user_id"], unique=True,
                    postgresql_where=sa.text("revoked_at IS NULL AND kind = 'rep'"))


def downgrade() -> None:
    # A forwarded link has no rep to fall back to; it cannot survive the old shape.
    op.execute(f"DELETE FROM {_TABLE} WHERE kind <> 'rep'")
    op.drop_index(_LIVE, table_name=_TABLE)
    op.create_index(_LIVE, _TABLE, ["package_id", "rep_user_id"], unique=True,
                    postgresql_where=sa.text("revoked_at IS NULL"))
    for col in ("pin_locked_until", "pin_attempts", "pin_set_at", "pin_hash", "recipient_email", "recipient_name"):
        op.drop_column(_TABLE, col)
    op.alter_column(_TABLE, "rep_user_id", existing_type=sa.dialects.postgresql.UUID(as_uuid=True), nullable=False)
    op.drop_column(_TABLE, "kind")
