"""Every file has a team, and one timeline everyone on it can read.

A file (`application_profiles`) gets one agent seat and any number of
underwriter seats (`file_team_members`), and a company (`company_id`, the
agent's referral partner company unless the desk set it). Every meaningful
write on the file appends a `file_events` row with a visibility tier; the
seats are told in-app at once, and a one-minute drain batches email notices
into one per person per file. See app/services/file_team.py and
app/services/file_events.py.

Revision ID: 0201_file_team_and_timeline
Revises: 0200_merchant_processing_offers
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0201_file_team_and_timeline"
down_revision = "0200_merchant_processing_offers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "application_profiles",
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("referral_partner_companies.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "application_profiles",
        sa.Column(
            "company_set_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_application_profiles_company", "application_profiles", ["company_id"])

    op.create_table(
        "file_team_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seat", sa.String(24), nullable=False),
        sa.Column(
            "assigned_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("derived_from", sa.String(48), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("profile_id", "user_id", "seat", name="uq_file_team_members_profile_user_seat"),
    )
    op.create_index("ix_file_team_members_profile", "file_team_members", ["profile_id"])
    op.create_index("ix_file_team_members_user", "file_team_members", ["user_id"])
    op.create_index(
        "uq_file_team_one_agent",
        "file_team_members",
        ["profile_id"],
        unique=True,
        postgresql_where=sa.text("seat = 'agent'"),
    )

    op.create_table(
        "file_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("visibility", sa.String(12), nullable=False),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_label", sa.String(120), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text, nullable=True),
        sa.Column("target_type", sa.String(60), nullable=True),
        sa.Column("target_id", sa.String(80), nullable=True),
        sa.Column("meta", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("notice_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("notice_recipients", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("notice_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_file_events_profile_created", "file_events", ["profile_id", "created_at"])
    op.create_index("ix_file_events_notice", "file_events", ["notice_status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_file_events_notice", table_name="file_events")
    op.drop_index("ix_file_events_profile_created", table_name="file_events")
    op.drop_table("file_events")
    op.drop_index("uq_file_team_one_agent", table_name="file_team_members")
    op.drop_index("ix_file_team_members_user", table_name="file_team_members")
    op.drop_index("ix_file_team_members_profile", table_name="file_team_members")
    op.drop_table("file_team_members")
    op.drop_index("ix_application_profiles_company", table_name="application_profiles")
    op.drop_column("application_profiles", "company_set_by_user_id")
    op.drop_column("application_profiles", "company_id")
