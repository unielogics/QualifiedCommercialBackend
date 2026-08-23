"""Global application profiles, ownership, bank lineage, and note channels.

Revision ID: 0141_application_profiles
Revises: 0140_dos_multi_owner_multi_bank
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0141_application_profiles"
down_revision = "0140_dos_multi_owner_multi_bank"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "application_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clients.id", ondelete="SET NULL")),
        sa.Column("deal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("deals.id", ondelete="SET NULL")),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("loans.id", ondelete="SET NULL")),
        sa.Column("intake_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("public_underwriting_intakes.id", ondelete="SET NULL")),
        sa.Column("dealer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dos_dealers.id", ondelete="SET NULL")),
        sa.Column("primary_bucket_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("buckets.id", ondelete="SET NULL")),
        sa.Column("vertical", sa.String(32), nullable=False, server_default="main_street"),
        sa.Column("funding_category", sa.String(64)),
        sa.Column("entity_type", sa.String(32)),
        sa.Column("industry", sa.String(80)),
        sa.Column("naics_code", sa.String(8)),
        sa.Column("naics_label", sa.String(180)),
        sa.Column("custom_industry", sa.String(180)),
        sa.Column("classification_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("classification_state", postgresql.JSONB()),
        sa.Column("classified_at", sa.DateTime(timezone=True)),
        sa.Column("classified_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("backfill_needs_review", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("deal_id", name="uq_application_profiles_deal"),
        sa.UniqueConstraint("loan_id", name="uq_application_profiles_loan"),
        sa.UniqueConstraint("intake_id", name="uq_application_profiles_intake"),
        sa.UniqueConstraint("dealer_id", name="uq_application_profiles_dealer"),
    )
    op.create_index("ix_application_profiles_client", "application_profiles", ["client_id"])
    op.create_index("ix_application_profiles_bucket", "application_profiles", ["primary_bucket_id"])

    op.create_table(
        "application_owners",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("first_name", sa.String(80), nullable=False),
        sa.Column("last_name", sa.String(80), nullable=False),
        sa.Column("email", sa.String(320)),
        sa.Column("phone", sa.String(48)),
        sa.Column("ownership_pct", sa.Numeric(5, 2)),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_guarantor", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("dob", sa.Date()),
        sa.Column("street", sa.String(240)),
        sa.Column("city", sa.String(120)),
        sa.Column("state", sa.String(8)),
        sa.Column("zip", sa.String(12)),
        sa.Column("invite_token_hash", sa.String(64), unique=True),
        sa.Column("invite_sent_at", sa.DateTime(timezone=True)),
        sa.Column("invite_opened_at", sa.DateTime(timezone=True)),
        sa.Column("credit_pull_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("credit_pulls.id", ondelete="SET NULL")),
        sa.Column("credit_score", sa.Integer()),
        sa.Column("credit_tier", sa.String(16)),
        sa.Column("credit_pulled_at", sa.DateTime(timezone=True)),
        sa.Column("credit_summary", postgresql.JSONB()),
        sa.Column("notes", sa.Text()),
        sa.Column("backfill_needs_review", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "ownership_pct IS NULL OR (ownership_pct >= 0 AND ownership_pct <= 100)",
            name="ck_application_owners_percentage",
        ),
    )
    op.create_index("ix_application_owners_profile", "application_owners", ["profile_id"])
    op.create_index("ix_application_owners_credit_pull", "application_owners", ["credit_pull_id"])
    op.create_index(
        "uq_application_owners_primary",
        "application_owners",
        ["profile_id"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
    )
    op.create_index(
        "uq_application_owners_email",
        "application_owners",
        ["profile_id", sa.text("lower(email)")],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL"),
    )

    op.create_table(
        "application_plaid_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("item_id", sa.String(64), nullable=False, unique=True),
        sa.Column("institution_name", sa.String(160)),
        sa.Column("accounts_label", sa.String(200)),
        sa.Column("encrypted_access_token", sa.Text()),
        sa.Column("encryption_provider", sa.String(16), nullable=False, server_default="fernet"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("error", sa.Text()),
        sa.Column("last_pulled_at", sa.DateTime(timezone=True)),
        sa.Column("next_refresh_at", sa.DateTime(timezone=True)),
        sa.Column("auto_refresh", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_primary_operating", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_application_plaid_items_profile", "application_plaid_items", ["profile_id"])
    op.create_index(
        "uq_application_plaid_items_primary",
        "application_plaid_items",
        ["profile_id"],
        unique=True,
        postgresql_where=sa.text("is_primary_operating AND status <> 'removed'"),
    )

    op.create_table(
        "application_bank_consents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False),
        sa.Column("method", sa.String(24), nullable=False),
        sa.Column("disclosure_version", sa.String(24), nullable=False),
        sa.Column("disclosure_hash", sa.String(64), nullable=False),
        sa.Column("disclosure_text", sa.Text(), nullable=False),
        sa.Column("consenter_name", sa.String(160)),
        sa.Column("captured_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("ip_address", sa.String(64)),
        sa.Column("user_agent", sa.String(400)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_application_bank_consents_profile", "application_bank_consents", ["profile_id"])

    op.add_column(
        "credit_pulls",
        sa.Column("application_owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("application_owners.id", ondelete="SET NULL")),
    )
    op.create_index("ix_credit_pulls_application_owner_id", "credit_pulls", ["application_owner_id"])
    op.add_column(
        "bucket_files",
        sa.Column("application_plaid_item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("application_plaid_items.id", ondelete="SET NULL")),
    )
    op.add_column("bucket_files", sa.Column("plaid_statement_id", sa.String(128)))
    op.add_column("bucket_files", sa.Column("statement_period", sa.String(7)))
    op.create_index("ix_bucket_files_application_plaid_item", "bucket_files", ["application_plaid_item_id"])
    op.create_index(
        "uq_bucket_files_application_plaid_statement",
        "bucket_files",
        ["application_plaid_item_id", "plaid_statement_id"],
        unique=True,
        postgresql_where=sa.text("application_plaid_item_id IS NOT NULL AND plaid_statement_id IS NOT NULL"),
    )
    op.add_column(
        "bucket_notes",
        sa.Column("channel", sa.String(16), nullable=False, server_default="partner"),
    )
    op.create_index("ix_bucket_notes_channel", "bucket_notes", ["bucket_id", "channel"])

    # Dealer-backed files remain adapters over dos_owners/dos_plaid_items.
    op.execute(
        """
        INSERT INTO application_profiles (
            id, dealer_id, primary_bucket_id, vertical, funding_category,
            entity_type, industry, naics_code, naics_label, backfill_needs_review
        )
        SELECT gen_random_uuid(), d.id, d.bucket_id, 'dealer', d.funding_purpose,
               d.entity_type, d.industry, d.naics_code, d.naics_label, false
          FROM dos_dealers d
        """
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT d.id AS dealer_id, d.handoff_intake_id AS intake_id,
                   row_number() OVER (PARTITION BY d.handoff_intake_id ORDER BY d.created_at, d.id) AS rn
              FROM dos_dealers d
             WHERE d.handoff_intake_id IS NOT NULL
        )
        UPDATE application_profiles p
           SET intake_id = ranked.intake_id,
               client_id = i.client_id,
               primary_bucket_id = COALESCE(p.primary_bucket_id, i.bucket_id)
          FROM ranked
          JOIN public_underwriting_intakes i ON i.id = ranked.intake_id
         WHERE p.dealer_id = ranked.dealer_id AND ranked.rn = 1
        """
    )
    op.execute(
        """
        INSERT INTO application_profiles (
            id, client_id, intake_id, primary_bucket_id, vertical,
            funding_category, industry, backfill_needs_review
        )
        SELECT gen_random_uuid(), i.client_id, i.id, i.bucket_id,
               CASE
                   WHEN i.variant ILIKE '%real_estate%' OR i.variant ILIKE '%funding_review%' THEN 'real_estate'
                   WHEN i.variant ILIKE '%mca%' THEN 'mca'
                   WHEN i.variant ILIKE '%dealer%' THEN 'dealer'
                   ELSE 'main_street'
               END,
               i.loan_purpose,
               i.intake_state #>> '{main_street_details,industry}',
               true
          FROM public_underwriting_intakes i
         WHERE NOT EXISTS (
             SELECT 1 FROM application_profiles p WHERE p.intake_id = i.id
         )
        """
    )
    op.execute(
        """
        INSERT INTO application_profiles (
            id, client_id, deal_id, vertical, funding_category, backfill_needs_review
        )
        SELECT gen_random_uuid(), d.client_id, d.id, 'real_estate', d.deal_type, true
          FROM deals d
        """
    )
    op.execute(
        """
        UPDATE application_profiles p
           SET loan_id = l.id
          FROM loans l
         WHERE p.deal_id = l.source_deal_id AND p.loan_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE application_profiles p
           SET loan_id = l.id
          FROM loans l
         WHERE p.intake_id = l.source_intake_id
           AND p.loan_id IS NULL
           AND NOT EXISTS (SELECT 1 FROM application_profiles x WHERE x.loan_id = l.id)
        """
    )
    op.execute(
        """
        UPDATE application_profiles p
           SET loan_id = i.promoted_loan_id
          FROM public_underwriting_intakes i
         WHERE p.intake_id = i.id
           AND i.promoted_loan_id IS NOT NULL
           AND p.loan_id IS NULL
           AND NOT EXISTS (
               SELECT 1 FROM application_profiles x WHERE x.loan_id = i.promoted_loan_id
           )
        """
    )
    op.execute(
        """
        INSERT INTO application_profiles (
            id, client_id, loan_id, vertical, funding_category, entity_type,
            backfill_needs_review
        )
        SELECT gen_random_uuid(), l.client_id, l.id,
               CASE WHEN lower(CAST(l.type AS text)) LIKE '%mca%' THEN 'mca' ELSE 'real_estate' END,
               CAST(l.purpose AS text), CAST(l.entity_type AS text), true
          FROM loans l
         WHERE NOT EXISTS (
             SELECT 1 FROM application_profiles p WHERE p.loan_id = l.id
         )
        """
    )
    op.execute(
        """
        INSERT INTO application_owners (
            id, profile_id, first_name, last_name, email, phone,
            ownership_pct, is_primary, is_guarantor, backfill_needs_review
        )
        SELECT gen_random_uuid(), p.id,
               split_part(trim(COALESCE(i.full_name, c.name, 'Unknown Owner')), ' ', 1),
               COALESCE(
                   NULLIF(trim(substr(trim(COALESCE(i.full_name, c.name, 'Unknown Owner')),
                       strpos(trim(COALESCE(i.full_name, c.name, 'Unknown Owner')), ' ') + 1)), ''),
                   'Unknown'
               ),
               COALESCE(i.email, c.email), COALESCE(i.phone, c.phone),
               100.00, true, true, true
          FROM application_profiles p
          LEFT JOIN public_underwriting_intakes i ON i.id = p.intake_id
          LEFT JOIN clients c ON c.id = p.client_id
         WHERE p.dealer_id IS NULL
           AND COALESCE(i.full_name, c.name) IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_bucket_notes_channel", table_name="bucket_notes")
    op.drop_column("bucket_notes", "channel")
    op.drop_index("ix_bucket_files_application_plaid_item", table_name="bucket_files")
    op.drop_index("uq_bucket_files_application_plaid_statement", table_name="bucket_files")
    op.drop_column("bucket_files", "statement_period")
    op.drop_column("bucket_files", "plaid_statement_id")
    op.drop_column("bucket_files", "application_plaid_item_id")
    op.drop_index("ix_credit_pulls_application_owner_id", table_name="credit_pulls")
    op.drop_column("credit_pulls", "application_owner_id")
    op.drop_index("ix_application_bank_consents_profile", table_name="application_bank_consents")
    op.drop_table("application_bank_consents")
    op.drop_index("uq_application_plaid_items_primary", table_name="application_plaid_items")
    op.drop_index("ix_application_plaid_items_profile", table_name="application_plaid_items")
    op.drop_table("application_plaid_items")
    op.drop_index("uq_application_owners_email", table_name="application_owners")
    op.drop_index("uq_application_owners_primary", table_name="application_owners")
    op.drop_index("ix_application_owners_credit_pull", table_name="application_owners")
    op.drop_index("ix_application_owners_profile", table_name="application_owners")
    op.drop_table("application_owners")
    op.drop_index("ix_application_profiles_bucket", table_name="application_profiles")
    op.drop_index("ix_application_profiles_client", table_name="application_profiles")
    op.drop_table("application_profiles")
