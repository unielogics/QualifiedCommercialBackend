"""Let staff explicitly select internal notes for AI context."""
import sqlalchemy as sa

from alembic import op

revision = "0289_internal_note_ai_context"
down_revision = "0288_permission_requests"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bucket_notes", sa.Column("include_in_ai_context", sa.Boolean(),
        nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("bucket_notes", "include_in_ai_context")
