"""Add user-owned booking settings.

Revision ID: 0082_user_booking_settings
Revises: 0081_rate_sheet_amount_credit_tiers
Create Date: 2026-06-27
"""

from __future__ import annotations

import uuid
import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0082_user_booking_settings"
down_revision = "0081_rate_sheet_amount_credit_tiers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "booking_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=140), nullable=True),
        sa.Column("intro", sa.String(length=600), nullable=True),
        sa.Column("primary_color", sa.String(length=7), server_default="#5eead4", nullable=False),
        sa.Column("background_color", sa.String(length=7), server_default="#05070d", nullable=False),
        sa.Column("duration_min", sa.Integer(), server_default="30", nullable=False),
        sa.Column("timezone", sa.String(length=80), server_default="America/New_York", nullable=False),
        sa.Column("available_days", postgresql.JSONB(astext_type=sa.Text()), server_default="[1,2,3,4,5]", nullable=False),
        sa.Column("start_time", sa.String(length=5), server_default="09:00", nullable=False),
        sa.Column("end_time", sa.String(length=5), server_default="17:00", nullable=False),
        sa.Column("logo_s3_key", sa.String(length=512), nullable=True),
        sa.Column("profile_photo_s3_key", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_unique_constraint("uq_booking_settings_user_id", "booking_settings", ["user_id"])
    op.create_unique_constraint("uq_booking_settings_slug", "booking_settings", ["slug"])
    op.create_index("ix_booking_settings_enabled_slug", "booking_settings", ["enabled", "slug"])

    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, user_id, settings_data FROM brokers WHERE settings_data IS NOT NULL")).mappings().all()
    used_slugs: set[str] = set()
    for row in rows:
        settings = row["settings_data"] or {}
        if not isinstance(settings, dict):
            continue
        booking = settings.get("booking") or {}
        if not isinstance(booking, dict):
            continue
        slug = booking.get("slug")
        if not slug or slug in used_slugs:
            continue
        used_slugs.add(slug)
        bind.execute(
            sa.text(
                """
                INSERT INTO booking_settings (
                    id, user_id, enabled, slug, title, intro, primary_color,
                    background_color, duration_min, timezone, available_days,
                    start_time, end_time, created_at, updated_at
                )
                VALUES (
                    :id, :user_id, :enabled, :slug, :title, :intro, :primary_color,
                    :background_color, :duration_min, :timezone, CAST(:available_days AS jsonb),
                    :start_time, :end_time, now(), now()
                )
                ON CONFLICT (user_id) DO NOTHING
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "user_id": str(row["user_id"]),
                "enabled": bool(booking.get("enabled", False)),
                "slug": slug,
                "title": booking.get("title"),
                "intro": booking.get("intro"),
                "primary_color": booking.get("primary_color") or "#5eead4",
                "background_color": booking.get("background_color") or "#05070d",
                "duration_min": int(booking.get("duration_min") or 30),
                "timezone": booking.get("timezone") or "America/New_York",
                "available_days": json.dumps(booking.get("available_days") or [1, 2, 3, 4, 5]),
                "start_time": booking.get("start_time") or "09:00",
                "end_time": booking.get("end_time") or "17:00",
            },
        )


def downgrade() -> None:
    op.drop_index("ix_booking_settings_enabled_slug", table_name="booking_settings")
    op.drop_constraint("uq_booking_settings_slug", "booking_settings", type_="unique")
    op.drop_constraint("uq_booking_settings_user_id", "booking_settings", type_="unique")
    op.drop_table("booking_settings")
