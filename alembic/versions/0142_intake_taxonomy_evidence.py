"""AI-intake taxonomy, extraction provenance, and secure verification drafts.

Revision ID: 0142_intake_taxonomy_evidence
Revises: 0141_application_profiles
"""

from __future__ import annotations

import csv
import json
import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0142_intake_taxonomy_evidence"
down_revision = "0141_application_profiles"
branch_labels = None
depends_on = None

CATALOG_NAMESPACE = uuid.UUID("61d737ab-5f40-4fdf-b85d-074d82842210")


def _entry_id(level: int, code: str) -> uuid.UUID:
    return uuid.uuid5(CATALOG_NAMESPACE, f"2022:{level}:{code}")


def upgrade() -> None:
    op.create_table(
        "application_funding_categories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("vertical", sa.String(32), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("requirements", postgresql.JSONB()),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("vertical", "slug", name="uq_application_funding_category_slug"),
    )
    op.create_index("ix_application_funding_categories_status", "application_funding_categories", ["status"])

    op.create_table(
        "application_taxonomy_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(8)),
        sa.Column("label", sa.String(180), nullable=False),
        sa.Column("normalized_label", sa.String(180), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("application_taxonomy_entries.id", ondelete="SET NULL")),
        sa.Column("source", sa.String(32), nullable=False, server_default="custom"),
        sa.Column("taxonomy_version", sa.String(24), nullable=False, server_default="2022"),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("aliases", postgresql.JSONB()),
        sa.Column("originating_profile_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("application_profiles.id", ondelete="SET NULL")),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("reviewed_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("canonical_entry_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("application_taxonomy_entries.id", ondelete="SET NULL")),
        sa.Column("review_note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_application_taxonomy_level_status", "application_taxonomy_entries", ["level", "status"])
    op.create_index("ix_application_taxonomy_parent", "application_taxonomy_entries", ["parent_id"])
    op.create_index("ix_application_taxonomy_normalized", "application_taxonomy_entries", ["normalized_label"])
    op.create_index(
        "uq_application_taxonomy_active_code",
        "application_taxonomy_entries",
        ["taxonomy_version", "level", "code"],
        unique=True,
        postgresql_where=sa.text("code IS NOT NULL AND status IN ('official', 'approved')"),
    )

    for _name, column in (
        ("subindustry", sa.Column("subindustry", sa.String(120))),
        ("industry_entry_id", sa.Column("industry_entry_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("application_taxonomy_entries.id", ondelete="SET NULL"))),
        ("subindustry_entry_id", sa.Column("subindustry_entry_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("application_taxonomy_entries.id", ondelete="SET NULL"))),
        ("activity_entry_id", sa.Column("activity_entry_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("application_taxonomy_entries.id", ondelete="SET NULL"))),
        ("taxonomy_version", sa.Column("taxonomy_version", sa.String(24), nullable=False, server_default="2022")),
        ("classification_provenance", sa.Column("classification_provenance", postgresql.JSONB())),
        ("is_draft", sa.Column("is_draft", sa.Boolean(), nullable=False, server_default=sa.false())),
        ("draft_finalized_at", sa.Column("draft_finalized_at", sa.DateTime(timezone=True))),
        ("extraction_reviewed_at", sa.Column("extraction_reviewed_at", sa.DateTime(timezone=True))),
        ("bank_verification_override_at", sa.Column("bank_verification_override_at", sa.DateTime(timezone=True))),
        ("bank_verification_override_by_user_id", sa.Column("bank_verification_override_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"))),
        ("bank_verification_override_reason", sa.Column("bank_verification_override_reason", sa.Text())),
    ):
        op.add_column("application_profiles", column)

    op.create_table(
        "application_extracted_facts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("field_key", sa.String(64), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("normalized_value", sa.Text()),
        sa.Column("confidence", sa.Numeric(5, 4)),
        sa.Column("source_file_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bucket_files.id", ondelete="SET NULL")),
        sa.Column("source_analysis_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bucket_file_analyses.id", ondelete="SET NULL")),
        sa.Column("extraction_method", sa.String(32), nullable=False, server_default="document_ai"),
        sa.Column("status", sa.String(24), nullable=False, server_default="suggested"),
        sa.Column("reviewed_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_application_extracted_facts_profile_status", "application_extracted_facts", ["profile_id", "status"])
    op.create_index("ix_application_extracted_facts_source", "application_extracted_facts", ["source_file_id"])

    op.create_table(
        "application_verification_invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("recipient_email", sa.String(320)),
        sa.Column("recipient_phone", sa.String(48)),
        sa.Column("purpose", sa.String(32), nullable=False, server_default="business_banking"),
        sa.Column("delivery_status", sa.String(24), nullable=False, server_default="created"),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_application_verification_invitations_profile", "application_verification_invitations", ["profile_id"])
    op.create_index("ix_application_verification_invitations_expires", "application_verification_invitations", ["expires_at"])

    connection = op.get_bind()
    taxonomy = sa.table(
        "application_taxonomy_entries",
        sa.column("id", postgresql.UUID(as_uuid=True)), sa.column("level", sa.Integer()),
        sa.column("code", sa.String()), sa.column("label", sa.String()), sa.column("normalized_label", sa.String()),
        sa.column("parent_id", postgresql.UUID(as_uuid=True)), sa.column("source", sa.String()),
        sa.column("taxonomy_version", sa.String()), sa.column("status", sa.String()), sa.column("aliases", postgresql.JSONB()),
    )
    source_path = Path(__file__).resolve().parents[2] / "app" / "data" / "naics_2022.csv"
    rows = []
    with source_path.open(encoding="utf-8", newline="") as handle:
        for item in csv.DictReader(handle):
            level = int(item["level"])
            code = item["code"]
            rows.append({
                "id": _entry_id(level, code), "level": level, "code": code,
                "label": item["label"], "normalized_label": " ".join(item["label"].casefold().split()),
                "parent_id": _entry_id(2 if level == 3 else 3, item["parent_code"]) if item["parent_code"] else None,
                "source": item["source"], "taxonomy_version": "2022", "status": "official",
                "aliases": json.loads(item["aliases"] or "[]"),
            })
    for offset in range(0, len(rows), 250):
        connection.execute(taxonomy.insert(), rows[offset:offset + 250])

    categories = sa.table(
        "application_funding_categories",
        sa.column("id", postgresql.UUID(as_uuid=True)), sa.column("vertical", sa.String()),
        sa.column("slug", sa.String()), sa.column("label", sa.String()), sa.column("status", sa.String()),
        sa.column("is_system", sa.Boolean()), sa.column("requirements", postgresql.JSONB()),
    )
    defaults = {
        "real_estate": [("dscr", "DSCR"), ("fix-and-flip", "Fix and flip"), ("ground-up", "Ground-up construction"), ("bridge", "Bridge")],
        "main_street": [("working-capital", "Working capital"), ("equipment", "Equipment financing"), ("sba", "SBA"), ("business-acquisition", "Business acquisition")],
        "dealer": [("inventory", "Inventory financing"), ("floorplan", "Floorplan"), ("working-capital", "Working capital")],
        "mca": [("consolidation", "MCA consolidation"), ("refinance", "MCA refinance")],
    }
    connection.execute(categories.insert(), [
        {"id": uuid.uuid5(CATALOG_NAMESPACE, f"funding:{vertical}:{slug}"), "vertical": vertical, "slug": slug,
         "label": label, "status": "active", "is_system": True, "requirements": {}}
        for vertical, values in defaults.items() for slug, label in values
    ])


def downgrade() -> None:
    op.drop_table("application_verification_invitations")
    op.drop_table("application_extracted_facts")
    for column in ("bank_verification_override_reason", "bank_verification_override_by_user_id", "bank_verification_override_at", "extraction_reviewed_at", "draft_finalized_at", "is_draft", "classification_provenance", "taxonomy_version", "activity_entry_id", "subindustry_entry_id", "industry_entry_id", "subindustry"):
        op.drop_column("application_profiles", column)
    op.drop_table("application_taxonomy_entries")
    op.drop_table("application_funding_categories")
