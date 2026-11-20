"""Encrypted SSNs and staff access records. Existing application answers stay as entered."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0285_secure_ssns"
down_revision = "0284_ai_suggestion_closure"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "secure_ssns",
        sa.Column("profile_id", UUID(), sa.ForeignKey("application_profiles.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("person_name", sa.String(120), nullable=False),
        sa.Column("last_four", sa.String(4), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "secure_ssn_access",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("profile_id", UUID(), sa.ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entry_id", UUID(), nullable=False),
        sa.Column("user_id", UUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("accessed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_secure_ssn_access_profile_id", "secure_ssn_access", ["profile_id"])


def downgrade():
    op.drop_table("secure_ssn_access")
    op.drop_table("secure_ssns")
